from time import sleep, time
from selenium.webdriver.common.by import By
from loguru import logger

class TelaDeErroException(Exception):
    pass

def verificar_telas_de_erro(driver):
    if driver.find_elements(By.CLASS_NAME, "sf-nao-autorizado-titulo"):
        raise TelaDeErroException("Tela de erro 1 detectada: Não foi Possível Autenticar o Usuário")

    if driver.find_elements(By.CLASS_NAME, "sf-erroOam"):
        raise TelaDeErroException("Tela de erro 2 detectada: Login Inválido (OAM)")

    swal2_containers = driver.find_elements(By.CLASS_NAME, "swal2-html-container")
    for container in swal2_containers:
        if container.is_displayed() and "Alguma coisa deu errado" in container.text:
            raise TelaDeErroException("Tela de erro 3 detectada: Swal2 Popup de erro genérico")

    try:
        body_element = driver.find_element(By.TAG_NAME, "body")
        if body_element:
            body_text = body_element.text
            if "tentando acessar está ocupado" in body_text or "apresenta problemas" in body_text or "fazer logon novamente" in body_text:
                raise TelaDeErroException("Tela de erro 4 detectada: Servidor Siebel ocupado ou indisponível")
            if "Detectamos um erro" in body_text or "Parâmetro inválido" in body_text or "SBL-" in body_text:
                raise TelaDeErroException("Tela de erro 5 detectada: Erro de aplicação Siebel (SBL-XXX)")
    except TelaDeErroException:
        raise
    except:
        pass

def load(driver, timeout=30):
    try:
        spinner = driver.find_element(By.XPATH, '/html/body/app-root/ngx-spinner/div/div[1]/div[1]')
        if spinner.is_displayed():
            logger.debug("Aguardando loading do Simplifique...")
            inicio = time()
            while spinner.is_displayed():
                if time() - inicio > timeout:
                    logger.warning(f"Loading excedeu o timeout de {timeout}s. Prosseguindo.")
                    break
                sleep(0.5)
            else:
                duracao = round(time() - inicio, 1)
                logger.debug(f"Loading concluído em {duracao}s.")
    except:
        pass
    verificar_telas_de_erro(driver)
