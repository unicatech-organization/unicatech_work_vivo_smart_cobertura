import sys
import os
import json
import subprocess
import time
import undetected_chromedriver as uc
from dotenv import load_dotenv
from loguru import logger

load_dotenv()
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# Perfil fixo do navegador: sempre a mesma pasta, em vez de um perfil novo em
# %TEMP% a cada conexao. Isso mantem a sessao logada entre reinicios do robo
# e, principalmente, torna seguro identificar um chrome.exe orfao - so mata
# processo cuja linha de comando aponta EXATAMENTE pra esta pasta, nunca
# um Chrome qualquer que o usuario tenha aberto por conta propria.
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHROME_PROFILE_DIR = os.path.join(BASE_DIR, "chrome_session")

def _detectar_versao_chrome() -> int | None:
    """Detecta a versão major do Chrome instalada no sistema (Windows)."""
    # 1. Tenta pelo registro do Windows
    try:
        resultado = subprocess.run(
            ['reg', 'query', r'HKLM\SOFTWARE\Google\Chrome\BLBeacon', '/v', 'version'],
            capture_output=True, text=True, timeout=5
        )
        if resultado.returncode == 0:
            for linha in resultado.stdout.splitlines():
                if 'version' in linha.lower():
                    versao = linha.strip().split()[-1]
                    major = int(versao.split('.')[0])
                    logger.info(f"Versão do Chrome detectada via registro: {versao} (major={major})")
                    return major
    except Exception:
        pass

    # 2. Tenta pelo executável do Chrome
    caminhos = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for caminho in caminhos:
        try:
            resultado = subprocess.run(
                ['powershell', '-Command', f'(Get-Item "{caminho}").VersionInfo.ProductVersion'],
                capture_output=True, text=True, timeout=5
            )
            if resultado.returncode == 0 and resultado.stdout.strip():
                versao = resultado.stdout.strip()
                major = int(versao.split('.')[0])
                logger.info(f"Versão do Chrome detectada via executável: {versao} (major={major})")
                return major
        except Exception:
            pass

    return None


def limpar_navegadores_orfaos():
    """Mata chrome.exe/chromedriver.exe presos no perfil fixo do robo, se sobrou algum.

    Como o perfil e sempre a mesma pasta (CHROME_PROFILE_DIR), nao ha mais
    lixo se acumulando em %TEMP%. O unico risco e um chrome.exe de uma
    execucao anterior que nao fechou direito (crash, taskkill /F, queda de
    energia) continuar de pe segurando esse MESMO perfil - ai o Chrome novo
    recusa abrir ("profile already in use"). So mata processo cuja linha de
    comando aponta EXATAMENTE pra essa pasta, entao nunca fecha outro Chrome
    que o usuario tenha aberto por conta propria. Chamado uma vez no inicio
    do worker, antes de abrir o navegador desta execucao.
    """
    try:
        resultado = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' or Name='chromedriver.exe'\" "
             "| Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=15
        )
        processos = json.loads(resultado.stdout) if resultado.returncode == 0 and resultado.stdout.strip() else []
        if isinstance(processos, dict):
            processos = [processos]
    except Exception as e:
        logger.debug(f"[Limpeza] Nao foi possivel listar processos do Chrome: {e}")
        processos = []

    pids_mortos = 0
    for proc in processos:
        cmd = proc.get("CommandLine") or ""
        if CHROME_PROFILE_DIR.lower() not in cmd.lower():
            continue
        pid = proc.get("ProcessId")
        if not pid:
            continue
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=5)
            pids_mortos += 1
        except Exception:
            pass

    if pids_mortos:
        logger.info(f"[Limpeza] {pids_mortos} processo(s) orfao(s) do Chrome (perfil do robo) finalizado(s).")
        time.sleep(1)  # da tempo do Windows liberar o lock dos arquivos

    # Lock deixado por um encerramento anterior que nao passou pelo driver.quit()
    lock = os.path.join(CHROME_PROFILE_DIR, "SingletonLock")
    if os.path.exists(lock) or os.path.islink(lock):
        try:
            os.remove(lock)
            logger.debug("[Limpeza] SingletonLock antigo do perfil removido.")
        except Exception:
            pass


def start_browser():
    """Mantém a compatibilidade com o script principal."""
    logger.info("Preparando inicialização do navegador (undetected_chromedriver)...")
    return True

def _criar_opcoes_chrome() -> "uc.ChromeOptions":
    """Monta um ChromeOptions novo.

    Precisa ser recriado a cada tentativa de uc.Chrome(): o Selenium consome
    (finaliza) o objeto assim que ele e passado pro construtor, entao reusar
    o mesmo objeto numa segunda tentativa (ex: fallback sem version_main)
    derruba com "you cannot reuse the ChromeOptions object".
    """
    opcoes = uc.ChromeOptions()

    # Previne que o Chrome hiberne ou suspenda áudio/vídeo quando a janela não está focada
    opcoes.add_argument("--disable-background-timer-throttling")
    opcoes.add_argument("--disable-backgrounding-occluded-windows")
    opcoes.add_argument("--disable-renderer-backgrounding")
    opcoes.add_argument("--disable-features=CalculateNativeWinOcclusion")

    # Impede que o Chrome feche sozinho por falta de sandbox ou GPU issues
    opcoes.add_argument("--no-sandbox")
    opcoes.add_argument("--disable-dev-shm-usage")
    return opcoes


def connect_browser():
    """Inicia o undetected_chromedriver para contornar o Cloudflare e retorna a instância do driver."""
    logger.info("Iniciando undetected_chromedriver...")
    os.makedirs(CHROME_PROFILE_DIR, exist_ok=True)

    # Garante que nao sobrou nenhum chrome.exe/chromedriver.exe preso do
    # ciclo anterior antes de tentar abrir um novo. Sem isso, um zumbi de uma
    # tentativa anterior que deu "chrome not reachable" (ou de um driver.quit()
    # que falhou num navegador ja quebrado) fica segurando o profile - a
    # proxima tentativa demora bem mais pra abrir (ou tambem trava) e deixa
    # OUTRO zumbi, e assim por diante: depois de algumas horas de worker
    # rodando, o acumulo de processos presos e o motivo do Chrome demorar
    # cada vez mais ate estourar o timeout de conexao (~60s) e falhar direto.
    try:
        limpar_navegadores_orfaos()
    except Exception as e:
        logger.warning(f"Falha ao limpar navegadores órfãos antes de conectar: {e}")

    # Detecta a versao do Chrome REALMENTE instalada na maquina. Prioridade sobre
    # o .env: o Chrome se auto-atualiza sozinho, entao um CHROME_VERSION fixo no
    # .env fica desatualizado com o tempo e passa a forcar um chromedriver de
    # versao errada (foi causa de bug em producao). O .env vira so um fallback
    # manual para quando a deteccao automatica falhar nesta maquina.
    versao = None
    versao_detectada = _detectar_versao_chrome()
    if versao_detectada:
        versao = str(versao_detectada)
    else:
        versao = os.getenv("CHROME_VERSION")
        if versao:
            logger.warning(f"Não foi possível detectar a versão do Chrome automaticamente. Usando CHROME_VERSION do .env: {versao}")
        else:
            logger.warning("Não foi possível detectar a versão do Chrome automaticamente e CHROME_VERSION não está definida no .env.")

    if versao:
        try:
            logger.info(f"Iniciando Chrome com version_main={versao}")
            driver = uc.Chrome(options=_criar_opcoes_chrome(), version_main=int(versao), user_data_dir=CHROME_PROFILE_DIR)
            logger.info("✔ Navegador iniciado com sucesso!")
            return driver
        except Exception as e:
            logger.warning(f"Falha com CHROME_VERSION={versao}: {e}. Tentando detecção automática do UC...")
            # A tentativa acima pode ter deixado um chrome.exe/chromedriver.exe
            # preso segurando o perfil (ex: "chrome not reachable"); sem isso
            # a proxima tentativa falha so por causa do lock do perfil.
            try:
                limpar_navegadores_orfaos()
            except Exception as e2:
                logger.warning(f"Falha ao limpar navegadores órfãos após tentativa frustrada: {e2}")

    try:
        logger.info("Tentando iniciar Chrome sem especificar versão (detecção automática do UC)...")
        driver = uc.Chrome(options=_criar_opcoes_chrome(), user_data_dir=CHROME_PROFILE_DIR)
        logger.info("✔ Navegador iniciado com sucesso!")
        return driver
    except Exception as e:
        logger.error(f"Falha ao iniciar undetected_chromedriver: {e}")
        logger.error("Dica: Verifique se o Google Chrome está instalado e atualizado.")
        logger.error("Dica: Tente definir CHROME_VERSION=XXX no .env (ex: CHROME_VERSION=151)")
        try:
            limpar_navegadores_orfaos()
        except Exception as e2:
            logger.warning(f"Falha ao limpar navegadores órfãos após tentativa frustrada: {e2}")
        return None
