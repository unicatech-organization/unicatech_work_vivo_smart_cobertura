import os
import re
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import WebDriverException, NoSuchWindowException
from time import sleep, time
from loguru import logger
from src.auth.ouvir_audio import transcrever_captcha
from src.auth.token_email import token_email, limpar_emails_antigos
from src.utils.helpers import verificar_telas_de_erro, TelaDeErroException

# Constantes de retry
MAX_TENTATIVAS_LOGIN = 5
PAUSA_APOS_FALHAS_MIN = 5
MAX_RETRIES_CAMPO_VAZIO = 10

# Esperas (segundos) - ajustadas para reduzir o tempo de ciclo entre tentativas
ESPERA_POS_NAVEGACAO = 1.5   # apos driver.get()
ESPERA_POS_ERRO = 1          # antes de repetir um passo que falhou
ESPERA_CURTA = 0.5           # entre interacoes no mesmo formulario

# Espera pelo resultado apos submeter o token OTP
TIMEOUT_RESULTADO_TOKEN = 15
INTERVALO_POLL_RESULTADO = 0.5

def _erro_sessao_invalida(erro):
    texto = str(erro).lower()
    indicadores = [
        "invalid session id", "no such window", "session not created",
        "unable to connect", "target window already closed", "browser not reachable",
        "navegador instável"
    ]
    return any(ind in texto for ind in indicadores)

def _msg_curta(erro):
    return str(erro).split('\n')[0].strip()

def _verificar_msg_tempo_esgotado(driver):
    try:
        body_text = driver.find_element(By.TAG_NAME, "body").text.lower()
        if "tempo esgotado" in body_text or "autenticação expirou" in body_text or "processo de autenticação expirou" in body_text:
            return True
    except: pass

    try:
        msgs = driver.find_elements(By.CLASS_NAME, "msg")
        for msg in msgs:
            if msg.is_displayed():
                texto = msg.text.lower()
                if "tempo esgotado" in texto or "expirou" in texto:
                    return True
    except: pass
    return False

def _esta_na_tela_login(driver):
    """True se o navegador voltou para a tela inicial de login (usuario/senha/captcha).

    Apos um OTP incorreto o site redireciona para o login: o campo de passcode
    some, o que sem esta checagem seria confundido com autenticacao concluida.
    """
    try:
        for by, valor in ((By.ID, "username"), (By.ID, "password"), (By.ID, "captcha-input")):
            elementos = driver.find_elements(by, valor)
            if elementos and elementos[0].is_displayed():
                return True
    except: pass
    return False

def _mensagem_de_erro_na_tela(driver):
    """Retorna o texto da primeira mensagem de erro visivel, ou None."""
    try:
        for msg in driver.find_elements(By.CLASS_NAME, "msg"):
            if msg.is_displayed() and msg.text.strip():
                return msg.text.strip()
    except: pass
    return None

CHAVES_CREDENCIAIS = ("vivo_user", "vivo_senha", "email_user", "email_senha")

def loginVivo(driver, credenciais):
    """Faz o login no portal Vivo.

    credenciais: dict com vivo_user, vivo_senha, email_user e email_senha,
    entregues pelo painel junto da tarefa. Nao ha leitura de .env aqui - quem
    chama e responsavel por fornecer os valores.
    """
    url_base = os.getenv("LOGIN_URL", "https://simplifiquevivoemp.com.br")
    credenciais = credenciais or {}
    usuario = credenciais.get("vivo_user")
    senha = credenciais.get("vivo_senha")
    email_user = credenciais.get("email_user")
    email_senha = credenciais.get("email_senha")

    faltando = [chave for chave in CHAVES_CREDENCIAIS if not credenciais.get(chave)]
    if faltando:
        logger.error(f"Credenciais nao informadas para o login: {', '.join(faltando)}")
        return False

    tentativas_login = 0

    while True:
        tentativas_login += 1
        if tentativas_login > MAX_TENTATIVAS_LOGIN:
            logger.error(f"Todas as {MAX_TENTATIVAS_LOGIN} tentativas de login esgotadas. Retornando falha para o worker reiniciar o navegador.")
            return False
        
        logger.info(f"--- Iniciando Tentativa de Login {tentativas_login}/{MAX_TENTATIVAS_LOGIN} ---")
        
        # Força o recarregamento limpo da página de login a cada tentativa principal
        try:
            logger.info(f"[Login] Recarregando página de login: {url_base}")
            driver.get(url_base)
            sleep(ESPERA_POS_NAVEGACAO)
        except Exception as nav_err:
            logger.error(f"[Login] Erro ao carregar página inicial: {nav_err}")
            if _erro_sessao_invalida(nav_err): return False
            sleep(ESPERA_POS_NAVEGACAO)

        captcha_ok = False
        momento_solicitacao = None
        retries_campo_vazio = 0
        retries_inner = 0
        MAX_RETRIES_INNER = 10
        
        while True:
            if retries_inner >= MAX_RETRIES_INNER:
                logger.error(f"[Login] Loop interno atingiu {MAX_RETRIES_INNER} tentativas sem sucesso. Saindo do loop interno.")
                break
            try:
                try:
                    _ = driver.current_url
                except Exception as e:
                    logger.error(f"Navegador não responde: {_msg_curta(e)}")
                    return False
                
                if _verificar_msg_tempo_esgotado(driver):
                    logger.warning("[Login] Tempo esgotado detectado no navegador. Recarregando...")
                    driver.get(url_base)
                    sleep(ESPERA_POS_NAVEGACAO)
                    retries_inner += 1
                    break
                
                try:
                    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.ID, "username")))
                    sleep(ESPERA_CURTA)
                    
                    usuarioLogin = driver.find_element(By.ID, "username")
                    usuarioLogin.clear()
                    usuarioLogin.send_keys(usuario)
                    
                    senhaLogin = driver.find_element(By.ID, "password")
                    senhaLogin.clear()
                    senhaLogin.send_keys(senha)
                    
                    valor_usuario = usuarioLogin.get_attribute("value")
                    if not valor_usuario:
                        logger.warning("Campo de login vazio, tentando preencher via JS...")
                        driver.execute_script(f"arguments[0].value = '{usuario}';", usuarioLogin)
                        driver.execute_script(f"arguments[0].value = '{senha}';", senhaLogin)
                        valor_usuario = usuarioLogin.get_attribute("value")
                        
                    if not valor_usuario:
                        retries_campo_vazio += 1
                        if retries_campo_vazio >= MAX_RETRIES_CAMPO_VAZIO:
                            retries_campo_vazio = 0
                            raise Exception("Campo esvaziado repetidamente")
                        sleep(ESPERA_CURTA)
                        continue
                        
                    retries_campo_vazio = 0
                    
                except Exception as e:
                    if _erro_sessao_invalida(e): return False
                    retries_inner += 1
                    logger.error(f"[Login] Erro ao preencher login ({retries_inner}/{MAX_RETRIES_INNER}): {_msg_curta(e)}")
                    driver.get(url_base)
                    sleep(ESPERA_POS_NAVEGACAO)
                    continue

                try:
                    sleep(ESPERA_POS_ERRO)
                    palavraCaptcha = ""

                    # O audio vem direto do endpoint do captcha (mesma chamada do
                    # botao "ouvir"): sem gravar o alto-falante, sem corrida de
                    # timing. Ate 3 tentativas, pedindo um captcha novo (#r) a cada
                    # transcricao invalida.
                    for tentativa_audio in range(3):
                        transcrito = transcrever_captcha(driver)
                        transcrito_limpo = re.sub(r'[^a-zA-Z0-9]', '', transcrito).lower()
                        logger.info(f"Captcha transcrito (tentativa {tentativa_audio+1}): Limpo='{transcrito_limpo}'")

                        if len(transcrito_limpo) == 5:
                            palavraCaptcha = transcrito_limpo
                            break

                        logger.warning("Transcrição inválida (tamanho != 5). Solicitando novo captcha...")
                        try: driver.find_element(By.ID, "r").click()
                        except: pass
                        sleep(ESPERA_POS_ERRO)

                    if not palavraCaptcha:
                        raise Exception("Transcrição do áudio inválida após 3 tentativas")

                except Exception as e:
                    retries_inner += 1
                    logger.error(f"[Audio/Transcrição] Erro ({retries_inner}/{MAX_RETRIES_INNER}): {e}")
                    driver.get(url_base)
                    sleep(ESPERA_POS_NAVEGACAO)
                    continue

                try:
                    driver.find_element(By.ID, 'captcha-input').send_keys(palavraCaptcha)
                    sleep(ESPERA_CURTA)
                    driver.find_element(By.CLASS_NAME, 'btn-submit').click()
                except Exception as e:
                    if _erro_sessao_invalida(e): return False
                    retries_inner += 1
                    logger.error(f"[Submit Captcha] Erro ao enviar captcha ({retries_inner}/{MAX_RETRIES_INNER}): {e}")
                    driver.get(url_base)
                    sleep(ESPERA_POS_NAVEGACAO)
                    continue

                try:
                    sleep(ESPERA_POS_NAVEGACAO)
                    if _verificar_msg_tempo_esgotado(driver):
                        logger.warning("[Submit Captcha] Tempo esgotado detectado pós-submit. Recarregando...")
                        driver.get(url_base)
                        sleep(ESPERA_POS_NAVEGACAO)
                        retries_inner += 1
                        break
                    
                    msgs = driver.find_elements(By.CLASS_NAME, "msg")
                    if len(msgs) > 0 and msgs[0].is_displayed() and msgs[0].text.strip() != "":
                        mensagem_erro = msgs[0].text.strip()
                        logger.warning(f"[Submit Captcha] Mensagem de erro na tela: {mensagem_erro}")
                        driver.get(url_base)
                        sleep(ESPERA_POS_NAVEGACAO)
                        retries_inner += 1
                        continue
                except Exception:
                    pass

                try:
                    WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.ID, 'email')))
                except Exception as e:
                    if _erro_sessao_invalida(e): return False
                    retries_inner += 1
                    logger.error(f"[Email Tela] Erro ao aguardar tela de email ({retries_inner}/{MAX_RETRIES_INNER}): {e}")
                    driver.get(url_base)
                    sleep(ESPERA_POS_NAVEGACAO)
                    continue

                try:
                    driver.find_element(By.ID, 'email').click()

                    # Limpa a caixa ANTES do clique: qualquer e-mail nao lido apos este
                    # ponto e necessariamente o token desta tentativa.
                    momento_solicitacao = limpar_emails_antigos(usuarioEmail=email_user, senhaEmail=email_senha)

                    driver.find_element(By.CLASS_NAME, 'btn-submit').click()
                    logger.info("Token OTP solicitado com sucesso.")
                    captcha_ok = True
                    break
                except Exception as e:
                    if _erro_sessao_invalida(e): return False
                    retries_inner += 1
                    logger.error(f"[Email Submit] Erro ao clicar no email/submeter ({retries_inner}/{MAX_RETRIES_INNER}): {e}")
                    driver.get(url_base)
                    sleep(ESPERA_POS_NAVEGACAO)
                    continue

            except TelaDeErroException:
                return False
            except Exception as e:
                if _erro_sessao_invalida(e): return False
                verificar_telas_de_erro(driver)
                retries_inner += 1
                try: driver.get(url_base)
                except: return False
                sleep(ESPERA_POS_NAVEGACAO)
                continue

        # Nos 'continue' abaixo nao ha driver.get(): o topo do loop externo ja
        # recarrega a pagina de login, evitando carregar a mesma URL duas vezes.
        if not captcha_ok:
            logger.warning("[Login] Captcha não concluído na tentativa atual. Recarregando e tentando novamente...")
            continue

        PASSCODE_XPATH = '//*[@id="passcode"] | //input[@name="passcode"] | //input[contains(@id, "passcode")] | //input[contains(@id, "code")]'
        
        try:
            WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.XPATH, PASSCODE_XPATH)))
        except Exception as e:
            if _erro_sessao_invalida(e): return False
            logger.error(f"[Passcode] Tela de passcode nao apareceu: {e}")
            continue

        try:
            token = token_email(timeout_segundos=60, desde=momento_solicitacao,
                                usuarioEmail=email_user, senhaEmail=email_senha)
            logger.info(f"Token capturado: {token}")
        except Exception as e:
            logger.error(f"[Token E-mail] Erro ao obter token do e-mail: {e}")
            continue

        try:
            escreverToken = WebDriverWait(driver, 5).until(EC.presence_of_element_located((By.XPATH, PASSCODE_XPATH)))
        except Exception as e:
            logger.error(f"[Passcode] Campo passcode sumiu: {e}")
            continue

        try:
            from selenium.webdriver.common.keys import Keys
            escreverToken.clear()
            escreverToken.send_keys(token)
            sleep(0.5)
            
            try:
                driver.find_element(By.CLASS_NAME, 'btn-submit').click()
            except:
                escreverToken.send_keys(Keys.ENTER)

            # Poll de 0,5s (teto de 15s): reage ao resultado assim que a tela muda,
            # em vez de esperar o proximo segundo cheio.
            erro_detectado = False
            for _ in range(int(TIMEOUT_RESULTADO_TOKEN / INTERVALO_POLL_RESULTADO)):
                sleep(INTERVALO_POLL_RESULTADO)
                try: _ = driver.current_url
                except: return False

                if _verificar_msg_tempo_esgotado(driver):
                    logger.warning("[Passcode] Tempo esgotado detectado apos submeter token.")
                    erro_detectado = True
                    break

                mensagem_erro = _mensagem_de_erro_na_tela(driver)
                if mensagem_erro:
                    logger.warning(f"[Passcode] Erro na tela: {mensagem_erro}")
                    erro_detectado = True
                    break

                # Token recusado: o site devolve para a tela de login. Detectar aqui
                # evita esperar o teto do loop e evita falso positivo de sucesso.
                if _esta_na_tela_login(driver):
                    logger.warning("[Passcode] Token recusado: redirecionado de volta para a tela de login.")
                    erro_detectado = True
                    break

                if len(driver.find_elements(By.XPATH, PASSCODE_XPATH)) == 0:
                    break

            if erro_detectado:
                continue

            if len(driver.find_elements(By.XPATH, PASSCODE_XPATH)) > 0:
                logger.warning(f"[Passcode] Campo de passcode ainda presente na tela apos {TIMEOUT_RESULTADO_TOKEN}s.")
                continue

            # O passcode sumir nao basta: confirma que nao caiu de volta no login.
            if _esta_na_tela_login(driver):
                logger.warning("[Passcode] Passcode sumiu, mas a tela de login reapareceu. Tentando novamente...")
                continue

            logger.info("Login concluido com sucesso!")
            return True

        except Exception as e:
            logger.error(f"[Passcode Submit] Erro ao submeter token: {e}")
            continue

    return False
