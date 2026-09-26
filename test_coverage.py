"""
Teste manual da consulta de cobertura, sem o painel: faz login uma vez e
consulta cada endereço com a MESMA process_task do worker.py.

Uso:
    python test_coverage.py                         # endereços de exemplo
    python test_coverage.py 13419230:1180 00000000:10
"""
import os
import sys
import json
from dotenv import load_dotenv

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

from worker import abrir_sessao_siebel, fechar_navegador, process_task

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

EXEMPLOS = [
    ("13419230", "1180"),   # existe, GPON com disponibilidade
    ("13419230", "99999"),  # número fora da faixa: Siebel devolve CEP vizinho
    ("00000000", "10"),     # CEP inexistente
]

if __name__ == "__main__":
    enderecos = [tuple(a.split(":", 1)) for a in sys.argv[1:]] or EXEMPLOS

    driver = abrir_sessao_siebel(credenciais_locais())
    if not driver:
        print("Falha ao abrir a sessão do Siebel (navegador/login).")
        sys.exit(1)
    try:
        for cep, numero in enderecos:
            resultado = process_task(driver, {"cep": cep, "numero": numero}, {}, {})
            print(json.dumps(resultado, ensure_ascii=False, indent=2))
    finally:
        fechar_navegador(driver)
