"""Descobre o último NSU da ACBr a partir dos XMLs manuais antigos.

A Distribuição NF-e da ACBr avança por NSU. Quando o cliente já tem XMLs antigos,
o primeiro download automático não deve recomeçar uma janela longa (padrão 7
dias): ele precisa continuar do ponto em que os arquivos manuais pararam. Este
script lê todos os
``.xml`` guardados em ``xmls/`` (inclusive subpastas), procura o atributo ``NSU``
da tag ``<nfeProc>``/raiz/distribuição e grava o MAIOR NSU de cada CNPJ
destinatário em ``empresas.ultimo_nsu_acbr``.

XMLs completos de NF-e (``nfeProc``) normalmente NÃO trazem o NSU — ele existe só
nos XMLs de distribuição. Nesses casos o script avisa no terminal e mostra o SQL
para o usuário informar o NSU manualmente no banco.

Uso (na raiz do projeto):
    python atualizar_nsu_manual.py              # lê, mostra e grava o maior NSU
    python atualizar_nsu_manual.py --simular    # só mostra, sem gravar
    python atualizar_nsu_manual.py --pasta outra/pasta
"""

from __future__ import annotations

import argparse
import base64
import gzip
import re
import sys
from pathlib import Path
from typing import Iterable

import xml.etree.ElementTree as ET

from backend.database import SessionLocal, init_db
from backend.models import Empresa

PASTA_PADRAO = Path(__file__).resolve().parent / "xmls"


def _nome_local(tag) -> str:
    """Nome da tag/atributo sem o namespace (``{...}nfeProc`` -> ``NFEPROC``)."""
    return str(tag or "").rsplit("}", 1)[-1].upper()


def _normalizar_cnpj(valor) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def _normalizar_nsu(valor) -> str:
    """NSU como texto numérico, sem zeros à esquerda; vazio quando inválido."""
    texto = str(valor or "").strip()
    if not texto:
        return ""
    digitos = re.sub(r"\D", "", texto)
    return str(int(digitos)) if digitos else ""


def _nsu_int(valor) -> int:
    normalizado = _normalizar_nsu(valor)
    return int(normalizado) if normalizado else 0


def _nsu_do_elemento(elemento) -> str:
    """Valor do atributo ``NSU`` (sem diferenciar maiúsculas/namespace)."""
    for chave, valor in elemento.attrib.items():
        if _nome_local(chave) == "NSU":
            normalizado = _normalizar_nsu(valor)
            if normalizado:
                return normalizado
    return ""


def _extrair_nsu(root) -> str:
    """Maior NSU encontrado no XML (atributo da raiz, nfeProc, docZip ou tag NSU)."""
    candidatos = []

    def adicionar(valor):
        normalizado = _normalizar_nsu(valor)
        if normalizado:
            candidatos.append(int(normalizado))

    adicionar(_nsu_do_elemento(root))
    for elemento in root.iter():
        nome = _nome_local(elemento.tag)
        if nome in {"NFEPROC", "DOCZIP"}:
            adicionar(_nsu_do_elemento(elemento))
        elif nome == "NSU" and elemento.text:
            adicionar(elemento.text)
    return str(max(candidatos)) if candidatos else ""


def _cnpjs_destinatarios(root) -> set:
    """CNPJs do grupo ``dest`` (destinatário) do XML."""
    cnpjs = set()
    for elemento in root.iter():
        if _nome_local(elemento.tag) not in {"DEST", "DESTINATARIO"}:
            continue
        for filho in elemento:
            if _nome_local(filho.tag) == "CNPJ":
                cnpj = _normalizar_cnpj(filho.text)
                if len(cnpj) == 14:
                    cnpjs.add(cnpj)
    return cnpjs


def _raizes_embutidas(root) -> Iterable:
    """XMLs internos dos ``docZip`` (base64 + gzip) de um retorno de distribuição."""
    for elemento in root.iter():
        if _nome_local(elemento.tag) != "DOCZIP" or not elemento.text:
            continue
        try:
            conteudo = base64.b64decode(elemento.text.strip(), validate=False)
            if conteudo[:2] == b"\x1f\x8b":
                conteudo = gzip.decompress(conteudo)
            yield ET.fromstring(conteudo)
        except (ValueError, OSError, EOFError, ET.ParseError):
            continue


def _ler_dados_xml(caminho: Path) -> tuple:
    """Devolve ``(maior_nsu, cnpjs_destinatarios)`` de um arquivo XML."""
    root = ET.parse(caminho).getroot()
    raizes = [root, *_raizes_embutidas(root)]
    maior_nsu = 0
    cnpjs = set()
    for raiz in raizes:
        maior_nsu = max(maior_nsu, _nsu_int(_extrair_nsu(raiz)))
        cnpjs.update(_cnpjs_destinatarios(raiz))
    return (str(maior_nsu) if maior_nsu else "", cnpjs)


def _formatar_cnpj(cnpj) -> str:
    digitos = _normalizar_cnpj(cnpj)
    if len(digitos) != 14:
        return str(cnpj or "")
    return f"{digitos[:2]}.{digitos[2:5]}.{digitos[5:8]}/{digitos[8:12]}-{digitos[12:]}"


def _nome_loja(empresa: Empresa | None, cnpj: str) -> str:
    if empresa is None:
        return "loja não cadastrada"
    return empresa.nome_loja or empresa.razao_social or f"Loja {cnpj}"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Lê os XMLs antigos de xmls/ e grava em empresas.ultimo_nsu_acbr "
            "o maior NSU de cada CNPJ destinatário."
        )
    )
    parser.add_argument(
        "--pasta",
        default=str(PASTA_PADRAO),
        help=f"pasta dos XMLs (padrão: {PASTA_PADRAO})",
    )
    parser.add_argument(
        "--simular",
        "--dry-run",
        action="store_true",
        dest="simular",
        help="só mostra o que seria gravado (não altera o banco)",
    )
    argumentos = parser.parse_args(argv)

    pasta = Path(argumentos.pasta).expanduser().resolve()
    if not pasta.is_dir():
        print(f"Pasta não encontrada: {pasta}")
        return 1
    arquivos = sorted(pasta.rglob("*.xml"))
    if not arquivos:
        print(f"Nenhum arquivo .xml encontrado em {pasta}")
        return 0

    # CNPJ -> {"maior": int, "arquivos": int}; o maior NSU vence ao final.
    encontrados = {}
    arquivos_sem_nsu = {}
    arquivos_sem_cnpj = 0
    erros = []
    for caminho in arquivos:
        try:
            nsu, cnpjs = _ler_dados_xml(caminho)
        except (ET.ParseError, OSError) as erro:
            erros.append((caminho, erro))
            continue
        if not cnpjs:
            arquivos_sem_cnpj += 1
            continue
        for cnpj in cnpjs:
            if not nsu:
                arquivos_sem_nsu[cnpj] = arquivos_sem_nsu.get(cnpj, 0) + 1
                continue
            dados = encontrados.setdefault(cnpj, {"maior": 0, "arquivos": 0})
            dados["maior"] = max(dados["maior"], _nsu_int(nsu))
            dados["arquivos"] += 1

    init_db()
    db = SessionLocal()
    try:
        empresas = db.query(Empresa).order_by(Empresa.id).all()
        por_cnpj = {_normalizar_cnpj(empresa.cnpj): empresa for empresa in empresas}

        print(f"Lidos {len(arquivos)} arquivo(s) XML em {pasta}\n")

        atualizadas = 0
        if encontrados:
            print("Maior NSU encontrado por loja:")
            for cnpj, dados in sorted(encontrados.items()):
                empresa = por_cnpj.get(cnpj)
                atual = _nsu_int(empresa.ultimo_nsu_acbr) if empresa else 0
                if empresa is None:
                    print(
                        f"  {_formatar_cnpj(cnpj)}  {_nome_loja(None, cnpj)}  "
                        f"maior NSU {dados['maior']} — NÃO atualizado"
                    )
                    continue
                if dados["maior"] <= atual:
                    print(
                        f"  {_formatar_cnpj(cnpj)}  {_nome_loja(empresa, cnpj)}  "
                        f"já está em {atual} (maior encontrado: {dados['maior']})"
                    )
                    continue
                if not argumentos.simular:
                    empresa.ultimo_nsu_acbr = str(dados["maior"])
                    atualizadas += 1
                print(
                    f"  {_formatar_cnpj(cnpj)}  {_nome_loja(empresa, cnpj)}  "
                    f"{atual} -> {dados['maior']} "
                    f"({dados['arquivos']} XML(s) com NSU)"
                )
        else:
            print("Nenhum NSU foi encontrado em nenhum XML.")

        sem_nsu_sem_fonte = sorted(
            cnpj for cnpj in arquivos_sem_nsu if cnpj not in encontrados
        )
        if sem_nsu_sem_fonte:
            print(
                "\n⚠️  XMLs sem NSU (NF-e completa normalmente não guarda "
                "esse dado):"
            )
            for cnpj in sem_nsu_sem_fonte:
                empresa = por_cnpj.get(cnpj)
                print(
                    f"  {_formatar_cnpj(cnpj)}  {_nome_loja(empresa, cnpj)}  "
                    f"{arquivos_sem_nsu[cnpj]} arquivo(s) sem atributo NSU"
                )
            print(
                "\n  Informe o último NSU manualmente no banco (SQLite) com um "
                "comando assim:"
            )
            for cnpj in sem_nsu_sem_fonte:
                print(
                    f"    UPDATE empresas SET ultimo_nsu_acbr = "
                    f"'COLOQUE_O_NSU_AQUI' WHERE cnpj = '{cnpj}';"
                )
            print(
                "  Use o maior NSU que a ACBr já tenha devolvido para essa loja. "
                "Depois disso, o próximo download continua a partir dele."
            )

        if arquivos_sem_cnpj:
            print(
                f"\n⚠️  {arquivos_sem_cnpj} arquivo(s) sem CNPJ de destinatário "
                "legível; não foi possível associá-los a uma loja."
            )
        if erros:
            print(f"\n⚠️  {len(erros)} arquivo(s) não puderam ser lidos:")
            for caminho, erro in erros[:10]:
                print(f"  {caminho}: {erro}")
            if len(erros) > 10:
                print(f"  ... e mais {len(erros) - 10}.")

        if argumentos.simular:
            print(
                "\nSimulação: nada foi gravado. "
                "Rode sem --simular para aplicar."
            )
            return 0

        db.commit()
        if atualizadas:
            print(
                f"\n{atualizadas} loja(s) atualizada(s) em "
                "empresas.ultimo_nsu_acbr."
            )
        elif encontrados:
            print("\nNenhuma loja precisou de alteração.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())

