"""Criptografia dos segredos de integração gravados no BANCO (Omie).

O `app_secret` do aplicativo Omie é uma credencial de API: quem o tiver consegue
lançar/consultar dados fiscais em nome da loja. Ele não pode ficar em texto no
banco (um backup, um dump ou a tela de suporte não podem expor o segredo), então
as colunas ``empresas.omie_app_key`` / ``empresas.omie_app_secret`` guardam o
texto cifrado com **Fernet** (AES-128-CBC + HMAC-SHA256, autenticado).

Chave de criptografia (a mesma para todas as leituras/gravações do processo):

1. ``OMIE_ENC_KEY`` — variável dedicada, recomendada quando se quer separar a
   chave da integração do segredo dos tokens (permite rotacionar uma sem a outra);
2. ``NFE_SECRET_KEY`` — o mesmo segredo já usado para assinar os tokens
   (backend/auth.py), para não obrigar uma configuração nova em instalações
   existentes;
3. arquivo ``.secret_key`` da raiz do projeto — criado pelo primeiro start da API
   quando o ``NFE_SECRET_KEY`` não existe. Neste módulo ele é apenas LIDO: gerar
   um segredo novo aqui faria os dados cifrados ficarem ilegíveis quando a API
   gravasse outro.

O segredo (texto) nunca é usado direto como chave do Fernet: ele passa por
PBKDF2-HMAC-SHA256 (32 bytes de saída, sal fixo do projeto) — assim uma senha
curta não vira uma chave fraca e o formato da chave é sempre o que o Fernet
espera (32 bytes em base64 url-safe).

Formato gravado: ``fernet:v1:<token>``. O prefixo serve para (a) reconhecer o
que já está cifrado (idempotência de migração/re-gravação) e (b) devolver como
está um valor antigo em texto puro, sem quebrar a leitura. Quando a chave muda,
o Fernet levanta ``InvalidToken`` e o erro é traduzido em
:class:`ErroCredenciais` com instrução de como re-gravar — nenhum segredo é
devolvido pela metade.
"""

from __future__ import annotations

import base64
import os
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parents[1]
# O .env da raiz é lido antes de qualquer os.getenv; `override=False` preserva o
# valor injetado pelo serviço de nuvem, que tem precedência sobre o arquivo.
load_dotenv(RAIZ / ".env", override=False)

ARQUIVO_SEGREDO = RAIZ / ".secret_key"
VARIAVEIS_CHAVE = ("OMIE_ENC_KEY", "NFE_SECRET_KEY")
TAMANHO_MINIMO_SEGREDO = 32
# Sal fixo e versão do formato: mudar qualquer um dos dois invalida o que já está
# gravado, então os dois andam juntos com o prefixo PREFIXO.
SAL = b"sistema_nfe/credenciais/omie/v1"
ITERACOES = 200_000
PREFIXO = "fernet:v1:"


class ErroCredenciais(RuntimeError):
    """Chave de criptografia ausente/errada ao ler ou gravar um segredo."""


def _segredo() -> str:
    """Segredo-base da criptografia (OMIE_ENC_KEY > NFE_SECRET_KEY > .secret_key).

    Lido a cada chamada (com cache por valor em :func:`_chave`) para que um teste
    ou uma troca de variável no processo seja respeitada sem reiniciar nada.
    """
    for nome in VARIAVEIS_CHAVE:
        valor = (os.getenv(nome) or "").strip()
        if len(valor) >= TAMANHO_MINIMO_SEGREDO:
            return valor

    try:
        guardado = ARQUIVO_SEGREDO.read_text(encoding="utf-8").strip()
    except OSError:
        guardado = ""
    if len(guardado) >= TAMANHO_MINIMO_SEGREDO:
        return guardado

    raise ErroCredenciais(
        "Chave de criptografia ausente: defina OMIE_ENC_KEY (ou NFE_SECRET_KEY) "
        f"com pelo menos {TAMANHO_MINIMO_SEGREDO} caracteres no .env, ou deixe a "
        f"API gerar o {ARQUIVO_SEGREDO.name} no primeiro start."
    )


@lru_cache(maxsize=8)
def _chave(segredo: str) -> str:
    """Chave Fernet (base64 url-safe) derivada do segredo por PBKDF2-HMAC-SHA256."""
    derivada = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=SAL,
        iterations=ITERACOES,
    ).derive(segredo.encode("utf-8"))
    return base64.urlsafe_b64encode(derivada).decode("ascii")



def cifrado(valor: str) -> bool:
    """True quando o valor já está no formato gravado por este módulo."""
    return str(valor or "").startswith(PREFIXO)


def criptografar(texto: str) -> str:
    """Cifra um segredo para ser gravado no banco (``""`` continua ``""``)."""
    texto = str(texto or "").strip()
    if not texto:
        return ""
    if cifrado(texto):
        return texto  # já cifrado: re-gravar não cria uma camada nova
    token = Fernet(_chave(_segredo())).encrypt(texto.encode("utf-8"))
    return PREFIXO + token.decode("ascii")


def descriptografar(valor: str) -> str:
    """Devolve o segredo em texto puro.

    Valor vazio devolve ``""``. Valor sem o prefixo é tratado como texto puro
    (registro antigo/semeado à mão) e volta como veio. Token inválido — banco
    copiado para outro ambiente ou chave rotacionada — vira
    :class:`ErroCredenciais` com a instrução de regravar a credencial; assim a
    integração falha de forma explícita, em vez de chamar a API com lixo.
    """
    valor = str(valor or "").strip()
    if not valor or not cifrado(valor):
        return valor
    token = valor[len(PREFIXO):]
    try:
        return Fernet(_chave(_segredo())).decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, UnicodeDecodeError) as exc:
        raise ErroCredenciais(
            "Não foi possível decifrar a credencial Omie gravada: a chave "
            "(OMIE_ENC_KEY/NFE_SECRET_KEY/.secret_key) mudou desde a gravação. "
            "Regrave as credenciais da loja (testar_omie_sync.py "
            "--gravar-credenciais)."
        ) from exc


def mascarar_segredo(valor: str, visiveis: int = 4) -> str:
    """Segredo para log/terminal: só os últimos caracteres, o resto em ``*``."""
    texto = str(valor or "")
    if not texto:
        return ""
    visiveis = max(0, int(visiveis))
    if len(texto) <= visiveis:
        return "*" * len(texto)
    return "*" * (len(texto) - visiveis) + texto[-visiveis:]
