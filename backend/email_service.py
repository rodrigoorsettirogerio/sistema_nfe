"""Envio de e-mails dos alertas de preço via SMTP.

As credenciais ficam somente no `.env`/ambiente do processo: nada é gravado no banco
nem devolvido pela API. A senha é normalizada sem espaços porque as "Senhas de App"
do Gmail costumam ser exibidas em blocos (``abcd efgh ijkl mnop``).

O chamador (``backend/main.py``) decide o destinatário a partir da configuração da
conta e traduz as exceções SMTP para respostas HTTP amigáveis.
"""

import os
import re
import smtplib
import ssl
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import dotenv_values

CAMINHO_ENV = Path(__file__).resolve().parents[1] / ".env"


def recarregar_configuracao_smtp(caminho=None) -> None:
    """(Re)carrega as chaves ``SMTP_*`` do ``.env`` com prioridade sobre o ambiente.

    O ``.env`` é a fonte da verdade apenas para o SMTP: sem essa prioridade, um
    ``SMTP_PASSWORD`` antigo exportado no terminal/IDE vence o valor novo do
    arquivo e a API tenta autenticar com a senha errada (o Gmail costuma
    responder encerrando a conexão). As demais variáveis (ACBr, DeepSeek, banco)
    NÃO são sobrescritas — assim um teste ou serviço que injeta
    ``ACBR_API_URL``/``ACBR_AUTH_URL`` no processo não é desviado para o valor
    do ``.env``. O caminho é absoluto para funcionar mesmo quando a API é
    iniciada de outra pasta de trabalho.

    A rota de envio chama esta função antes de cada tentativa, então alterar o
    ``.env`` passa a valer no próximo clique, sem reiniciar a API.
    """
    valores = dotenv_values(caminho or CAMINHO_ENV)
    for chave, valor in valores.items():
        if chave.startswith("SMTP_") and valor is not None:
            os.environ[chave] = valor


# Garante o `.env` correto já na importação (scripts, testes e processo da API).
recarregar_configuracao_smtp()

PORTA_PADRAO = 587
PORTA_SSL_PADRAO = 465
TIMEOUT_PADRAO = 20
NOME_REMETENTE_PADRAO = "Inteligência de Custos NF-e"
LIMITE_DETALHE_ERRO = 400


def _senha_sem_espacos(valor: str) -> str:
    """Remove todo espaço em branco da Senha de App (Gmail mostra em blocos)."""
    return re.sub(r"\s+", "", str(valor or ""))


def _timeout_smtp() -> float:
    """Timeout de conexão/leitura em segundos (SMTP_TIMEOUT, padrão 20)."""
    texto = (os.getenv("SMTP_TIMEOUT") or "").strip()
    if not texto:
        return float(TIMEOUT_PADRAO)
    try:
        timeout = float(texto)
    except ValueError as erro:
        raise ValueError("SMTP_TIMEOUT inválido: informe um número de segundos.") from erro
    if timeout <= 0:
        raise ValueError("SMTP_TIMEOUT deve ser maior que zero.")
    return timeout


def _limpar_detalhe(texto: object) -> str:
    """Resumo curto e sem credenciais para exibir na tela/log."""
    detalhe = re.sub(r"\s+", " ", str(texto or "")).strip()
    # Nunca devolve a Senha de App, mesmo que algum servidor a ecoe por engano.
    for segredo in {
        (os.getenv("SMTP_PASSWORD") or "").strip(),
        _senha_sem_espacos(os.getenv("SMTP_PASSWORD") or ""),
    }:
        if segredo:
            detalhe = detalhe.replace(segredo, "***")
    if len(detalhe) > LIMITE_DETALHE_ERRO:
        detalhe = detalhe[:LIMITE_DETALHE_ERRO].rstrip() + "…"
    return detalhe or "sem detalhes adicionais"


def detalhe_erro_smtp(erro: BaseException) -> str:
    """Mensagem técnica segura da falha SMTP (código e resposta do servidor).

    O ``main.py`` concatena este resumo ao texto amigável da resposta HTTP para que
    o Streamlit mostre a causa real (ex.: 535 senha recusada, timeout, TLS etc.)
    sem expor a senha configurada no ``.env``.
    """
    if isinstance(erro, smtplib.SMTPResponseException):
        partes = [type(erro).__name__, f"código {erro.smtp_code}"]
        resposta = erro.smtp_error
        if isinstance(resposta, bytes):
            resposta = resposta.decode("utf-8", "replace")
        if resposta:
            partes.append(str(resposta))
        return _limpar_detalhe(" — ".join(partes))
    texto = _limpar_detalhe(erro)
    return _limpar_detalhe(f"{type(erro).__name__}: {texto}")


def _envio_desativado() -> bool:
    """True quando ``NFE_SMTP_DESATIVADO=1`` (testes/desenvolvimento sem abrir socket).

    Os testes end-to-end sobem a API de verdade: sem este flag o cadastro tentaria
    autenticar no Gmail real só para enviar um código a um endereço de mentira.
    """
    return (os.getenv("NFE_SMTP_DESATIVADO") or "").strip().lower() in {
        "1",
        "true",
        "sim",
        "yes",
    }


def _fechar_servidor(servidor) -> None:
    """Encerra a sessão sem mascarar a exceção original do envio."""
    try:
        servidor.quit()
    except Exception:
        try:
            servidor.close()
        except Exception:
            pass


def _autenticar(servidor, usuario: str, senha: str) -> None:
    """Faz o LOGIN tratando o Gmail que desconecta em vez de devolver 535.

    Em algumas contas/redes o Gmail encerra a conexão logo após receber a Senha de
    App inválida, sem enviar a resposta 535. Sem esta tradução o usuário veria apenas
    ``SMTPServerDisconnected``, sem saber que precisa conferir a senha.
    """
    try:
        servidor.login(usuario, senha)
    except smtplib.SMTPAuthenticationError:
        raise
    except smtplib.SMTPServerDisconnected as erro:
        raise smtplib.SMTPAuthenticationError(
            535,
            (
                "O servidor encerrou a conexão durante o LOGIN; confira "
                "SMTP_USER e a Senha de App (Gmail)."
            ).encode("utf-8"),
        ) from erro


def send_email(
    to_email: str,
    subject: str,
    body: str,
    cc_email: str = "",
    html_body: str = "",
) -> bool:
    """Envia um e-mail UTF-8 e devolve ``True`` quando concluído.

    ``body`` é sempre a versão em texto puro. Quando ``html_body`` é informado, a
    mensagem vira ``multipart/alternative`` com as duas versões: clientes que abrem
    HTML (Gmail/Outlook) veem o relatório formatado; os demais (e o próprio texto do
    alerta antigo) continuam recebendo o conteúdo legível.

    ``cc_email`` é opcional: quando informado (e diferente do destinatário), entra no
    cabeçalho ``Cc`` — é assim que o e-mail secundário dos alertas recebe uma cópia.
    A porta 587 (Gmail) usa o handshake explícito exigido pelo servidor:
    ``EHLO -> STARTTLS -> EHLO -> LOGIN``. A porta 465 usa SSL direto.
    Levanta ``ValueError`` quando a configuração está ausente/inválida e propaga
    ``SMTPAuthenticationError``/``SMTPException``/``OSError`` para o chamador
    distinguir falha de autenticação, conexão e envio. Com ``NFE_SMTP_DESATIVADO=1``
    levanta ``ValueError`` antes de qualquer conexão (ambiente de teste).
    """
    if _envio_desativado():
        raise ValueError("Envio de e-mail desativado (NFE_SMTP_DESATIVADO=1).")
    host = (os.getenv("SMTP_HOST") or "").strip()
    porta_texto = (os.getenv("SMTP_PORT") or "").strip()
    usuario = (os.getenv("SMTP_USER") or "").strip()
    senha = _senha_sem_espacos(os.getenv("SMTP_PASSWORD") or "")
    nome_remetente = (os.getenv("SMTP_FROM_NAME") or "").strip() or NOME_REMETENTE_PADRAO

    if not host:
        raise ValueError("SMTP_HOST precisa estar configurado no ambiente.")
    if not usuario or not senha:
        raise ValueError(
            "SMTP_USER e SMTP_PASSWORD precisam estar configurados no ambiente."
        )

    destino = str(to_email or "").strip()
    if not destino:
        raise ValueError("Destinatário do e-mail não informado.")

    try:
        porta = int(porta_texto) if porta_texto else PORTA_PADRAO
    except ValueError as erro:
        raise ValueError("SMTP_PORT inválido: informe um número de porta.") from erro
    if not 1 <= porta <= 65535:
        raise ValueError("SMTP_PORT inválido: use uma porta entre 1 e 65535.")
    timeout = _timeout_smtp()

    mensagem = MIMEText(str(body or ""), "plain", "utf-8")
    if str(html_body or "").strip():
        # multipart/alternative: a última parte (HTML) é a preferida pelos clientes.
        mensagem = MIMEMultipart("alternative")
        mensagem.attach(MIMEText(str(body or ""), "plain", "utf-8"))
        mensagem.attach(MIMEText(str(html_body), "html", "utf-8"))
    mensagem["Subject"] = Header(str(subject or ""), "utf-8")
    mensagem["From"] = Header(f"{nome_remetente} <{usuario}>", "utf-8")
    mensagem["To"] = destino
    copia = str(cc_email or "").strip()
    if copia and copia.lower() != destino.lower():
        mensagem["Cc"] = copia

    contexto = ssl.create_default_context()
    if porta == PORTA_SSL_PADRAO:
        servidor = smtplib.SMTP_SSL(
            host, porta, timeout=timeout, context=contexto
        )
        try:
            servidor.ehlo()
            _autenticar(servidor, usuario, senha)
            servidor.send_message(mensagem)
        finally:
            _fechar_servidor(servidor)
    else:
        servidor = smtplib.SMTP(host, porta, timeout=timeout)
        try:
            servidor.ehlo()
            servidor.starttls(context=contexto)
            servidor.ehlo()
            _autenticar(servidor, usuario, senha)
            servidor.send_message(mensagem)
        finally:
            _fechar_servidor(servidor)

    return True
