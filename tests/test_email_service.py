"""Testes do cliente SMTP dos alertas (sem rede e sem enviar e-mail).

Valida o contrato exigido pelo Gmail na porta 587:
``EHLO -> STARTTLS -> EHLO -> LOGIN -> SEND -> QUIT``; a porta 465 com SSL direto;
a normalização da Senha de App; a prioridade do ``.env`` sobre uma variável antiga
exportada no terminal; e a tradução de desconexão durante o login para uma mensagem
de autenticação compreensível na tela.

Uso (na raiz do projeto):
    python tests/test_email_service.py
"""

import os
import smtplib
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from backend import email_service as servico


class SMTPFalso:
    """SMTP em memória que registra a ordem das chamadas."""

    chamadas = []

    def __init__(self, host, porta, timeout=None, context=None):
        self.chamadas.append(("init", host, porta, timeout))

    def ehlo(self):
        self.chamadas.append(("ehlo",))

    def starttls(self, context=None):
        self.chamadas.append(("starttls", context is not None))

    def login(self, usuario, senha):
        self.chamadas.append(("login", usuario, len(senha)))

    def send_message(self, mensagem):
        SMTPFalso.ultima_mensagem = mensagem
        self.chamadas.append(
            (
                "send",
                mensagem["To"],
                mensagem["From"],
                mensagem["Subject"],
                mensagem["Cc"] if "Cc" in mensagem else "",
            )
        )

    def quit(self):
        self.chamadas.append(("quit",))

    def close(self):
        pass


class SMTPDesconectaNoLogin(SMTPFalso):
    def login(self, usuario, senha):
        raise smtplib.SMTPServerDisconnected("Connection unexpectedly closed")


CONFIG = {
    "SMTP_HOST": "smtp.gmail.com",
    "SMTP_PORT": "587",
    "SMTP_USER": "remetente@example.com",
    "SMTP_PASSWORD": "abcd efgh ijkl mnop",
    "SMTP_FROM_NAME": "Inteligência de Custos NF-e",
    "SMTP_TIMEOUT": "5",
}


def _executar_com_config(classe_smtp, porta, senha=None, copia=""):
    """Executa send_email com a classe SMTP falsa e devolve as chamadas."""
    SMTPFalso.chamadas = []
    config = {**CONFIG, "SMTP_PORT": str(porta)}
    if senha is not None:
        config["SMTP_PASSWORD"] = senha
    with patch.dict(os.environ, config):
        with patch.object(servico.smtplib, "SMTP", classe_smtp):
            with patch.object(servico.smtplib, "SMTP_SSL", classe_smtp):
                assert servico.send_email(
                    "destino@example.com",
                    "Alertas de preço — teste",
                    "Corpo do alerta",
                    cc_email=copia,
                )
    return list(SMTPFalso.chamadas)


def testar_handshake_587():
    chamadas = _executar_com_config(SMTPFalso, 587)
    assert [item[0] for item in chamadas] == [
        "init",
        "ehlo",
        "starttls",
        "ehlo",
        "login",
        "send",
        "quit",
    ], chamadas
    assert chamadas[0][2] == 587, chamadas
    assert chamadas[0][3] == 5.0, chamadas
    assert chamadas[4][2] == 16, chamadas  # senha sem os espaços de exibição
    assert chamadas[5][1] == "destino@example.com", chamadas
    print("OK 587:", " -> ".join(item[0] for item in chamadas))


def testar_handshake_465():
    chamadas = _executar_com_config(SMTPFalso, 465)
    assert [item[0] for item in chamadas] == [
        "init",
        "ehlo",
        "login",
        "send",
        "quit",
    ], chamadas
    print("OK 465:", " -> ".join(item[0] for item in chamadas))


def testar_desconexao_no_login():
    try:
        _executar_com_config(SMTPDesconectaNoLogin, 587)
    except smtplib.SMTPAuthenticationError as erro:
        assert erro.smtp_code == 535, erro
        assert "LOGIN" in erro.smtp_error.decode("utf-8"), erro
        print("OK login recusado:", erro.smtp_code, erro.smtp_error.decode("utf-8"))
    else:
        raise AssertionError("deveria traduzir a desconexão em erro de autenticação")


def testar_detalhe_sem_senha():
    erro = smtplib.SMTPAuthenticationError(
        535,
        b"5.7.8 Username and Password not accepted abcd efgh ijkl mnop",
    )
    with patch.dict(os.environ, CONFIG):
        detalhe = servico.detalhe_erro_smtp(erro)
    assert "535" in detalhe, detalhe
    assert "***" in detalhe, detalhe
    assert "abcd" not in detalhe, detalhe
    print("OK detalhe sem senha:", detalhe)


def testar_copia_email_secundario():
    chamadas = _executar_com_config(SMTPFalso, 587, copia="copia@example.com")
    assert chamadas[5][4] == "copia@example.com", chamadas
    # O mesmo endereço do destinatário não vira um Cc duplicado.
    chamadas = _executar_com_config(SMTPFalso, 587, copia="DESTINO@example.com")
    assert chamadas[5][4] == "", chamadas
    print("OK Cc: e-mail secundário entra como cópia (sem duplicar o destinatário)")


def testar_configuracao_ausente():
    with patch.dict(os.environ, {**CONFIG, "SMTP_HOST": ""}):
        try:
            servico.send_email("destino@example.com", "Assunto", "Corpo")
        except ValueError as erro:
            assert "SMTP_HOST" in str(erro), erro
            print("OK config ausente:", erro)
        else:
            raise AssertionError("SMTP_HOST vazio deveria levantar ValueError")


def testar_env_override():
    """O .env novo precisa vencer um SMTP_PASSWORD antigo no ambiente do processo."""
    with tempfile.TemporaryDirectory() as pasta:
        arquivo = Path(pasta) / ".env"
        arquivo.write_text("SMTP_PASSWORD=abcd efgh ijkl mnop\n", encoding="utf-8")
        with patch.dict(
            os.environ, {"SMTP_PASSWORD": "senha-antiga-exportada-no-terminal"}
        ):
            assert os.environ["SMTP_PASSWORD"] == "senha-antiga-exportada-no-terminal"
            servico.recarregar_configuracao_smtp(arquivo)
            assert os.environ["SMTP_PASSWORD"] == "abcd efgh ijkl mnop", os.environ[
                "SMTP_PASSWORD"
            ]
    print("OK override: .env venceu a variável antiga do ambiente")


def testar_corpo_html_multipart():
    """`html_body` envia multipart/alternative: texto puro + HTML na mesma mensagem.

    É o que faz o relatório de compras chegar formatado no Gmail/Outlook sem quebrar
    clientes/leitores que só abrem texto.
    """
    SMTPFalso.chamadas = []
    SMTPFalso.ultima_mensagem = None
    with patch.dict(os.environ, CONFIG):
        with patch.object(servico.smtplib, "SMTP", SMTPFalso):
            with patch.object(servico.smtplib, "SMTP_SSL", SMTPFalso):
                assert servico.send_email(
                    "destino@example.com",
                    "Relatório de compras",
                    "Texto puro do relatório",
                    html_body="<p>Relatório <b>HTML</b></p>",
                )
    mensagem = SMTPFalso.ultima_mensagem
    assert mensagem.get_content_type() == "multipart/alternative", (
        mensagem.get_content_type()
    )
    partes = mensagem.get_payload()
    assert isinstance(partes, list), partes
    assert [parte.get_content_type() for parte in partes] == [
        "text/plain",
        "text/html",
    ], [parte.get_content_type() for parte in partes]
    assert "Texto puro do relatório" in partes[0].get_payload(decode=True).decode(
        "utf-8"
    )
    assert "<b>HTML</b>" in partes[1].get_payload(decode=True).decode("utf-8")
    print("OK multipart: relatório segue como texto puro + HTML")


def main():
    testar_env_override()
    testar_handshake_587()
    testar_handshake_465()
    testar_desconexao_no_login()
    testar_detalhe_sem_senha()
    testar_copia_email_secundario()
    testar_configuracao_ausente()
    testar_corpo_html_multipart()
    print("\nTUDO CERTO: cliente SMTP e mensagens de erro dos alertas passaram.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
