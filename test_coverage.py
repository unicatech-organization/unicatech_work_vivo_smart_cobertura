import os
import sys
import time
from dotenv import load_dotenv
from loguru import logger

# Corrige encoding no Windows para logs do terminal
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from src.core.browser_manager import start_browser, connect_browser
from src.auth.login_captcha import loginVivo
from src.siebel_api import SiebelAPIClient

def credenciais_locais():
    """Fora do painel (testes manuais), as credenciais vem do .env local.
    A leitura e explicita aqui: loginVivo nunca le variaveis de ambiente."""
    load_dotenv()
    return {
        "vivo_user": os.getenv("vivo_user"),
        "vivo_senha": os.getenv("vivo_senha"),
        "email_user": os.getenv("email_user"),
        "email_senha": os.getenv("email_senha"),
    }

def test_check_coverage(cep: str, numero: str, cidade: str = "", estado: str = ""):
    credenciais = credenciais_locais()
    logger.info("Starting coverage test for Vivo Smart APIs...")

    start_browser()
    driver = connect_browser()
    if not driver:
        logger.error("Failed to start/connect browser.")
        return

    logger.info("Executing loginVivo(driver, credenciais)...")
    if not loginVivo(driver, credenciais):
        logger.error("LOGIN FAILED!")
        return
    logger.info("LOGIN SUCCESSFUL!")

    # Garante que caiu na tela do Siebel (onde o objeto JS SiebelApp existe),
    # igual ao worker.py faz logo apos o login.
    redirect_url = os.getenv(
        "LOGIN_REDIRECT_URL",
        "https://vivovendas.vivo.com.br/sales_ext/start.swe?SWECmd=GotoView&SWEView=NV+Dealer+Home+Page+View&SWERF=1&SWEHo=vivovendas.vivo.com.br&SWEBU=1"
    )
    if "SWEView" not in driver.current_url:
        logger.info(f"Redirecionando para o Siebel: {redirect_url}")
        driver.get(redirect_url)
        time.sleep(5.0)

    # A view de cobertura precisa estar ativa no servidor antes do NVSearchAddress
    # funcionar (equivale a clicar no ícone "Validar Cobertura" na UI).
    coverage_view_url = "https://vivovendas.vivo.com.br/sales_ext/start.swe?SWECmd=GotoView&SWEView=NV+Check+Address+Coverage+Only+View+-+Dealer"
    logger.info(f"Navegando para a view de cobertura: {coverage_view_url}")
    driver.get(coverage_view_url)
    time.sleep(3.0)

    # Cria o client DEPOIS de navegar: ele le o SRN/SWEC atuais da pagina no __init__.
    client = SiebelAPIClient(driver)

    logger.info(f"Buscando cobertura para CEP={cep} N={numero} Cidade={cidade} Estado={estado}...")
    busca = client.search_coverage(cep, numero, cidade, estado)
    logger.info(f"Endereços encontrados: {busca['enderecos']}")

    detalhes = client.check_coverage_details()
    logger.info(f"Campos de cobertura: {detalhes['fields']}")
    logger.info(f"É GPON? {detalhes['is_gpon']}")

if __name__ == "__main__":
    test_check_coverage(cep="13419230", numero="1180", cidade="PIRACICABA", estado="SP")
