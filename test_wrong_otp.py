import os
import sys
import time
from dotenv import load_dotenv
from loguru import logger
from selenium.webdriver.common.by import By

# Corrige encoding no Windows para logs do terminal
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from src.core.browser_manager import start_browser, connect_browser
import src.auth.login_captcha as lc

def test_wrong_otp():
    # Fora do painel, o teste le as credenciais do .env e as passa explicitamente:
    # loginVivo nunca le variaveis de ambiente.
    load_dotenv()
    credenciais = {
        "vivo_user": os.getenv("vivo_user"),
        "vivo_senha": os.getenv("vivo_senha"),
        "email_user": os.getenv("email_user"),
        "email_senha": os.getenv("email_senha"),
    }
    logger.info("=== Testando comportamento com OTP incorreto ===")
    
    start_browser()
    driver = connect_browser()
    if not driver:
        logger.error("Falha ao conectar ao navegador.")
        return

    # Substitui temporariamente a função token_email por um token comprovadamente ERRADO
    def fake_token_email(*args, **kwargs):
        logger.warning("[MOCK TEST] Injetando token OTP sabidamente INCORRETO: '000000'")
        return "000000"

    lc.token_email = fake_token_email

    logger.info("Iniciando loginVivo com token OTP incorreto forçado...")
    res = lc.loginVivo(driver, credenciais)
    if res:
        logger.error("ERRO INESPERADO: O login retornou True mesmo com OTP errado!")
    else:
        logger.info("SUCESSO NO TESTE: O login identificou a falha no OTP, tratou o erro e tentou novamente/retornou False corretamente.")

if __name__ == "__main__":
    test_wrong_otp()
