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
from datetime import datetime
from dotenv import load_dotenv

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import StaleElementReferenceException, ElementClickInterceptedException
from loguru import logger

# Carrega configurações locais do .env
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

# --- CONFIGURAÇÃO AUTOMÁTICA & DASHBOARD ---
BASE_URL = os.getenv("API_BASE_URL", "http://192.168.100.2/workers").rstrip('/')
QUEUE_SLUG = os.getenv("QUEUE_SLUG", "vivosimplifique1")
QUEUE_TOKEN = os.getenv("QUEUE_TOKEN", "78d43d99-874c-49ee-b18f-5c0e5a766d76")
WORKER_NAME = os.getenv("WORKER_NAME", "Worker-Python-01")
MACHINE_NAME = socket.gethostname()

# --- VARIÁVEIS DE AMBIENTE DO ROBÔ (cadastradas no painel, por worker) ---
# Chaves que ESTE robô precisa para funcionar. O painel descobre a lista pelo
# header X-Worker-Vars-Keys a cada chamada e segura a entrega de tarefas
# (HTTP 400) enquanto alguma delas estiver sem valor para este worker.
# O valor é sempre específico de cada robô, nunca compartilhado na fila.
REQUIRED_ENV_KEYS = ["vivo_user", "vivo_senha", "email_user", "email_senha"]

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

def extrair_megas(texto: str) -> str:
    """
    Extrai a quantidade de megas/velocidade a partir de um texto (nome do produto, promoção ou tecnologia).
    Ex: 'Vivo Fibra 500 Mega' -> '500 Mega'
        'Banda Larga 1 Giga' -> '1000 Mega'
        'Plano 300MB' -> '300 Mega'
    """
    if not texto:
        return ""
    
    # 1. Procura por padrões de Giga (ex: 1 Giga, 1GB, 1 Gbps, 2.5 Giga)
    giga_match = re.search(r'(\d+(?:[\.,]\d+)?)\s*(?:giga|gigas|gbps|gb)\b', texto, re.IGNORECASE)
    if giga_match:
        val_str = giga_match.group(1).replace(',', '.')
        try:
            val = float(val_str)
            if val < 10:
                return f"{int(val * 1000)} Mega"
            return f"{int(val)} Mega"
        except ValueError:
            pass

    # 2. Procura por padrões de Mega/MB/Mbps (ex: 500 Mega, 500MB, 500Mbps)
    mega_match = re.search(r'(\d+)\s*(?:mega|megas|mbps|mb)\b', texto, re.IGNORECASE)
    if mega_match:
        return f"{mega_match.group(1)} Mega"

    # 3. Procura por M isolado (ex: 500M, 600 M)
    m_match = re.search(r'(\d+)\s*m\b', texto, re.IGNORECASE)
    if m_match:
        return f"{m_match.group(1)} Mega"

    return ""

def format_result(cnpj: str, company_name: str, company_address: str, account_id: str, product_records: list) -> list:
    """
    Formata o resultado no schema plano consolidado esperado pelo Dashboard centralizador,
    incluindo nome_plano e megas.
    """
    extraido_em = datetime.now().isoformat()
    
    # Se a empresa não tiver produtos ativos, retorna uma linha com os dados da empresa e colunas de produtos vazias
    if not product_records:
        return [{
            "cnpj": cnpj,
            "nome": company_name,
            "endereco_completo": company_address,
            "id_conta": account_id,
            "extraido_em": extraido_em,
            "id": None,
            "id_integracao": None,
            "nome_produto": None,
            "status": None,
            "data_criacao": None,
            "data_instalacao": None,
            "numero_serie": None,
            "quantidade": None,
            "nome_promocao": None,
            "id_pai": None,
            "nivel_hierarquia": None,
            "fidelizacao": None,
            "tecnologia_acesso": None,
            "tecnologia_voz": None,
            "endereco_servico": None,
            "nome_plano": None,
            "megas": None
        }]
        
    products_by_id = {str(p.get("Id")): p for p in product_records if p.get("Id")}

    def obter_nome_plano(r):
        # 1. Tenta nome da promoção do próprio registro
        prom = r.get("Prod Prom Name")
        if prom and str(prom).strip():
            return str(prom).strip()

        # 2. Procura nos ancestrais
        curr = r
        visited = set()
        while curr:
            pid = str(curr.get("Parent Id") or curr.get("GVT Parent Hierarchy Item Id") or "")
            if not pid or pid == "-1" or pid in visited:
                break
            visited.add(pid)
            curr = products_by_id.get(pid)
            if curr and curr.get("Prod Prom Name") and str(curr.get("Prod Prom Name")).strip():
                return str(curr.get("Prod Prom Name")).strip()

        # 3. Fallback para nome do produto
        return r.get("GVT Product Name Calc", "") or ""

    def obter_megas(r):
        # 1. Tenta no próprio produto
        for campo in ["GVT Product Name Calc", "Prod Prom Name", "GVT Display Access Technology"]:
            val = extrair_megas(r.get(campo, ""))
            if val:
                return val

        # 2. Tenta nos filhos se for produto pai
        prod_id = str(r.get("Id", ""))
        if prod_id:
            for child in product_records:
                c_parent = str(child.get("Parent Id") or child.get("GVT Parent Hierarchy Item Id") or "")
                if c_parent == prod_id:
                    for campo in ["GVT Product Name Calc", "Prod Prom Name", "GVT Display Access Technology"]:
                        val = extrair_megas(child.get(campo, ""))
                        if val:
                            return val

        # 3. Tenta no pai se for produto filho
        parent_id = str(r.get("Parent Id") or r.get("GVT Parent Hierarchy Item Id") or "")
        if parent_id and parent_id != "-1" and parent_id in products_by_id:
            p_prod = products_by_id[parent_id]
            for campo in ["GVT Product Name Calc", "Prod Prom Name", "GVT Display Access Technology"]:
                val = extrair_megas(p_prod.get(campo, ""))
                if val:
                    return val

        return ""

    formatted_list = []
    for r in product_records:
        parent_id = str(r.get("Parent Id", "")) if r.get("Parent Id") != -1 else ""
        level = r.get("Hierarchy Level", 0)
        
        nome_plano = obter_nome_plano(r)
        megas = obter_megas(r)
        
        formatted_list.append({
            "cnpj": cnpj,
            "nome": company_name,
            "endereco_completo": company_address,
            "id_conta": account_id,
            "extraido_em": extraido_em,
            "id": r.get("Id", ""),
            "id_integracao": r.get("Integration Id", ""),
            "nome_produto": r.get("GVT Product Name Calc", ""),
            "status": r.get("GVT Status", ""),
            "data_criacao": r.get("Created", ""),
            "data_instalacao": r.get("GVT Install Date", ""),
            "numero_serie": r.get("Serial Number", ""),
            "quantidade": r.get("Quantity", ""),
            "nome_promocao": r.get("Prod Prom Name", ""),
            "id_pai": parent_id,
            "nivel_hierarquia": int(level) if level is not None else 0,
            "fidelizacao": r.get("GVT Commitment Calc", ""),
            "tecnologia_acesso": r.get("GVT Display Access Technology", ""),
            "tecnologia_voz": r.get("GVT Display Voice Technology", ""),
            "endereco_servico": r.get("GVT Service Address", ""),
            "nome_plano": nome_plano,
            "megas": megas
        })
    return formatted_list

def process_task(driver, payload: dict, config: dict = None) -> list:
    """
    Recebe o payload da tarefa (CNPJ) e faz a extração de dados no Siebel.
    Retorna uma lista de registros consolidados.
    """
    # Recupera o CNPJ do payload da forma mais flexível possível
    cnpj = payload.get("cnpj") or payload.get("CNPJ")
    if not cnpj and isinstance(payload, str):
        cnpj = payload
        
    if not cnpj:
        raise ValueError("Payload da tarefa inválido: CNPJ não encontrado.")
        
    # Limpa formatação e preenche zeros à esquerda
    cnpj = ''.join(filter(str.isdigit, str(cnpj))).zfill(14)
    cnpj_fmt = f"{cnpj[:2]}.{cnpj[2:5]}.{cnpj[5:8]}/{cnpj[8:12]}-{cnpj[12:]}"
    print(f"\n🔍 Processando CNPJ: {cnpj_fmt}")
    
    # Inicializa cliente da API Siebel
    api = SiebelAPIClient(driver)
    
    # Busca empresa no Siebel
    result = api.search_cnpj(cnpj)
    
    if result.get("error"):
        raise RuntimeError(f"Erro retornado pela API Siebel: {result['error']}")
        
    empresa = result.get("empresa")
    if not empresa:
        print("  ⚠ Cliente não encontrado no Siebel. Retornando pesquisa como completa sem produtos.")
        return format_result(cnpj, "N/A", "N/A", "N/A", [])
        
    name = empresa.get("name", "")
    address = empresa.get("address", "")
    acc_id = empresa.get("id", "")
    
    print(f"  ✔ Empresa: {name}")
    
    # Expande árvore de produtos
    produtos_raiz = result.get("produtos", [])
    if produtos_raiz:
        print(f"  → Expandindo {len(produtos_raiz)} produtos raiz...")
        all_products = api.expand_all_recursive(acc_id, produtos_raiz)
        print(f"  ✔ {len(all_products)} produto(s) extraído(s) com sucesso.")
    else:
        all_products = []
        print("  ⚠ Nenhum produto ativo encontrado.")
        
    # Formata e retorna no padrão unificado
    return format_result(cnpj, name, address, acc_id, all_products)

def process_coverage_task(driver, payload: dict) -> dict:
    """
    Recebe o payload da tarefa (CNPJ, CEP e número) e verifica se o endereço
    tem cobertura GPON no Siebel. Retorna um dict simples com o resultado,
    sem o schema completo de produtos usado em process_task.
    """
    cnpj = payload.get("cnpj") or payload.get("CNPJ") or ""
    cep = payload.get("cep") or payload.get("CEP")
    numero = payload.get("numero") or payload.get("Numero") or payload.get("numero_imovel")
    cidade = payload.get("cidade") or payload.get("Cidade") or ""
    estado = payload.get("estado") or payload.get("Estado") or ""

    if not cep or not numero:
        raise ValueError("Payload da tarefa inválido: CEP e/ou número não encontrados.")

    print(f"\n📡 Verificando cobertura GPON: CEP={cep} Nº={numero}")

    # Garante que a view de cobertura está ativa no servidor antes do
    # NVSearchAddress funcionar (equivale a clicar no ícone "Validar Cobertura").
    coverage_view_url = "https://vivovendas.vivo.com.br/sales_ext/start.swe?SWECmd=GotoView&SWEView=NV+Check+Address+Coverage+Only+View+-+Dealer"
    driver.get(coverage_view_url)
    time.sleep(3.0)

    api = SiebelAPIClient(driver)
    api.search_coverage(cep, numero, cidade, estado)
    detalhes = api.check_coverage_details()

    print(f"  ✔ GPON: {detalhes['is_gpon']}")

    return {
        "cnpj": cnpj,
        "cep": cep,
        "numero": numero,
        "is_gpon": detalhes["is_gpon"],
    }

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

def wait_for_siebel_ready(driver, timeout=30):
    """
    Aguarda até que o portal Siebel não esteja mais ocupado (classe 'siebui-busy' no HTML)
    e que nenhum elemento 'loader' esteja visível na tela.
    """
    if not driver:
        return
    try:
        start_time = time.time()
        while time.time() - start_time < timeout:
            html_el = driver.find_element(By.TAG_NAME, "html")
            classes = html_el.get_attribute("class") or ""
            
            # Verifica se existem loaders visíveis
            loaders = driver.find_elements(By.CLASS_NAME, "loader")
            loader_visible = any(l.is_displayed() for l in loaders)
            
            if "siebui-busy" not in classes and not loader_visible:
                time.sleep(1.0)
                break
            time.sleep(0.5)
    except:
        pass

def run_worker():
    print(f"🚀 Iniciando Worker [{WORKER_NAME}] na fila [{QUEUE_SLUG}]...")
    print(f"💻 Máquina: {MACHINE_NAME} ({MACHINE_IP})")
    print(f"🔗 Conectando ao Dashboard em: {BASE_URL}")

    print("🧹 Verificando navegadores órfãos de uma execução anterior...")
    try:
        limpar_navegadores_orfaos()
    except Exception as e:
        print(f"  ⚠ Falha ao limpar navegadores órfãos: {e}")

    print("💤 Robô em modo passivo. Pingando a API aguardando horário/tarefa...")

    headers = {
        "X-Queue-Token": QUEUE_TOKEN,
        "X-Worker-Name": WORKER_NAME,
        "X-Machine-Name": MACHINE_NAME,
        "X-Machine-IP": MACHINE_IP,
        # Declara ao painel quais chaves este robô precisa. Enviar este header
        # é o que ativa a sincronização/validação de variáveis no servidor.
        "X-Worker-Vars-Keys": ",".join(REQUIRED_ENV_KEYS)
    }
    
    driver = None
    first_error_time = None
    credenciais = {}

    while True:
        try:
            # 1. Buscar Tarefa na fila do Dashboard (Modo Passivo)
            response = requests.get(
                f"{BASE_URL}/api/get-next-task/{QUEUE_SLUG}/", 
                headers=headers, timeout=10
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
                # Pode ser fila vazia ou fora de horário.
                fora_do_horario = False
                deve_desligar_pc = False
                try:
                    resp_data = response.json()
                    msg = resp_data.get("message", "").lower()
                    if "fora" in msg and "hor" in msg:
                        fora_do_horario = True
                    if resp_data.get("shutdown") is True:
                        deve_desligar_pc = True
                except Exception:
                    pass

                if fora_do_horario and driver:
                    print("\n⏰ [Agendamento] Fora do horário de operação. Fechando navegador para poupar recursos...")
                    try: driver.quit()
                    except: pass
                    driver = None

                if deve_desligar_pc:
                    print("\n🔴 [SHUTDOWN] Dashboard solicitou desligamento do PC!")
                    handle_command("shutdown_pc", driver)
                    return  # Encerra o loop do worker

                first_error_time = None
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
                if driver:
                    try: driver.quit()
                    except: pass
                    driver = None
                time.sleep(15)
                continue

            if response.status_code != 200:
                print(f"\n❌ Erro API ({response.status_code}) - {response.text}")
                if driver:
                    print("[Navegador] Fechando navegador devido a status de API não-ok...")
                    try: driver.quit()
                    except: pass
                    driver = None
                time.sleep(10)
                continue

            task = response.json()
            task_id = task['id']
            payload = task['payload']
            config = task.get('config', {})
            env_vars = task.get('vars', {})

            # Credenciais deste worker chegam junto da tarefa (única fonte).
            novas_credenciais = extrair_credenciais(env_vars)
            faltando = [chave for chave, valor in novas_credenciais.items() if not valor]
            if faltando:
                print(f"\n⚠️ Tarefa #{task_id} sem credenciais no painel deste worker: {faltando}")
                try:
                    requests.post(
                        f"{BASE_URL}/api/complete-task/{task_id}/",
                        headers=headers,
                        json={"success": False, "result": {},
                              "error_message": f"Credenciais nao cadastradas no painel deste worker: {faltando}"},
                        timeout=15
                    )
                except Exception as post_err:
                    print(f"  ⚠ Falha ao devolver a tarefa ao Dashboard: {post_err}")
                if driver:
                    try: driver.quit()
                    except: pass
                    driver = None
                time.sleep(15)
                continue

            # Se mudaram no painel, a sessão aberta está logada com a credencial
            # antiga e precisa ser derrubada para refazer o login.
            if credenciais and novas_credenciais != credenciais and driver:
                print("\n🔑 Credenciais do painel mudaram. Fechando navegador para refazer o login...")
                try: driver.quit()
                except: pass
                driver = None
            credenciais = novas_credenciais

            print(f"\n⚡ Tarefa #{task_id} recebida! Preparando ambiente do Navegador...")

            # Garante conexão com o navegador Chrome ativo agora que temos uma tarefa
            if not driver:
                print("\n[Navegador] Inicializando navegador...")
                start_browser()
                print("\n[Navegador] Conectando ao navegador Chrome...")
                driver = connect_browser()
                _driver_ativo["driver"] = driver
                if not driver:
                    print("  ✖ Falha ao iniciar o Chrome. Verifique se o Google Chrome está instalado/atualizado.")
                    print("  → Aguardando 10 segundos para tentar reconectar...")
                    time.sleep(10)
                    continue
                print("  ✔ Conectado ao navegador com sucesso!")
                
                # Verificar se está logado e acionar Auto-Login se necessário
                try:
                    current_url = driver.current_url
                    if "simplifiquevivoemp.com.br" not in current_url and "SWEView" not in current_url:
                        print("  [Auth] O navegador não parece estar na área logada. Iniciando Auto-Login...")
                        login_sucesso = loginVivo(driver, credenciais)
                        if not login_sucesso:
                            print("  ✖ Falha no Auto-Login. Fechando navegador e limpando sessao...")
                            try: driver.quit()
                            except: pass
                            driver = None
                            time.sleep(10)
                            continue

                        print("  ✔ Auto-Login realizado com sucesso!")
                        try:
                            for _ in range(15):
                                token = driver.execute_script("return localStorage.getItem('JwtToken');")
                                if token:
                                    print("  ✔ Token (JwtToken) identificado com sucesso no localStorage!")
                                    time.sleep(5)
                                    break
                                time.sleep(2)
                        except Exception as token_err:
                            print(f"  ⚠ Erro ao verificar token no localStorage: {token_err}")

                            
                    # Garante que estamos na URL pós-login (Siebel) e com a aba 'Venda' ativa
                    redirect_url = os.getenv("LOGIN_REDIRECT_URL", "https://vivovendas.vivo.com.br/sales_ext/start.swe?SWECmd=GotoView&SWEView=NV+Dealer+Home+Page+View&SWERF=1&SWEHo=vivovendas.vivo.com.br&SWEBU=1")
                    if redirect_url:
                        current_url = driver.current_url
                        if "SWEView" not in current_url:
                            max_redirect_attempts = 3
                            for attempt in range(1, max_redirect_attempts + 1):
                                print(f"  → Redirecionando para o Siebel (Tentativa {attempt}/{max_redirect_attempts}): {redirect_url}")
                                driver.get(redirect_url)
                                print("  → Aguardando o portal carregar (5 segundos)...")
                                time.sleep(5.0)
                                
                                # Verifica se o Siebel retornou tela de servidor ocupado
                                if verificar_erro_siebel(driver):
                                    print(f"  ⚠ Erro 'Servidor Ocupado' detectado na tentativa {attempt}.")
                                    if attempt < max_redirect_attempts:
                                        print("  → Aguardando 10 segundos antes de tentar novamente o redirecionamento...")
                                        time.sleep(10)
                                        continue
                                    else:
                                        print("  🚨 Erro do Siebel persistiu após todas as tentativas. Reiniciando navegador...")
                                        try: driver.quit()
                                        except: pass
                                        driver = None
                                        break
                                else:
                                    # Sucesso no redirecionamento
                                    break
                            
                            # Se o driver foi reiniciado/zerado
                            if not driver:
                                continue
                        
                        # Uma vez na página do Siebel, garante que a aba Venda está ativa com retentativas para evitar elementos obsoletos (stale elements)
                        click_venda_success = False
                        for attempt_click in range(1, 6):
                            try:
                                # Aguarda o portal estar pronto/não ocupado
                                wait_for_siebel_ready(driver, timeout=20)
                                
                                # Verifica se a aba Venda está ativa
                                venda_ativa = driver.find_elements(By.XPATH, "//li[contains(@class, 'ui-tabs-active') or @aria-selected='true']//a[normalize-space(text())='Venda' or contains(@title, 'Venda')]")
                                if not venda_ativa:
                                    print(f"  → Aba 'Venda' não está ativa. Procurando e clicando (Tentativa {attempt_click}/5)...")
                                    btn_venda = WebDriverWait(driver, 10).until(
                                        EC.element_to_be_clickable((By.XPATH, "//a[normalize-space(text())='Venda' or contains(@title, 'Venda')]"))
                                    )
                                    btn_venda.click()
                                    print("  ✔ Botão/Aba 'Venda' clicada com sucesso!")
                                    time.sleep(3.0)
                                else:
                                    print("  ✔ Aba 'Venda' já está ativa.")
                                click_venda_success = True
                                break
                            except (StaleElementReferenceException, ElementClickInterceptedException) as click_retry_err:
                                print(f"  ⚠ Elemento obsoleto ou interceptado ao clicar na aba 'Venda': {click_retry_err}. Retentando em 2.0s...")
                                time.sleep(2.0)
                            except Exception as click_err:
                                print(f"  ⚠ Erro inesperado ao tentar clicar na aba 'Venda': {click_err}. Retentando em 2.0s...")
                                time.sleep(2.0)
                        
                        if not click_venda_success:
                            print("  🚨 Falha ao clicar na aba 'Venda' após 5 tentativas.")
                                
                except Exception as redir_err:
                    print(f"  ⚠ Erro ao verificar login ou redirecionar: {redir_err}")
            
            # Testa se a sessão do browser ainda responde e se está livre do erro Siebel (com retentativa local)
            try:
                driver.execute_script("return 1")
                if verificar_erro_siebel(driver):
                    print("  ⚠ Erro 'Servidor Ocupado' detectado no loop principal. Tentando refresh...")
                    driver.refresh()
                    time.sleep(5)
                    if verificar_erro_siebel(driver):
                        redirect_url = os.getenv("LOGIN_REDIRECT_URL", "https://vivovendas.vivo.com.br/sales_ext/start.swe?SWECmd=GotoView&SWEView=NV+Dealer+Home+Page+View&SWERF=1&SWEHo=vivovendas.vivo.com.br&SWEBU=1")
                        print(f"  → Servidor continua ocupado após refresh. Tentando forçar redirecionamento para {redirect_url}...")
                        driver.get(redirect_url)
                        time.sleep(5)
                        if verificar_erro_siebel(driver):
                            raise RuntimeError("Portal Siebel ocupado persistentemente")
            except Exception as e:
                print(f"\n[Navegador] Conexão com o navegador perdida ou erro no Siebel: {e}. Resetando driver...")
                if driver:
                    try: driver.quit()
                    except: pass
                driver = None
                continue

            # 2. Processar a Extração
            try:
                # Tarefa de cobertura (CEP/número) usa um fluxo separado do de CNPJ.
                if payload.get("cep") and payload.get("numero"):
                    result = process_coverage_task(driver, payload)
                else:
                    result = process_task(driver, payload, config)
                success = True
                error_msg = None
            except Exception as e:
                success = False
                result = {}
                error_msg = str(traceback.format_exc())
                print(f"❌ Erro no processamento: {e}")

            # 3. Enviar Resultado de volta ao Dashboard
            post_data = {
                "success": success,
                "result": result,
                "error_message": error_msg
            }
            
            requests.post(
                f"{BASE_URL}/api/complete-task/{task_id}/",
                headers=headers,
                json=post_data,
                timeout=15
            )
            print(f"✅ Tarefa #{task_id} finalizada com sucesso!" if success else f"⚠️ Tarefa #{task_id} reportada com erro.")
            
            if success:
                first_error_time = None
            else:
                if not first_error_time:
                    first_error_time = time.time()
                elif time.time() - first_error_time > 600:
                    print("\n🚨 ERROS PERSISTENTES POR 10 MINUTOS. REINICIANDO NAVEGADOR E FLUXO DO ZERO...")
                    if driver:
                        try: driver.quit()
                        except: pass
                        driver = None
                    first_error_time = None
                    time.sleep(2)

        except requests.exceptions.ConnectionError:
            print("\n⚠️ Falha de rede ao conectar ao Dashboard. Retentando em 10s...")
            if not first_error_time: first_error_time = time.time()
            elif time.time() - first_error_time > 600:
                print("\n🚨 ERROS PERSISTENTES POR 10 MINUTOS. REINICIANDO NAVEGADOR E FLUXO DO ZERO...")
                if driver:
                    try: driver.quit()
                    except: pass
                    driver = None
                first_error_time = None
            time.sleep(10)
        except Exception as e:
            print(f"\n❌ Erro no loop geral do worker: {e}")
            if not first_error_time: first_error_time = time.time()
            elif time.time() - first_error_time > 600:
                print("\n🚨 ERROS PERSISTENTES POR 10 MINUTOS. REINICIANDO NAVEGADOR E FLUXO DO ZERO...")
                if driver:
                    try: driver.quit()
                    except: pass
                    driver = None
                first_error_time = None
            time.sleep(5)

if __name__ == "__main__":
    run_worker()
