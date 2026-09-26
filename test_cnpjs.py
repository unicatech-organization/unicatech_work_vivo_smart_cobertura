import os
import sys
import time
import json
from dotenv import load_dotenv

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import StaleElementReferenceException, ElementClickInterceptedException

# Corrige encoding no Windows para logs do terminal
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from src.core.browser_manager import start_browser, connect_browser
from src.auth.login_captcha import loginVivo
from worker import process_task, verificar_erro_siebel, wait_for_siebel_ready

def test_cnpjs():
    cnpjs = [
        "10721990000100",
        "46206025000147",
        "46206025000147",
        "74485814000108",
        "74485814000108",
        "74485814000108",
        "40443579000181",
        "40443579000181",
        "50653594000108",
        "50653594000108",
        "42274956000121",
        "42274956000121",
        "5284044000104",
        "5284044000104"
    ]
    
    # Remove duplicates but keeps order
    unique_cnpjs = list(dict.fromkeys(cnpjs))
    print(f"Iniciando teste para {len(unique_cnpjs)} CNPJs únicos: {unique_cnpjs}")

    driver = None
    
    start_browser()
    driver = connect_browser()
    if not driver:
        print("Falha ao conectar ao Chrome. Certifique-se de que o Chrome de debug está aberto.")
        return

    try:
        current_url = driver.current_url
        if "simplifiquevivoemp.com.br" not in current_url and "SWEView" not in current_url:
            print("O navegador não parece estar na área logada. Iniciando Auto-Login...")
            login_sucesso = loginVivo(driver, {
                "vivo_user": os.getenv("vivo_user"),
                "vivo_senha": os.getenv("vivo_senha"),
                "email_user": os.getenv("email_user"),
                "email_senha": os.getenv("email_senha"),
            })
            if not login_sucesso:
                print("Falha no Auto-Login.")
                return

        redirect_url = os.getenv("LOGIN_REDIRECT_URL", "https://vivovendas.vivo.com.br/sales_ext/start.swe?SWECmd=GotoView&SWEView=NV+Dealer+Home+Page+View&SWERF=1&SWEHo=vivovendas.vivo.com.br&SWEBU=1")
        if redirect_url:
            current_url = driver.current_url
            if "SWEView" not in current_url:
                driver.get(redirect_url)
                time.sleep(5.0)
                if verificar_erro_siebel(driver):
                    print("Erro do Siebel detectado no carregamento inicial.")
                    return

            click_venda_success = False
            for attempt_click in range(1, 6):
                try:
                    wait_for_siebel_ready(driver, timeout=20)
                    venda_ativa = driver.find_elements(By.XPATH, "//li[contains(@class, 'ui-tabs-active') or @aria-selected='true']//a[normalize-space(text())='Venda' or contains(@title, 'Venda')]")
                    if not venda_ativa:
                        btn_venda = WebDriverWait(driver, 10).until(
                            EC.element_to_be_clickable((By.XPATH, "//a[normalize-space(text())='Venda' or contains(@title, 'Venda')]"))
                        )
                        btn_venda.click()
                        time.sleep(3.0)
                    click_venda_success = True
                    break
                except Exception as click_err:
                    time.sleep(2.0)
            
            if not click_venda_success:
                print("Falha ao clicar na aba 'Venda' após 5 tentativas.")
                return

    except Exception as redir_err:
        print(f"Erro ao verificar login ou redirecionar: {redir_err}")
        return

    results = []
    
    for cnpj in unique_cnpjs:
        print(f"\n======================================")
        print(f"=== Processando CNPJ: {cnpj} ===")
        print(f"======================================")
        
        try:
            res = process_task(driver, {"cnpj": cnpj})
            results.append({"cnpj": cnpj, "status": "success", "data": res})
            print(f"Sucesso para {cnpj}. Produtos formatados: {len(res)}")
        except Exception as e:
            print(f"Erro processando {cnpj}: {e}")
            results.append({"cnpj": cnpj, "status": "error", "error": str(e)})

    # Salva o resultado num arquivo JSON
    with open("test_results.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=4)
    
    print("\nTestes finalizados! Resultados salvos em test_results.json.")

if __name__ == "__main__":
    load_dotenv()
    test_cnpjs()
