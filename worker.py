import os
import sys
import time
import re
import json
import socket
import platform
import subprocess
import atexit
import signal
import requests
import traceback
import unicodedata
from datetime import datetime
from dotenv import load_dotenv

from selenium.webdriver.common.by import By
from loguru import logger

# Carrega configurações locais do .env (só conexão com o painel; credenciais
# do robô vêm do painel, ver REQUIRED_ENV_KEYS abaixo)
load_dotenv()

# Log em arquivo (roda como servico oculto no startup, sem console visivel):
# sem isso, qualquer log de uma execucao sem terminal (schtasks /sc onlogon)
# se perde, e um crash nao deixa nenhum rastro pra diagnosticar depois.
_LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
logger.add(os.path.join(_LOG_DIR, "worker_{time:YYYY-MM-DD}.log"),
           rotation="00:00", retention="14 days", encoding="utf-8", enqueue=True,
           level="INFO")

# Corrige encoding no Windows para logs do terminal
if sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')


class _EspelharNoLog:
    """Espelha stdout/stderr (print, tracebacks) tambem para o logger.

    Sem isso, a pasta logs/ e criada mas o arquivo fica vazio ate a primeira
    tarefa chegar: todo o status do loop principal (fila vazia, erro de rede,
    config pendente no painel) usa print(), que some quando o worker roda
    oculto (schtasks) sem console. Mantem a saida original tambem (util em
    execucao manual/interativa).
    """
    def __init__(self, stream_original, nivel):
        self._original = stream_original
        self._nivel = nivel
        self._buffer = ""

    def write(self, texto):
        self._original.write(texto)
        self._buffer += texto
        while "\n" in self._buffer:
            linha, self._buffer = self._buffer.split("\n", 1)
            linha = linha.strip()
            if linha:
                logger.log(self._nivel, linha)

    def flush(self):
        self._original.flush()

    def isatty(self):
        return False

    def __getattr__(self, nome):
        # Delega qualquer outro metodo/atributo (reconfigure, encoding, fileno...)
        # pro stream real, sem precisar reimplementar a interface toda de TextIO.
        return getattr(self._original, nome)


sys.stdout = _EspelharNoLog(sys.stdout, "INFO")
sys.stderr = _EspelharNoLog(sys.stderr, "ERROR")

from src.core.browser_manager import start_browser, connect_browser, limpar_navegadores_orfaos
from src.siebel_api import SiebelAPIClient
from src.auth.login_captcha import loginVivo

# Referencia ao driver atual para o handler de encerramento abaixo: com
# Ctrl+C ou um "encerrar tarefa" gracioso, sem isso o chrome.exe ficaria
# orfao (ninguem chama driver.quit()) ate a proxima limpeza no startup.
_driver_ativo = {"driver": None}

def _encerrar_navegador_ao_sair(*_args):
    driver = _driver_ativo.get("driver")
    if driver:
        try: driver.quit()
        except: pass
    if _args:
        sys.exit(0)

atexit.register(_encerrar_navegador_ao_sair)
signal.signal(signal.SIGINT, _encerrar_navegador_ao_sair)
if hasattr(signal, "SIGBREAK"):
    signal.signal(signal.SIGBREAK, _encerrar_navegador_ao_sair)

# --- CONFIGURAÇÃO AUTOMÁTICA ---
# Conexão com o painel. Fica no .env (e não fixa no código como no script da
# aba "API & Integração") porque o mesmo código roda contra o painel de
# produção e o de desenvolvimento, e o token muda entre os dois.
BASE_URL = os.getenv("API_BASE_URL", "http://192.168.100.2/workers").rstrip('/')  # URL do seu Dashboard
QUEUE_SLUG = os.getenv("QUEUE_SLUG", "vivosmartcobertura")
QUEUE_TOKEN = os.getenv("QUEUE_TOKEN", "")  # Token Mestre desta fila (aba "API & Integração")
MACHINE_NAME = socket.gethostname()  # Hostname automático
# `{machine}` deixa o mesmo .env versionado funcionar em vários PCs sem que
# todos se apresentem ao painel como a mesma instância.
WORKER_NAME = os.getenv("WORKER_NAME", "Worker-Cobertura-{machine}").replace("{machine}", MACHINE_NAME)

# --- VARIÁVEIS DE AMBIENTE DO ROBÔ (lidas do painel, não de .env local) ---
# Chaves que ESTE robô precisa para funcionar. O painel descobre a lista pelo
# header X-Worker-Vars-Keys a cada chamada e segura a entrega de tarefas
# (HTTP 400) enquanto alguma chave obrigatória estiver sem valor para este
# worker. O valor é sempre específico de cada robô, nunca compartilhado na fila.
#
# Obrigatória x opcional é decisão DESTE script, não do painel: um toggle
# manual do "Obrig." no painel dura só até o próximo poll deste robô.
REQUIRED_ENV_KEYS = ["vivo_user", "vivo_senha", "email_user", "email_senha"]
OPTIONAL_ENV_KEYS = []  # usadas se preenchidas, mas não bloqueiam o robô

def extrair_credenciais(env_vars: dict) -> dict:
    """Normaliza as credenciais que vieram junto da tarefa.

    O painel é a única fonte: nada aqui cai para o .env. As credenciais são
    repassadas explicitamente para quem precisa delas (loginVivo).
    """
    env_vars = env_vars or {}
    return {
        chave: str(env_vars.get(chave) or env_vars.get(chave.upper()) or "").strip()
        for chave in REQUIRED_ENV_KEYS
    }

def get_local_ip():
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return 'Erro ao obter ip'

MACHINE_IP = get_local_ip()

# (conexão, leitura) em segundos. Leitura de 30s: se o painel demorar mais que o
# timeout para responder o get-next-task, a tarefa já foi separada para este robô
# mas nunca chega nele — um timeout curto demais transforma lentidão em tarefa presa.
HTTP_TIMEOUT = (5, 30)

# Sem tarefa por este tempo, o navegador é fechado para poupar recursos. A
# sessão do Siebel expiraria de qualquer jeito, e o login é refeito sozinho
# quando a próxima tarefa chegar. (O painel responde 204 SEM corpo tanto para
# "fila vazia" quanto para "fora do horário", então não dá para distinguir os
# dois pela resposta — o tempo ocioso cobre ambos.)
FECHAR_NAVEGADOR_OCIOSO_SEG = 15 * 60

SIEBEL_HOME_URL = os.getenv(
    "LOGIN_REDIRECT_URL",
    "https://vivovendas.vivo.com.br/sales_ext/start.swe?SWECmd=GotoView&SWEView=NV+Dealer+Home+Page+View&SWERF=1&SWEHo=vivovendas.vivo.com.br&SWEBU=1"
)
# A view de cobertura precisa estar ativa no servidor antes do NVSearchAddress
# funcionar (equivale a clicar no ícone "Validar Cobertura" na UI).
COVERAGE_VIEW_URL = "https://vivovendas.vivo.com.br/sales_ext/start.swe?SWECmd=GotoView&SWEView=NV+Check+Address+Coverage+Only+View+-+Dealer"

# Status devolvidos em `status_consulta`. Endereço não encontrado e CEP
# inválido são RESPOSTAS da consulta (success: true), não falhas do robô:
# reprocessar a tarefa daria sempre o mesmo resultado.
STATUS_DISPONIVEL = "DISPONIVEL"                    # tem tecnologia e porta livre
STATUS_SEM_DISPONIBILIDADE = "SEM_DISPONIBILIDADE"  # tem rede (ex: GPON), mas nenhuma porta livre
STATUS_SEM_COBERTURA = "SEM_COBERTURA"              # endereço existe, nenhuma tecnologia de acesso
STATUS_MULTIPLOS = "MULTIPLOS_ENDERECOS"            # CEP genérico: várias ruas, sem logradouro p/ escolher
STATUS_NAO_ENCONTRADO = "ENDERECO_NAO_ENCONTRADO"
STATUS_CEP_INVALIDO = "CEP_INVALIDO"


class SessaoSiebelPerdida(Exception):
    """A sessão logada caiu (redirecionou para o login ou o SiebelApp sumiu).
    O loop principal refaz o login e tenta a mesma tarefa mais uma vez."""


def handle_command(command, driver=None):
    """Executa comandos recebidos do painel de controle."""
    print(f"\n🎮 Comando recebido do painel: {command}")

    if command == "restart_worker":
        print("🔄 Reiniciando worker...")
        if driver:
            try: driver.quit()
            except: pass
        os.execv(sys.executable, [sys.executable] + sys.argv)

    elif command == "shutdown_pc":
        print("🔴 Desligando computador em 30 segundos...")
        if driver:
            try: driver.quit()
            except: pass
        if platform.system() == "Windows":
            subprocess.Popen(["shutdown", "/s", "/t", "30", "/c",
                "Worker Dashboard: desligamento remoto solicitado."])
        else:
            subprocess.Popen(["sudo", "shutdown", "-h", "+1",
                "Worker Dashboard: desligamento remoto solicitado."])
        sys.exit(0)

    elif command == "restart_pc":
        print("🟠 Reiniciando computador em 30 segundos...")
        if driver:
            try: driver.quit()
            except: pass
        if platform.system() == "Windows":
            subprocess.Popen(["shutdown", "/r", "/t", "30", "/c",
                "Worker Dashboard: reinício remoto solicitado."])
        else:
            subprocess.Popen(["sudo", "reboot"])
        sys.exit(0)

    elif command == "disable":
        # Servidor já marcou este worker como inativo no painel (fora do horário
        # de operação, desligamento automático) — só encerra o processo local.
        # O bootstrap.ps1 religa o script depois; enquanto o worker estiver
        # desativado no painel, a API responde 401 e ele só fica aguardando.
        print("⏹️ Worker desativado remotamente (fora do horário de operação). Encerrando processo...")
        if driver:
            try: driver.quit()
            except: pass
        sys.exit(0)


def send_result(task_id, post_data, headers):
    """
    Entrega o resultado ao painel, tentando de novo se a rede falhar.

    Sem isso, um timeout ou erro 5xx no complete-task perdia o resultado em
    silêncio e a tarefa ficava presa como "Em Processamento" para sempre (o
    robô continua ativo, então o painel nunca a devolve para a fila). Repetir
    é seguro: tarefa já finalizada responde 200 com "already_finished".
    """
    for attempt in range(1, 6):
        try:
            response = requests.post(
                f"{BASE_URL}/api/complete-task/{task_id}/",
                headers=headers,
                json=post_data,
                timeout=HTTP_TIMEOUT
            )
            if response.status_code == 200:
                return True
            if 400 <= response.status_code < 500 and response.status_code not in (408, 429):
                # 403: a tarefa já não é deste robô (voltou para a fila por
                # inatividade); 400: corpo inválido. Repetir não muda nada.
                print(f"\n❌ Resultado da tarefa #{task_id} recusado ({response.status_code}): {response.text[:300]}")
                return False
            print(f"\n⚠️ Painel respondeu {response.status_code} ao entregar #{task_id} (tentativa {attempt}/5)")
        except requests.exceptions.RequestException as e:
            print(f"\n⚠️ Falha ao entregar #{task_id} (tentativa {attempt}/5): {e}")
        time.sleep(5 * attempt)
    print(f"\n❌ Não foi possível entregar o resultado da tarefa #{task_id}.")
    return False


def verificar_erro_siebel(driver):
    """
    Verifica se o portal Siebel está exibindo a tela de erro de servidor ocupado, parâmetro inválido ou outro erro da aplicação.
    """
    if not driver:
        return False
    try:
        body_element = driver.find_element(By.TAG_NAME, "body")
        if body_element:
            body_text = body_element.text
            if "tentando acessar está ocupado" in body_text or "apresenta problemas" in body_text or "fazer logon novamente" in body_text:
                return True
            if "Detectamos um erro" in body_text or "Parâmetro inválido" in body_text or "SBL-" in body_text:
                return True
    except:
        pass
    return False


def abrir_sessao_siebel(credenciais):
    """
    Abre o Chrome, faz o login (captcha + OTP) e deixa o navegador numa view
    do Siebel. Retorna o driver pronto, ou None se não conseguiu (o loop
    principal tenta de novo na próxima tarefa).
    """
    print("\n[Navegador] Inicializando navegador...")
    start_browser()
    driver = connect_browser()
    _driver_ativo["driver"] = driver
    if not driver:
        print("  ✖ Falha ao iniciar o Chrome. Verifique se o Google Chrome está instalado/atualizado.")
        return None
    print("  ✔ Conectado ao navegador com sucesso!")

    try:
        current_url = driver.current_url
        if "simplifiquevivoemp.com.br" not in current_url and "SWEView" not in current_url:
            print("  [Auth] O navegador não está na área logada. Iniciando Auto-Login...")
            if not loginVivo(driver, credenciais):
                print("  ✖ Falha no Auto-Login. Fechando navegador e limpando sessão...")
                fechar_navegador(driver)
                return None
            print("  ✔ Auto-Login realizado com sucesso!")

        # Cai na tela do Siebel (onde o objeto JS SiebelApp existe). Retenta se
        # o portal responder com a tela de "servidor ocupado".
        if "SWEView" not in driver.current_url:
            for attempt in range(1, 4):
                print(f"  → Redirecionando para o Siebel (Tentativa {attempt}/3)...")
                driver.get(SIEBEL_HOME_URL)
                time.sleep(5.0)
                if not verificar_erro_siebel(driver):
                    break
                print(f"  ⚠ Erro 'Servidor Ocupado' detectado na tentativa {attempt}.")
                if attempt == 3:
                    print("  🚨 Erro do Siebel persistiu após todas as tentativas. Reiniciando navegador...")
                    fechar_navegador(driver)
                    return None
                time.sleep(10)
    except Exception as e:
        print(f"  ⚠ Erro ao fazer login ou redirecionar: {e}")
        fechar_navegador(driver)
        return None

    return driver


def fechar_navegador(driver):
    if driver:
        try: driver.quit()
        except: pass
    _driver_ativo["driver"] = None


COVERAGE_VIEW_NAME = "NV Check Address Coverage Only View - Dealer"


def _view_cobertura_ativa(driver, timeout=10.0):
    """
    Pergunta ao próprio Siebel qual view está ativa. A URL do navegador NÃO
    serve para isso: o Open UI é uma página única e, testado ao vivo, a barra
    de endereço seguia mostrando a Home enquanto a view ativa já era a de
    cobertura (e a busca funcionava normalmente).

    Sem `verificar_erro_siebel` aqui: ele procura textos como "SBL-" na página
    inteira, e a própria view de cobertura pode exibir uma mensagem assim (erro
    da consulta anterior) — dava falso "sessão perdida" e relogin à toa. Com o
    Siebel fora do ar/ocupado não existe view ativa, então esta checagem já
    cobre esse caso.

    Espera ATIVA (checa a cada 0,5 s, até `timeout`) em vez de um sleep fixo:
    segue assim que o Siebel termina de montar a view (normalmente < 1 s) e
    ainda tolera um Siebel lento sem dar falso "sessão perdida".
    """
    fim = time.time() + timeout
    ativa = ""
    while True:
        try:
            ativa = driver.execute_script(
                "try { return SiebelApp.S_App.GetActiveView().GetName(); } catch (e) { return ''; }"
            ) or ""
        except Exception:
            ativa = ""
        if ativa == COVERAGE_VIEW_NAME:
            return True
        if time.time() >= fim:
            print(f"  [Siebel] view ativa: '{ativa or '-'}'")
            return False
        time.sleep(0.5)


def abrir_view_cobertura(driver, passar_pela_home=False):
    """
    Deixa a view de cobertura ativa no servidor e devolve o cliente da API.

    O Siebel às vezes "reseta" o contexto e manda de volta para a Home
    (GotoView "NV Dealer Home Page View"): o GotoView da cobertura cai na
    Home, ou a busca volta sem dados. Isso NÃO é logout — passar pela Home e
    reabrir a cobertura resolve, sem refazer captcha + OTP. Só se ainda assim
    não abrir é que a sessão é dada como perdida (-> relogin).
    """
    if passar_pela_home:
        driver.get(SIEBEL_HOME_URL)
        time.sleep(4.0)
    driver.get(COVERAGE_VIEW_URL)
    if not _view_cobertura_ativa(driver):
        print("  ⚠ View de cobertura não ficou ativa no Siebel. Reabrindo pela Home...")
        driver.get(SIEBEL_HOME_URL)
        time.sleep(4.0)
        driver.get(COVERAGE_VIEW_URL)
        if not _view_cobertura_ativa(driver):
            raise SessaoSiebelPerdida("View de cobertura não ficou ativa no Siebel nem após passar pela Home")
    try:
        return SiebelAPIClient(driver)
    except ValueError as e:
        raise SessaoSiebelPerdida(str(e))


RETENCAO_RESPOSTAS_CRUAS_DIAS = 14  # mesma retenção dos logs do loguru


def _salvar_resposta_crua(nome, body):
    """
    Guarda a resposta SWE de um caso anômalo em logs/ para diagnóstico.
    Apaga as mais velhas que a retenção: a do loguru só cobre worker_*.log, e
    sem isto um robô rodando por meses acumularia arquivos sem limite.
    """
    try:
        limite = time.time() - RETENCAO_RESPOSTAS_CRUAS_DIAS * 86400
        for antigo in os.listdir(_LOG_DIR):
            caminho_antigo = os.path.join(_LOG_DIR, antigo)
            if antigo.endswith(".txt") and os.path.getmtime(caminho_antigo) < limite:
                os.remove(caminho_antigo)
    except Exception:
        pass
    try:
        seguro = re.sub(r'[^\w.-]', '_', nome)
        caminho = os.path.join(_LOG_DIR, f"{datetime.now():%Y%m%d_%H%M%S}_{seguro}.txt")
        with open(caminho, "w", encoding="utf-8") as f:
            f.write(body or "")
        print(f"  📝 Resposta crua salva em {caminho}")
    except Exception as e:
        print(f"  ⚠ Não foi possível salvar a resposta crua: {e}")


def _texto_comparavel(texto) -> str:
    """Maiúsculo, sem acento e sem pontuação, para casar nomes de rua."""
    texto = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r'[^A-Z0-9 ]', ' ', texto.upper()).split()


# Tipos de logradouro em qualquer grafia: "R", "RUA", "AV", "AVENIDA"... Fora
# da comparação porque o Siebel abrevia ("R QUINZE DE NOVEMBRO") e a planilha
# de entrada costuma vir por extenso ("Rua Quinze de Novembro").
_TIPOS_LOGRADOURO = {
    "R", "RUA", "AV", "AVENIDA", "AL", "ALAMEDA", "TV", "TRAV", "TRAVESSA", "PC", "PCA",
    "PRACA", "EST", "ESTRADA", "ROD", "RODOVIA", "VIA", "VL", "VILA", "LGO", "LARGO", "DE", "DA", "DO", "DAS", "DOS",
}


def _rua_e_cep(endereco):
    texto = endereco.get("Endereco") or ""
    cep = re.search(r'CEP\s*(\d{8})', texto)
    # "AV PAULISTA|EDI:MUSEU DE ARTE SAO PAULO" -> "AV PAULISTA": o sufixo após
    # "|" é o nome do edifício; duas linhas do mesmo prédio só diferem nele.
    rua = texto.split(",")[0].split("|")[0]
    return " ".join(_texto_comparavel(rua)), (cep.group(1) if cep else "")


def escolher_endereco(enderecos, logradouro, cep):
    """
    Índice da linha a consultar quando a busca devolve mais de uma, ou None
    se não der para decidir com segurança.

    CEP genérico de cidade pequena devolve várias ruas para o mesmo número
    (ex: 13525000 nº 100 -> 9 endereços); sem o nome da rua não há como saber
    qual é o do cliente, e consultar a 1ª linha às cegas devolveria a
    cobertura de outro endereço. Ordem de desempate:
      1. logradouro do payload (se veio) filtra as ruas que casam;
      2. entre as que sobraram, a de CEP idêntico ao pedido;
      3. se todas as que sobraram são a MESMA rua no MESMO CEP, a primeira.
    """
    candidatos = list(range(len(enderecos)))
    alvo = [p for p in _texto_comparavel(logradouro) if p not in _TIPOS_LOGRADOURO]
    if alvo:
        candidatos = [i for i in candidatos
                      if all(p in _rua_e_cep(enderecos[i])[0].split() for p in alvo)]
    if len(candidatos) == 1:
        return candidatos[0]
    if not candidatos:
        return None

    mesmo_cep = [i for i in candidatos if _rua_e_cep(enderecos[i])[1] == cep]
    if len(mesmo_cep) == 1:
        return mesmo_cep[0]

    restantes = mesmo_cep or candidatos
    if len({_rua_e_cep(enderecos[i]) for i in restantes}) == 1:
        return restantes[0]
    return None


def normalizar_cep(valor) -> str:
    """Só dígitos; CEP com 7 dígitos ganha o zero à esquerda que o Excel come."""
    digitos = re.sub(r'\D', '', str(valor or ""))
    if len(digitos) == 7:
        digitos = digitos.zfill(8)
    return digitos


def normalizar_numero(valor) -> str:
    """'1180.0' (número lido de planilha) vira '1180'; o resto só é aparado."""
    if isinstance(valor, float) and valor.is_integer():
        return str(int(valor))
    texto = str(valor if valor is not None else "").strip()
    if re.fullmatch(r'\d+\.0+', texto):
        texto = texto.split('.')[0]
    return texto


def _consultar_cobertura(driver, payload):
    """
    Consulta a cobertura de UM endereço (CEP + número) no Siebel.

    Recebe:
      - payload (dict): {"cep": "13419230", "numero": "1180", "logradouro": ""}
                        (só cep e numero são obrigatórios; logradouro desempata
                        CEP genérico que devolve várias ruas)
      - config (dict): `default_config` da fila (não usado hoje)
      - env_vars (dict): credenciais deste worker cadastradas no painel
    Retorna:
      - dict (1 para 1) sempre com as MESMAS chaves, seja qual for o status,
        para as colunas OUT_ do painel e do CSV ficarem alinhadas.

    Endereço não encontrado / CEP inválido NÃO levantam exceção: são a
    resposta da consulta. Só erro de verdade (sessão, rede, Siebel fora)
    vira exceção -> tarefa FAILED.
    """
    cep = normalizar_cep(payload.get("cep") or payload.get("CEP"))
    numero = normalizar_numero(payload.get("numero") or payload.get("Numero") or payload.get("numero_imovel"))
    # cidade/estado do payload NÃO vão para o Siebel: o padrão é só CEP +
    # número, e na validação um caso com cidade preenchida voltou sem a busca
    # executada. O CEP já determina a cidade.
    # Opcional: só é usado para escolher a rua quando o CEP devolve várias.
    logradouro = str(payload.get("logradouro") or payload.get("rua") or payload.get("Logradouro") or "").strip()

    if not numero:
        raise ValueError("Payload da tarefa inválido: número do endereço não informado.")

    resultado = {
        "cep": cep,
        "numero": numero,
        "status_consulta": "",
        "endereco_encontrado": False,
        "disponivel": False,
        "is_gpon": False,
        "tecnologia_acesso": "",
        "velocidade_maxima": "",
        "portas_disponiveis": 0,
        "faixas_velocidade": "",
        "linhas_telefonicas": 0,
        "cobertura_movel": "",
        "tecnologia_tv": "",
        "mensagem": "",
        "endereco": "",
        "cep_retornado": "",
        "cep_divergente": False,
        "bairro": "",
        "rede": "",
        "tipo_cidade": "",
        "intervalo": "",
        "tipo_armario": "",
        "velocidade_maxima_armario": "",
        "caixa": "",
        "central_primaria": "",
        "area_telefonica": "",
        "estacao": "",
        "enderecos_encontrados": 0,
        "enderecos_candidatos": "",
        "detalhes_cobertura": "",
        "consultado_em": datetime.now().isoformat(timespec="seconds"),
    }

    # Mesma regra do próprio Siebel ("O número deve ter apenas 8 dígitos
    # numéricos"), checada antes para não gastar uma ida ao portal.
    if len(cep) != 8:
        resultado["status_consulta"] = STATUS_CEP_INVALIDO
        resultado["mensagem"] = f"CEP inválido: '{payload.get('cep') or payload.get('CEP') or ''}' (precisa ter 8 dígitos)."
        print(f"  ⚠ {resultado['mensagem']}")
        return resultado

    print(f"\n📡 Verificando cobertura: CEP={cep} Nº={numero}")

    api = abrir_view_cobertura(driver)

    busca = api.search_coverage(cep, numero)
    if not busca["enderecos"] and not busca.get("erro"):
        # Status Completed sem nenhuma linha e sem ErrMsg: não é a resposta
        # normal de "não encontrado" (essa vem com erro). Já aconteceu com um
        # endereço que existe e que na busca seguinte voltou normal — então
        # reabre a view e refaz uma vez antes de concluir qualquer coisa.
        _salvar_resposta_crua(f"busca_vazia_{cep}_{numero}", api.ultima_resposta)
        print("  ⚠ Busca voltou vazia sem mensagem de erro. Refazendo a consulta uma vez...")
        time.sleep(2.0)
        api = abrir_view_cobertura(driver, passar_pela_home=True)
        busca = api.search_coverage(cep, numero)
    enderecos = busca["enderecos"]
    resultado["enderecos_encontrados"] = len(enderecos)

    if busca.get("erro"):
        erro = busca["erro"]
        if "não encontrado" in erro.lower() or "nao encontrado" in erro.lower():
            resultado["status_consulta"] = STATUS_NAO_ENCONTRADO
        elif "cep" in erro.lower() and "inválido" in erro.lower():
            resultado["status_consulta"] = STATUS_CEP_INVALIDO
        else:
            raise RuntimeError(f"Erro retornado pelo Siebel na busca do endereço: {erro}")
        resultado["mensagem"] = erro
        print(f"  ⚠ {erro}")
        return resultado

    if not enderecos:
        # Sem linha E sem ErrMsg, duas vezes: o Siebel não executou a busca
        # (resposta só com estado de tela). NÃO é "endereço não existe" — esse
        # veredito só vale quando o próprio Siebel diz "Endereço não encontrado".
        # Falha a tarefa (reprocessável) em vez de gravar uma resposta falsa.
        _salvar_resposta_crua(f"busca_vazia_{cep}_{numero}_retry", api.ultima_resposta)
        raise RuntimeError("Siebel não executou a busca (resposta sem endereços e sem mensagem de erro, 2 tentativas).")

    if len(enderecos) > 1:
        resultado["enderecos_candidatos"] = " || ".join(
            f"{e.get('Endereco', '')} [{e.get('Tecnologia Acesso') or 'sem tecnologia'}]" for e in enderecos
        )
        indice = escolher_endereco(enderecos, logradouro, cep)
        if indice is None:
            tecnologias = sorted({e.get("Tecnologia Acesso") for e in enderecos if e.get("Tecnologia Acesso")})
            resultado.update({
                "status_consulta": STATUS_MULTIPLOS,
                "endereco_encontrado": True,
                "tecnologia_acesso": ", ".join(tecnologias),
                "mensagem": (f"{len(enderecos)} endereços para este CEP e número. Informe 'logradouro' "
                             f"na tarefa para consultar a cobertura do endereço certo."
                             + (f" Logradouro '{logradouro}' não casou com um único candidato." if logradouro else "")),
            })
            print(f"  ⚠ {resultado['mensagem']}")
            return resultado
    else:
        indice = 0

    # O NVCheckCoverage consulta a linha passada em SWERowId (VRId-<n>).
    selecionado = enderecos[indice]
    detalhes = api.check_coverage_details(selecionado.get("Id") or f"VRId-{indice}")
    notas = detalhes["notas"]
    fields = detalhes["fields"]

    endereco_txt = selecionado.get("Endereco", "")
    cep_match = re.search(r'CEP\s*(\d{8})', endereco_txt)
    cep_retornado = cep_match.group(1) if cep_match else ""

    resultado.update({
        "endereco_encontrado": True,
        "disponivel": notas["disponivel"],
        "is_gpon": detalhes["is_gpon"],
        "tecnologia_acesso": fields.get("GVT Access Technology Calc") or selecionado.get("Tecnologia Acesso", ""),
        "velocidade_maxima": notas["velocidade_maxima"],
        "portas_disponiveis": notas["portas_disponiveis"],
        "faixas_velocidade": notas["faixas_velocidade"],
        "linhas_telefonicas": notas["linhas_telefonicas"],
        "cobertura_movel": fields.get("NV Mobile Coverage Flag", ""),
        "tecnologia_tv": notas["tecnologia_tv"],
        "tipo_cidade": notas["tipo_cidade"],
        "tipo_armario": notas["tipo_armario"],
        "velocidade_maxima_armario": notas["velocidade_maxima_armario"],
        "caixa": notas["caixa"],
        "central_primaria": notas["central_primaria"],
        "area_telefonica": fields.get("telephonicArea") or selecionado.get("AT", ""),
        "estacao": fields.get("microArea") or selecionado.get("ES", ""),
        # Texto integral das notas do Siebel, uma informação por linha, para
        # conferência humana de qualquer detalhe que não virou coluna própria.
        "detalhes_cobertura": "\n".join(
            l for l in (x.replace("\xa0", " ").strip(" -;\t")
                        for x in re.split(r'\r?\n', fields.get("GVT Coverage Notes", "")))
            if l
        ),
        "mensagem": f"{notas['codigo_mensagem']} {notas['mensagem']}".strip(),
        "endereco": endereco_txt,
        "cep_retornado": cep_retornado,
        # O Siebel casa o número pela faixa do logradouro: um número que não
        # existe no CEP informado pode voltar num CEP vizinho da mesma rua.
        "cep_divergente": bool(cep_retornado) and cep_retornado != cep,
        "bairro": selecionado.get("Bairro", ""),
        "rede": fields.get("donoRede") or selecionado.get("Rede", ""),
        "intervalo": selecionado.get("Intervalo", ""),
    })
    if notas["disponivel"]:
        resultado["status_consulta"] = STATUS_DISPONIVEL
    elif resultado["tecnologia_acesso"]:
        resultado["status_consulta"] = STATUS_SEM_DISPONIBILIDADE
    else:
        resultado["status_consulta"] = STATUS_SEM_COBERTURA

    print(f"  ✔ {resultado['status_consulta']} | {resultado['tecnologia_acesso']} | "
          f"{resultado['velocidade_maxima'] or '-'} | portas={resultado['portas_disponiveis']}"
          + (f" | CEP retornado {cep_retornado} ≠ {cep}" if resultado["cep_divergente"] else ""))
    return resultado


# --- Camada "leiga" do resultado ---
# Os campos técnicos (status_consulta, tecnologia_acesso, GPON, I2402...) são o
# contrato estável para filtros e para a tela; estes três são o que um vendedor
# ou cliente entende sem conhecer a rede: rótulo curto, frase explicativa e a
# tecnologia pelo nome comercial.
SITUACAO_LEIGA = {
    STATUS_DISPONIVEL: "Disponível",
    STATUS_SEM_DISPONIBILIDADE: "Sem vaga no momento",
    STATUS_SEM_COBERTURA: "Sem cobertura",
    STATUS_MULTIPLOS: "Várias ruas neste CEP",
    STATUS_NAO_ENCONTRADO: "Endereço não encontrado",
    STATUS_CEP_INVALIDO: "CEP inválido",
}

TECNOLOGIA_LEIGA = {
    "GPON": "Fibra óptica",
    "METALICO": "Cabo de cobre",
    "FTTC": "Fibra até o armário + cabo de cobre",
    "HFC": "Cabo coaxial",
}


def velocidade_leiga(velocidade):
    """'1 Gbps' -> '1 Giga', '500 Mbps' -> '500 Mega' (como o cliente fala)."""
    m = re.match(r'\s*([\d.,]+)\s*([GMK])bps', velocidade or "", re.IGNORECASE)
    if not m:
        return velocidade or ""
    unidade = {"G": "Giga", "M": "Mega", "K": "Kbps"}[m.group(2).upper()]
    return f"{m.group(1)} {unidade}"


def descrever_tecnologia(codigo):
    if not codigo:
        return ""
    partes = [p.strip() for p in codigo.split(",") if p.strip()]
    return ", ".join(TECNOLOGIA_LEIGA.get(p.upper(), p.title()) for p in partes)


def resumo_leigo(r):
    """Uma frase que explica o resultado sem jargão de rede."""
    status = r["status_consulta"]
    tec = descrever_tecnologia(r["tecnologia_acesso"]).lower() or "de internet"
    vel = velocidade_leiga(r["velocidade_maxima"])

    if status == STATUS_DISPONIVEL:
        frase = f"Tem {tec} disponível para instalação" + (f", com velocidade de até {vel}." if vel else ".")
    elif status == STATUS_SEM_DISPONIBILIDADE:
        frase = (f"A rede de {tec} chega a este endereço, mas no momento não há vaga para "
                 f"uma nova instalação. Vale consultar de novo mais tarde.")
    elif status == STATUS_SEM_COBERTURA:
        frase = "A Vivo não tem rede de internet fixa neste endereço."
    elif status == STATUS_MULTIPLOS:
        frase = (f"Este CEP é compartilhado por {r['enderecos_encontrados']} ruas com esse número. "
                 f"Informe o nome da rua para ver a cobertura do endereço certo.")
    elif status == STATUS_NAO_ENCONTRADO:
        frase = "Endereço não encontrado na base da Vivo. Confira se o CEP e o número estão corretos."
    elif status == STATUS_CEP_INVALIDO:
        frase = "CEP inválido: ele precisa ter 8 números."
    else:
        frase = ""

    if r.get("cep_divergente") and r.get("cep_retornado"):
        frase += (f" Atenção: a Vivo localizou este número no CEP {r['cep_retornado'][:5]}-{r['cep_retornado'][5:]}, "
                  f"diferente do informado.")
    return frase.strip()


def process_task(driver, payload, config, env_vars):
    """
    Consulta a cobertura de UM endereço e devolve o resultado técnico mais a
    camada leiga (`situacao`, `resumo`, `tecnologia_descricao`), sempre com as
    mesmas chaves. Ver `_consultar_cobertura` para a consulta em si.
    """
    resultado = _consultar_cobertura(driver, payload)
    resultado["situacao"] = SITUACAO_LEIGA.get(resultado["status_consulta"], resultado["status_consulta"])
    resultado["tecnologia_descricao"] = descrever_tecnologia(resultado["tecnologia_acesso"])
    resultado["velocidade_maxima"] = velocidade_leiga(resultado["velocidade_maxima"])
    resultado["resumo"] = resumo_leigo(resultado)
    return resultado


def run_worker():
    print(f"🚀 Iniciando Worker [{WORKER_NAME}] na fila [{QUEUE_SLUG}]...")
    print(f"💻 Máquina: {MACHINE_NAME} ({MACHINE_IP})")
    print(f"🔗 Conectando ao Dashboard em: {BASE_URL}")

    if not QUEUE_TOKEN:
        print("❌ QUEUE_TOKEN vazio no .env. Copie o Token Mestre da fila (aba 'API & Integração') para o .env.")
        time.sleep(60)
        sys.exit(1)

    print("🧹 Verificando navegadores órfãos de uma execução anterior...")
    try:
        limpar_navegadores_orfaos()
    except Exception as e:
        print(f"  ⚠ Falha ao limpar navegadores órfãos: {e}")

    headers = {
        "X-Queue-Token": QUEUE_TOKEN,
        "X-Worker-Name": WORKER_NAME,
        "X-Machine-Name": MACHINE_NAME,
        "X-Machine-IP": MACHINE_IP,
        # Declara ao painel quais chaves este robô precisa. Enviar este header
        # é o que ativa a sincronização/validação de variáveis no servidor.
        "X-Worker-Vars-Keys": ",".join(REQUIRED_ENV_KEYS + OPTIONAL_ENV_KEYS),
        # Dentre as chaves acima, quais são opcionais — o resto é obrigatório.
        # Enviar isto (mesmo vazio) faz o painel obedecer estas duas listas.
        "X-Worker-Vars-Optional-Keys": ",".join(OPTIONAL_ENV_KEYS),
    }

    driver = None
    credenciais = {}
    ultima_tarefa = time.time()

    while True:
        try:
            # 1. Buscar Tarefa
            response = requests.get(
                f"{BASE_URL}/api/get-next-task/{QUEUE_SLUG}/",
                headers=headers, timeout=HTTP_TIMEOUT
            )

            # Verificar comandos remotos em QUALQUER resposta
            try:
                resp_data = response.json()
                if resp_data.get("command"):
                    handle_command(resp_data["command"], driver)
                    continue
            except (json.JSONDecodeError, ValueError):
                pass

            if response.status_code == 204:
                # Sem tarefas (ou fora do horário / em pausa), espera um pouco
                if driver and time.time() - ultima_tarefa > FECHAR_NAVEGADOR_OCIOSO_SEG:
                    print("\n💤 Sem tarefas há um tempo. Fechando navegador para poupar recursos...")
                    fechar_navegador(driver)
                    driver = None
                print(".", end="", flush=True)
                time.sleep(5)
                continue

            if response.status_code == 400:
                # Alguma chave de REQUIRED_ENV_KEYS está sem valor cadastrado no
                # painel para ESTE worker. Preencha na tela de edição do worker;
                # o robô fica tentando até estar configurado.
                try:
                    faltando = response.json().get("missing_vars", response.text)
                except (json.JSONDecodeError, ValueError):
                    faltando = response.text
                print(f"\n⚠️ Configuração pendente no painel deste worker: {faltando}")
                fechar_navegador(driver)
                driver = None
                time.sleep(15)
                continue

            if response.status_code != 200:
                print(f"\n❌ Erro API ({response.status_code}): {response.text[:300]}")
                time.sleep(10)
                continue

            task = response.json()
            task_id = task['id']
            payload = task['payload'] or {}
            config = task.get('config', {})
            env_vars = task.get('vars', {})
            ultima_tarefa = time.time()

            print(f"\n⚡ Tarefa #{task_id} recebida!")

            # Credenciais deste worker chegam junto da tarefa (única fonte). Se
            # mudaram no painel, a sessão aberta está logada com a credencial
            # antiga e precisa ser derrubada para refazer o login.
            novas_credenciais = extrair_credenciais(env_vars)
            if credenciais and novas_credenciais != credenciais and driver:
                print("\n🔑 Credenciais do painel mudaram. Fechando navegador para refazer o login...")
                fechar_navegador(driver)
                driver = None
            credenciais = novas_credenciais

            # 2. Processar (com uma nova tentativa se a sessão do Siebel caiu)
            result, success, error_msg = {}, False, None
            for tentativa in (1, 2, 3):
                try:
                    if not driver:
                        driver = abrir_sessao_siebel(credenciais)
                        if not driver:
                            # Já aconteceu de a janela nova fechar logo após abrir
                            # ("target window already closed"): tenta de novo em vez
                            # de reprovar a tarefa na primeira falha de navegador.
                            raise SessaoSiebelPerdida("Não foi possível abrir a sessão logada no Siebel (navegador/login).")
                    result = process_task(driver, payload, config, env_vars)
                    success, error_msg = True, None
                    break
                except SessaoSiebelPerdida as e:
                    print(f"  ⚠ Sessão do Siebel perdida ({e}). Refazendo login (tentativa {tentativa}/3)...")
                    fechar_navegador(driver)
                    driver = None
                    success, result, error_msg = False, {}, str(traceback.format_exc())
                    time.sleep(5)  # dá tempo do Chrome anterior liberar o perfil
                except Exception as e:
                    success, result, error_msg = False, {}, str(traceback.format_exc())
                    print(f"❌ Erro no processamento: {e}")
                    # Navegador morto não se recupera sozinho: derruba para a
                    # próxima tarefa abrir um novo.
                    try:
                        if driver:
                            driver.execute_script("return 1")
                    except Exception:
                        fechar_navegador(driver)
                        driver = None
                    break

            # 3. Enviar Resultado
            post_data = {
                "success": success,
                "result": result,
                "error_message": error_msg
            }

            if send_result(task_id, post_data, headers):
                print(f"✅ Tarefa #{task_id} finalizada!" if success else f"⚠️ Tarefa #{task_id} reportada com erro.")

        except requests.exceptions.ConnectionError:
            print("\n⚠️ Falha na conexão com o Dashboard. Tentando novamente em 10s...")
            time.sleep(10)
        except requests.exceptions.Timeout:
            print("\n⚠️ Painel demorou a responder. Tentando novamente em 10s...")
            time.sleep(10)
        except Exception as e:
            print(f"\n❌ Erro fatal no loop: {e}")
            time.sleep(5)

if __name__ == "__main__":
    # Inicialização automática: fica a cargo do setup_startup.ps1 (tarefa
    # agendada "VivoSmartCoberturaWorker" -> bootstrap.ps1 -> este script),
    # não do setup_autostart() do script genérico do painel.
    run_worker()
