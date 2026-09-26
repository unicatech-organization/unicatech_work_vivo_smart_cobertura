import re
from datetime import datetime, timezone, timedelta
from imap_tools import MailBox, AND, MailMessageFlags
from time import sleep, time

REMETENTE_VIVO = "noreply@vivo.com.br"

# Tolerancia para diferenca de relogio entre o servidor de e-mail e a maquina local.
# E-mails com data anterior a (momento da solicitacao - esta margem) sao descartados.
TOLERANCIA_RELOGIO_SEG = 120


def _credenciais(senhaEmail, usuarioEmail):
    """As credenciais vem do painel (chaves email_user/email_senha) via loginVivo.
    Nao ha leitura de .env aqui: quem chama precisa informar os dois valores."""
    if not senhaEmail or not usuarioEmail:
        raise ValueError("Credenciais de e-mail nao informadas (email_user/email_senha)")

    return usuarioEmail, senhaEmail


def _data_em_utc(data_email):
    """Normaliza a data do e-mail para UTC (headers sem timezone assumem hora local)."""
    if data_email is None:
        return None
    if data_email.tzinfo is None:
        data_email = data_email.astimezone()
    return data_email.astimezone(timezone.utc)


def limpar_emails_antigos(servidorIMAP="mail.unicatechtelecom.com.br", senhaEmail=None, usuarioEmail=None):
    """Marca como lidos todos os e-mails da Vivo ja existentes na caixa de entrada.

    Deve ser chamada IMEDIATAMENTE ANTES de clicar no botao que solicita o token,
    para que qualquer e-mail nao lido restante seja necessariamente o desta tentativa.

    Retorna o momento (UTC) a partir do qual um e-mail e considerado valido.
    """
    usuarioEmail, senhaEmail = _credenciais(senhaEmail, usuarioEmail)

    try:
        with MailBox(servidorIMAP).login(usuarioEmail, senhaEmail) as meuEmail:
            uids = [msg.uid for msg in meuEmail.fetch(AND(from_=REMETENTE_VIVO, seen=False), mark_seen=False, headers_only=True) if msg.uid]
            if uids:
                meuEmail.flag(uids, MailMessageFlags.SEEN, True)
                print(f"{len(uids)} e-mail(s) antigo(s) da Vivo marcado(s) como lido(s).")
            else:
                print("Nenhum e-mail antigo da Vivo pendente na caixa de entrada.")
    except Exception as e:
        # Falha aqui nao e fatal: o filtro por data ainda protege contra token antigo.
        print(f"Nao foi possivel limpar e-mails antigos da Vivo: {e}")

    return datetime.now(timezone.utc)


def token_email(servidorIMAP="mail.unicatechtelecom.com.br", senhaEmail=None, usuarioEmail=None,
                timeout_segundos=60, desde=None):
    """Aguarda e retorna o token OTP enviado pela Vivo.

    desde: datetime (UTC) da solicitacao do token. E-mails anteriores a esse
           instante sao ignorados, evitando capturar um token antigo.
    """
    usuarioEmail, senhaEmail = _credenciais(senhaEmail, usuarioEmail)

    if desde is None:
        desde = datetime.now(timezone.utc)
    elif desde.tzinfo is None:
        desde = desde.astimezone()
    desde = desde.astimezone(timezone.utc)

    # Corte efetivo, com margem para dessincronia de relogio do servidor de e-mail.
    corte = desde - timedelta(seconds=TOLERANCIA_RELOGIO_SEG)
    # O IMAP filtra apenas por dia; a comparacao fina de horario e feita depois.
    data_busca = corte.astimezone().date()

    inicio = time()
    tentativas = 0
    while True:
        tentativas += 1
        tempo_decorrido = time() - inicio
        if tempo_decorrido > timeout_segundos:
            raise TimeoutError(f"Tempo limite de {timeout_segundos}s esgotado aguardando e-mail de token da Vivo.")

        try:
            with MailBox(servidorIMAP).login(usuarioEmail, senhaEmail) as meuEmail:
                criterio = AND(from_=REMETENTE_VIVO, seen=False, date_gte=data_busca)
                # mark_seen=True: o e-mail lido nunca sera reaproveitado numa proxima tentativa.
                candidatos = list(meuEmail.fetch(criterio, mark_seen=True))

                recentes = []
                for msg in candidatos:
                    data_msg = _data_em_utc(msg.date)
                    if data_msg is not None and data_msg >= corte:
                        recentes.append((data_msg, msg))

                if recentes:
                    recentes.sort(key=lambda item: item[0])
                    data_msg, ultimoEmail = recentes[-1]
                    mensagem = ultimoEmail.html or ultimoEmail.text or ""
                    match = re.search(r'>\s*(\d{6})\s*<', mensagem) or re.search(r'\b(\d{6})\b', mensagem)
                    if match:
                        token = match.group(1)
                    else:
                        token = mensagem[5815:5821]
                    print(f"Token capturado do e-mail recebido em {data_msg.astimezone():%H:%M:%S}: {token}")
                    return token

                if candidatos:
                    print(f"{len(candidatos)} e-mail(s) da Vivo ignorado(s) por serem anteriores a solicitacao.")
        except Exception as e:
            print(f"Aguardando e-mail de token... (tentativa {tentativas}, decorrido: {int(tempo_decorrido)}s): {e}")

        sleep(3)


if __name__ == "__main__":
    # Execucao manual para depuracao: as credenciais vao na linha de comando.
    import sys
    if len(sys.argv) < 3:
        print("Uso: python -m src.auth.token_email <email_user> <email_senha>")
    else:
        print(token_email(usuarioEmail=sys.argv[1], senhaEmail=sys.argv[2]))
