"""Preenche o endereço fiscal das lojas já cadastradas (script de manutenção).

O cadastro na ACBr exige `logradouro`, `bairro`, `codigo_municipio` (IBGE), `uf`
e `cep`. As lojas antigas foram gravadas apenas com `municipio`; este backfill
busca o restante e grava na tabela `empresas`.

Ordem das fontes:
1. BrasilAPI (`/api/cnpj/v1/{cnpj}`);
2. se a BrasilAPI estiver limitada (429) ou fora, a API pública OpenCNPJ;
3. o código IBGE é resolvido pela API de localidades do IBGE quando a fonte
   devolve apenas município/UF.

Uso, a partir da raiz do projeto:
    python backfill_enderecos_empresas.py --dry-run
    python backfill_enderecos_empresas.py
"""

import argparse
import re
import sys
import unicodedata

import requests

from backend.database import SessionLocal, init_db
from backend.models import Empresa

BRASILAPI_URL = "https://brasilapi.com.br/api/cnpj/v1/{cnpj}"
OPENCNPJ_URL = "https://api.opencnpj.org/{cnpj}"
IBGE_MUNICIPIOS_URL = (
    "https://servicodados.ibge.gov.br/api/v1/localidades/estados/{uf}/municipios"
)
CAMPOS_ENDERECO = (
    "logradouro",
    "numero",
    "complemento",
    "bairro",
    "codigo_municipio",
    "uf",
    "cep",
)


def _texto(valor) -> str:
    return str(valor or "").strip()


def _normalizar(valor) -> str:
    """Compara nomes de município sem acento/caixa/espaços duplicados."""
    texto = unicodedata.normalize("NFKD", _texto(valor))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texto).strip().upper()


def _somente_digitos(valor) -> str:
    return re.sub(r"\D", "", _texto(valor))


def _json(url: str):
    try:
        resposta = requests.get(url, timeout=30)
    except requests.RequestException:
        return None
    if not resposta.ok:
        return None
    try:
        return resposta.json()
    except ValueError:
        return None


def _dados_brasilapi(cnpj: str) -> dict:
    dados = _json(BRASILAPI_URL.format(cnpj=cnpj))
    if not isinstance(dados, dict):
        return {}
    tipo = _texto(dados.get("descricao_tipo_de_logradouro"))
    logradouro = _texto(dados.get("logradouro"))
    if tipo and logradouro and not logradouro.upper().startswith(tipo.upper()):
        logradouro = f"{tipo} {logradouro}"
    return {
        "logradouro": logradouro,
        "numero": _texto(dados.get("numero")),
        "complemento": _texto(dados.get("complemento")),
        "bairro": _texto(dados.get("bairro")),
        "codigo_municipio": _somente_digitos(
            dados.get("codigo_municipio_ibge")
        ),
        "municipio": _texto(dados.get("municipio")),
        "uf": _texto(dados.get("uf")).upper(),
        "cep": _somente_digitos(dados.get("cep")),
    }


def _dados_opencnpj(cnpj: str) -> dict:
    dados = _json(OPENCNPJ_URL.format(cnpj=cnpj))
    if not isinstance(dados, dict):
        return {}
    tipo = _texto(dados.get("tipo_logradouro"))
    logradouro = _texto(dados.get("logradouro"))
    if tipo and logradouro and not logradouro.upper().startswith(tipo.upper()):
        logradouro = f"{tipo} {logradouro}"
    return {
        "logradouro": logradouro,
        "numero": _texto(dados.get("numero")),
        "complemento": _texto(dados.get("complemento")),
        "bairro": _texto(dados.get("bairro")),
        # Nesta fonte `codigo_municipio` é SIAFI; o IBGE é resolvido abaixo.
        "codigo_municipio": "",
        "municipio": _texto(dados.get("municipio")),
        "uf": _texto(dados.get("uf")).upper(),
        "cep": _somente_digitos(dados.get("cep")),
    }


def _codigo_ibge(uf: str, municipio: str) -> str:
    """Código IBGE do município (7 dígitos) pela API de localidades."""
    if not uf or not municipio:
        return ""
    dados = _json(IBGE_MUNICIPIOS_URL.format(uf=uf.upper()))
    if not isinstance(dados, list):
        return ""
    alvo = _normalizar(municipio)
    for item in dados:
        if _normalizar(item.get("nome")) == alvo:
            return _somente_digitos(item.get("id"))
    return ""


def _buscar_endereco(cnpj: str) -> dict:
    """Tenta BrasilAPI e, se necessário, OpenCNPJ; completa o código IBGE."""
    dados = _dados_brasilapi(cnpj)
    if any(
        not dados.get(campo)
        for campo in ("logradouro", "bairro", "uf", "cep")
    ):
        alternativo = _dados_opencnpj(cnpj)
        dados = {**dados, **{k: v for k, v in alternativo.items() if v}}
    if not dados.get("codigo_municipio"):
        dados["codigo_municipio"] = _codigo_ibge(
            dados.get("uf", ""), dados.get("municipio", "")
        )
    return dados


def _endereco_completo(dados: dict) -> bool:
    return bool(
        _texto(dados.get("logradouro"))
        and _texto(dados.get("bairro"))
        and len(_somente_digitos(dados.get("codigo_municipio"))) == 7
        and len(_texto(dados.get("uf"))) == 2
        and len(_somente_digitos(dados.get("cep"))) == 8
        and _texto(dados.get("municipio"))
    )


def _aplicar(empresa: Empresa, dados: dict) -> bool:
    """Grava apenas campos não vazios; devolve True se algo mudou."""
    mudou = False
    for campo in CAMPOS_ENDERECO:
        valor = _texto(dados.get(campo))
        if not valor:
            continue
        if campo in {"codigo_municipio", "cep"}:
            valor = _somente_digitos(valor)
        elif campo == "uf":
            valor = valor.upper()
        if getattr(empresa, campo) != valor:
            setattr(empresa, campo, valor)
            mudou = True

    # O município também entra no payload da ACBr (`cidade`). Lojas antigas
    # podem estar com ele vazio; preenche apenas quando não há valor manual.
    municipio = _texto(dados.get("municipio"))
    if municipio and not _texto(empresa.municipio):
        empresa.municipio = municipio
        mudou = True
    return mudou



def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Preenche o endereço fiscal das lojas existentes."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Mostra o que seria gravado, sem alterar o banco.",
    )
    args = parser.parse_args(argv)

    init_db()
    db = SessionLocal()
    atualizadas = 0
    incompletas = 0
    try:
        empresas = db.query(Empresa).order_by(Empresa.id).all()
        for empresa in empresas:
            dados = _buscar_endereco(empresa.cnpj)
            if not _endereco_completo(dados):
                incompletas += 1
                print(
                    f"  {empresa.cnpj}: endereço não encontrado "
                    f"(BrasilAPI/OpenCNPJ); nada foi alterado."
                )
                continue
            antes = tuple(getattr(empresa, campo) for campo in CAMPOS_ENDERECO)
            mudou = _aplicar(empresa, dados)
            depois = tuple(getattr(empresa, campo) for campo in CAMPOS_ENDERECO)
            if mudou:
                atualizadas += 1
                print(f"  {empresa.cnpj}: {antes} -> {depois}")
            else:
                print(f"  {empresa.cnpj}: endereço já estava completo.")
        if args.dry_run:
            db.rollback()
            print("\n--dry-run: nada foi gravado.")
        else:
            db.commit()
            print(f"\n{atualizadas} empresa(s) atualizada(s).")
        if incompletas:
            print(f"{incompletas} empresa(s) sem endereço completo.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())

