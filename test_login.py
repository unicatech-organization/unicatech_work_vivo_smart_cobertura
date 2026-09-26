import os
import sys
import time
from dotenv import load_dotenv
from loguru import logger

# Corrige encoding no Windows para logs do terminal
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from src.core.browser_manager import start_browser, connect_browser
from src.auth.login_captcha import loginVivo

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

def test_only_login():
    credenciais = credenciais_locais()
    logger.info("Starting login test for Vivo Smart APIs...")
    
    start_browser()
    driver = connect_browser()
    if not driver:
        logger.error("Failed to start/connect browser.")
        return

    logger.info("Executing loginVivo(driver, credenciais)...")
    res = loginVivo(driver, credenciais)
    if res:
        logger.info("LOGIN SUCCESSFUL!")
    else:
        logger.error("LOGIN FAILED!")

if __name__ == "__main__":
    test_only_login()
