"""Barreira do certificado digital A1 (.pfx/.p12) no vínculo de lojas.

Regra de negócio: uma loja só é vinculada à conta quando o cliente apresenta o
**certificado digital da empresa** e a **senha** dele. Este módulo concentra:

- a validação dos campos obrigatórios (arquivo e senha vazios bloqueiam o vínculo);
- a abertura do PKCS#12 com a senha informada (senha errada também bloqueia);
- a leitura dos dados públicos do certificado: titular, CNPJ e data de validade.

O que NUNCA é gravado: o arquivo .pfx/.p12 e a senha. No banco ficam apenas os dados
públicos (`empresas.certificado_titular/validade/enviado_em`). Quando a automação de
download de NFe precisar do certificado de fato, o cofre de segredos entra nessa etapa.
"""

import os
import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException

EXTENSOES_VALIDAS = (".pfx", ".p12")
TAMANHO_MINIMO_BYTES = 64
TAMANHO_MAXIMO_BYTES = 10 * 1024 * 1024  # 10 MB

# Mensagens também usadas nos testes (mantidas em constantes para não haver divergência
# entre o texto exibido no front e o do teste).
ERRO_SEM_ARQUIVO = "Envie o certificado digital da empresa (.pfx ou .p12)."
ERRO_ARQUIVO_VAZIO = "O arquivo do certificado digital está vazio."
ERRO_ARQUIVO_INVALIDO = "O arquivo enviado não parece um certificado digital (.pfx/.p12)."
ERRO_ARQUIVO_GRANDE = "O arquivo do certificado é grande demais (máximo de 10 MB)."
ERRO_EXTENSAO = "O certificado digital precisa ser um arquivo .pfx ou .p12."
ERRO_SEM_SENHA = "Informe a senha do certificado digital."
ERRO_SENHA = "Não foi possível abrir o certificado: confira a senha do arquivo .pfx/.p12."
ERRO_SEM_CERTIFICADO = "O arquivo .pfx não contém um certificado digital válido."
ERRO_VENCIDO = "Certificado vencido"
ERRO_CNPJ = "O CNPJ do certificado não corresponde ao CNPJ da empresa"
ERRO_SUPORTE = (
    "Este servidor não tem o suporte a certificados digitais instalado "
    "(pip install cryptography). O vínculo de lojas está bloqueado."
)

try:  # biblioteca padrão de PKCS#12 (cryptography). Sem ela o vínculo falha fechado.
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID

    _SUPORTE_CERTIFICADO = True
except ImportError:  # pragma: no cover - depende do ambiente
    _SUPORTE_CERTIFICADO = False

# CNPJ com ou sem máscara (o CN do e-CNPJ costuma ser "RAZAO SOCIAL:12345678000199").
PADRAO_CNPJ = re.compile(r"\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}")


def _erro(mensagem: str) -> HTTPException:
    """Erro 400 com a mensagem da barreira do certificado."""
    return HTTPException(status_code=400, detail=mensagem)


def _agora() -> datetime:
    return datetime.now(timezone.utc)


def _cnpj_do_texto(texto: str) -> Optional[str]:
    """Primeiro CNPJ (14 dígitos) encontrado no texto, com ou sem pontuação.

    O assunto do certificado e-CNPJ traz o CNPJ no CN (``RAZAO SOCIAL:12345678000199``)
    ou em outros campos, às vezes com máscara. O retorno é sempre só os 14 dígitos.
    """
    for encontrado in PADRAO_CNPJ.findall(str(texto or "")):
        digitos = re.sub(r"\D", "", encontrado)
        if len(digitos) == 14:
            return digitos
    return None


def _cnpj_do_certificado(certificado) -> Optional[str]:
    """CNPJ do titular: CN, assunto completo e ``subjectAltName`` (ICP-Brasil).

    No e-CNPJ o CNPJ pode estar no CN (``RAZAO SOCIAL:12345678000199``) ou no
    ``otherName`` OID 2.16.76.1.3.3 do ``subjectAltName``. Tentamos os dois para não
    deixar passar um certificado de outra empresa.
    """
    candidatos = [_titular_do_certificado(certificado)]
    try:
        candidatos.append(certificado.subject.rfc4514_string())
    except Exception:
        pass
    try:
        extensao = certificado.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        )
        for nome_alternativo in extensao.value:
            valor = getattr(nome_alternativo, "value", None)
            if isinstance(valor, bytes):
                # Os valores do ICP-Brasil são strings DER com os dígitos em ASCII.
                candidatos.append(valor.decode("latin-1", errors="ignore"))
            elif valor is not None:
                candidatos.append(str(valor))
    except Exception:
        pass
    for texto in candidatos:
        cnpj_encontrado = _cnpj_do_texto(texto)
        if cnpj_encontrado:
            return cnpj_encontrado
    return None


def _titular_do_certificado(certificado) -> str:
    """Nome do titular: CN do certificado (cai para o assunto completo)."""
    try:
        comuns = certificado.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        if comuns:
            return str(comuns[0].value).strip()
    except Exception:
        pass
    try:
        return str(certificado.subject.rfc4514_string()).strip()
    except Exception:
        return ""


def _validade_do_certificado(certificado) -> datetime:
    """Data/hora de validade final do certificado (ciente do fuso)."""
    validade = getattr(certificado, "not_valid_after_utc", None)
    if validade is None:  # cryptography < 42
        validade = certificado.not_valid_after.replace(tzinfo=timezone.utc)
    return validade



def validar_certificado(
    nome_arquivo: Optional[str],
    conteudo: Optional[bytes],
    senha: Optional[str],
    cnpj: Optional[str] = None,
) -> dict:
    """Barreira do certificado digital: devolve os dados públicos ou levanta 400.

    Ordem das checagens (o vínculo só acontece se todas passarem):
    1. arquivo enviado (nome + conteúdo) — vazio bloqueia;
    2. extensão .pfx/.p12 e tamanho plausível;
    3. senha informada — vazio bloqueia;
    4. abertura do PKCS#12 com a senha (senha errada bloqueia);
    5. CNPJ do certificado x CNPJ cadastrado (quando o certificado informa o CNPJ);
    6. certificado dentro da validade.
    """
    nome = os.path.basename(str(nome_arquivo or "")).strip()
    if not nome:
        raise _erro(ERRO_SEM_ARQUIVO)
    if not conteudo:
        raise _erro(ERRO_ARQUIVO_VAZIO)
    if os.path.splitext(nome)[1].lower() not in EXTENSOES_VALIDAS:
        raise _erro(ERRO_EXTENSAO)
    if len(conteudo) < TAMANHO_MINIMO_BYTES:
        raise _erro(ERRO_ARQUIVO_INVALIDO)
    if len(conteudo) > TAMANHO_MAXIMO_BYTES:
        raise _erro(ERRO_ARQUIVO_GRANDE)
    senha = str(senha or "")
    if not senha.strip():
        raise _erro(ERRO_SEM_SENHA)

    if not _SUPORTE_CERTIFICADO:
        raise HTTPException(status_code=503, detail=ERRO_SUPORTE)

    try:
        _chave, certificado, _extras = pkcs12.load_key_and_certificates(
            conteudo, senha.encode("utf-8")
        )
    except Exception:
        # Senha errada e arquivo corrompido caem aqui (ValueError na cryptography).
        # `from None`: a resposta ao cliente mostra só a mensagem da barreira.
        raise _erro(ERRO_SENHA) from None
    if certificado is None:
        raise _erro(ERRO_SEM_CERTIFICADO)

    validade = _validade_do_certificado(certificado)
    agora = _agora()

    titular = _titular_do_certificado(certificado)
    cnpj_certificado = _cnpj_do_certificado(certificado)
    digitos_cadastro = re.sub(r"\D", "", str(cnpj or ""))
    if (
        cnpj_certificado
        and len(digitos_cadastro) == 14
        and cnpj_certificado != digitos_cadastro
    ):
        raise _erro(ERRO_CNPJ)

    if validade <= agora:
        raise _erro(ERRO_VENCIDO)

    return {
        "titular": titular,
        "validade": f"{validade:%d/%m/%Y}",
        "validade_iso": validade.date().isoformat(),
        "cnpj_certificado": cnpj_certificado,
        "nome_arquivo": nome,
        "enviado_em": agora.isoformat(timespec="seconds"),
    }


def certificado_json(empresa) -> Optional[dict]:
    """Dados públicos do certificado validado (None = loja vinculada sem certificado)."""
    if not getattr(empresa, "certificado_validade", None) and not getattr(
        empresa, "certificado_titular", None
    ):
        return None
    return {
        "titular": getattr(empresa, "certificado_titular", None),
        "validade": getattr(empresa, "certificado_validade", None),
        "enviado_em": getattr(empresa, "certificado_enviado_em", None),
    }


def limpar_certificado(empresa) -> bool:
    """Apaga os dados públicos do certificado da loja ("Excluir Certificado" na tela).

    Só mexe nas três colunas de certificado: as notas, o dono e o nome da loja continuam
    intactos (o histórico da loja não depende do certificado). Devolve True quando havia
    algo para apagar — quem chama decide se responde 200 ou 400.
    """
    havia = bool(certificado_json(empresa))
    empresa.certificado_titular = None
    empresa.certificado_validade = None
    empresa.certificado_enviado_em = None
    return havia

