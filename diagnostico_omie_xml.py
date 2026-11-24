"""Diagnóstico pontual do Omie: onde está o XML/itens das NF-e de entrada.

Somente leitura: não grava no banco, não altera o cliente e não imprime
segredos (``OMIE_APP_SECRET`` nunca aparece; a app_key sai mascarada).

Uso:
    python diagnostico_omie_xml.py
    python diagnostico_omie_xml.py --dias 15 --chave <44 digitos>

Grava dumps JSON completos e um relatório em ``%TEMP%\\omie_diag\\`` para
inspeção, além de imprimir o resumo no console.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(RAIZ / ".env", override=False)

from backend.omie_client import (  # noqa: E402
    URL_DOCUMENTOS,
    URL_RECEBIMENTOS,
    ErroOmie,
    OmieClient,
)

URL_NFCONSULTAR = "https://app.omie.com.br/api/v1/produtos/nfconsultar/"
PASTA = Path(tempfile.gettempdir()) / "omie_diag"
RELATORIO: list = []


def secao(titulo: str) -> None:
    """Imprime e registra um cabeçalho de seção no relatório."""
    linha = f"\n=== {titulo} ==="
    print(linha)
    RELATORIO.append(linha)


def registrar(texto: str = "") -> None:
    """Imprime uma linha e guarda no relatório (mesmo conteúdo)."""
    print(texto)
    RELATORIO.append(texto)


def mascarar(texto: str) -> str:
    """Remove qualquer ocorrência do app_secret de um texto."""
    segredo = getattr(mascarar, "_segredo", "")
    if segredo and segredo in texto:
        texto = texto.replace(segredo, "***")
    return texto


def salvar_json(nome: str, dados) -> Path:
    """Grava o dump completo (sem segredos) e devolve o caminho."""
    PASTA.mkdir(parents=True, exist_ok=True)
    arquivo = PASTA / f"{nome}.json"
    arquivo.write_text(
        json.dumps(dados, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return arquivo


def achatar(dados, prefixo: str = "", nivel: int = 0, limite: int = 4) -> list:
    """Lista ``caminho = valor`` de todos os campos escalares do retorno."""
    linhas: list = []
    if isinstance(dados, dict):
        for chave, valor in dados.items():
            caminho = f"{prefixo}.{chave}" if prefixo else str(chave)
            if isinstance(valor, (dict, list)):
                if nivel < limite:
                    linhas.extend(achatar(valor, caminho, nivel + 1, limite))
                else:
                    linhas.append(f"{caminho} = <{type(valor).__name__}>")
            else:
                texto = valor if isinstance(valor, str) else repr(valor)
                if len(texto) > 160:
                    texto = f"{texto[:120]}... ({len(texto)} chars)"
                linhas.append(f"{caminho} = {texto}")
    elif isinstance(dados, list):
        linhas.append(f"{prefixo} = lista com {len(dados)} item(ns)")
        for indice, item in enumerate(dados[:2]):
            if nivel < limite:
                linhas.extend(
                    achatar(item, f"{prefixo}[{indice}]", nivel + 1, limite)
                )
    else:
        linhas.append(f"{prefixo} = {dados!r}")
    return linhas


def campos_xml(dados, prefixo: str = "") -> list:
    """Caminhos de campos cujo nome contém ``xml`` (cXml, cXmlDistribuicao...)."""
    achados: list = []
    if isinstance(dados, dict):
        for chave, valor in dados.items():
            caminho = f"{prefixo}.{chave}" if prefixo else str(chave)
            if "xml" in str(chave).lower():
                tamanho = len(valor) if isinstance(valor, str) else -1
                achados.append(f"{caminho} ({tamanho} chars)")
            achados.extend(campos_xml(valor, caminho))
    elif isinstance(dados, list):
        for indice, item in enumerate(dados):
            achados.extend(campos_xml(item, f"{prefixo}[{indice}]"))
    return achados


def chamar(cliente: OmieClient, url: str, call: str, param: dict):
    """Chama o Omie devolvendo ``(ok, corpo_ou_erro)``, sem derrubar o script."""
    time.sleep(0.4)  # o Omie limita a taxa (~1 req/s) e devolve 429 se abusar
    try:
        return True, cliente._chamar(url, call, param)
    except ErroOmie as erro:
        return False, erro


def mostrar_resultado(rotulo: str, sucesso: bool, resultado) -> None:
    """Mostra sucesso (contagem + campos) ou o erro do Omie mascarado."""
    if not sucesso:
        detalhe = mascarar(str(resultado))
        codigo = getattr(resultado, "codigo", "") or ""
        status = getattr(resultado, "status", None)
        registrar(
            f"[{rotulo}] ERRO (código={codigo or '-'}, HTTP={status}): {detalhe}"
        )
        return
    corpo = resultado if isinstance(resultado, dict) else {"retorno": resultado}
    registrar(f"[{rotulo}] OK — chaves de 1º nível: {sorted(corpo)}")
    for caminho in campos_xml(corpo)[:12]:
        registrar(f"[{rotulo}] campo de XML: {caminho}")


def _primeiro(dados: dict, *chaves) -> str:
    """Primeiro valor preenchido entre os nomes candidatos."""
    for chave in chaves:
        valor = dados.get(chave)
        if valor not in (None, ""):
            return str(valor).strip()
    return ""


def _achar_chave(dados, chave: str):
    """Primeiro valor encontrado para ``chave`` em qualquer nível do retorno."""
    if isinstance(dados, dict):
        if dados.get(chave) not in (None, ""):
            return dados[chave]
        for valor in dados.values():
            achado = _achar_chave(valor, chave)
            if achado is not None:
                return achado
    elif isinstance(dados, list):
        for item in dados:
            achado = _achar_chave(item, chave)
            if achado is not None:
                return achado
    return None


def secao_recebimentos(cliente: OmieClient, dias: int, chave_pedida: str):
    """Item 1: lista, escolhe uma nota e detalha com ``ConsultarRecebimento``."""
    secao(f"1) ListarRecebimentos (últimos {dias} dias) + ConsultarRecebimento")
    hoje = date.today()
    inicio = hoje - timedelta(days=max(1, dias) - 1)
    registrar(f"Período: {inicio:%d/%m/%Y} a {hoje:%d/%m/%Y}")
    recebimentos = cliente.listar_todos_recebimentos(inicio, hoje)
    registrar(f"Recebimentos retornados: {len(recebimentos)}")
    salvar_json("01_listar_recebimentos", recebimentos)
    if not recebimentos:
        registrar("Lista vazia: nada a detalhar.")
        return {}

    alvo = recebimentos[0]
    for nota in recebimentos:
        cabec = nota.get("cabec") if isinstance(nota.get("cabec"), dict) else nota
        chave = _primeiro(cabec, "cChaveNfe", "cChaveNFe", "cChave")
        if chave_pedida and chave == chave_pedida:
            alvo = nota
            break
        if not chave_pedida and _achar_chave(nota, "nIdReceb") is not None:
            alvo = nota
            break

    cabec = alvo.get("cabec") if isinstance(alvo.get("cabec"), dict) else alvo
    nid = _achar_chave(alvo, "nIdReceb")
    chave = _primeiro(cabec, "cChaveNfe", "cChaveNFe", "cChave") or chave_pedida
    numero = _primeiro(cabec, "cNumeroNFe", "nNumeroNF", "cNumeroNF", "nNumero")
    serie = _primeiro(cabec, "cSerie", "serie", "cSerieNFe")
    registrar(
        f"Alvo: nIdReceb={nid} | número={numero or '-'} | série={serie or '-'} "
        f"| chave={chave or '-'}"
    )

    param = {"nIdReceb": nid} if nid is not None else {"cChaveNfe": chave}
    pedido = dict(param, cExibirDetalhes="S")
    sucesso, resposta = chamar(
        cliente, URL_RECEBIMENTOS, "ConsultarRecebimento", pedido
    )
    if not sucesso:
        registrar(
            "ConsultarRecebimento não aceita cExibirDetalhes "
            "(campo inexistente em rcbtoConsultarRequest); repetindo sem ele."
        )
        sucesso, resposta = chamar(
            cliente, URL_RECEBIMENTOS, "ConsultarRecebimento", param
        )
    mostrar_resultado("ConsultarRecebimento", sucesso, resposta)
    if not sucesso:
        return {"nIdReceb": nid, "chave": chave, "numero": numero, "serie": serie}

    arquivo = salvar_json("02_consultar_recebimento", resposta)
    registrar(f"Dump completo: {arquivo}")
    registrar("Campos escalares (todos os níveis):")
    for linha in achatar(resposta, limite=6):
        registrar(f"  {linha}")
    xml = campos_xml(resposta)
    registrar(f"Campos contendo 'xml': {xml or 'NENHUM'}")

    itens = _achar_chave(resposta, "itensRecebimento") or []
    registrar(f"itensRecebimento: {len(itens)} item(ns)")
    for indice, item in enumerate(itens[:5]):
        if not isinstance(item, dict):
            registrar(f"  item {indice}: {item!r}")
            continue
        # Na Omie o produto/quantidade/preço ficam em ``itensCabec``.
        cab_item = item.get("itensCabec")
        if not isinstance(cab_item, dict):
            cab_item = item
        resumo = {
            campo: cab_item[campo]
            for campo in (
                "nSequencia",
                "cCodigoProduto",
                "cProduto",
                "cDescricaoProduto",
                "cDescricao",
                "cUnidadeNfe",
                "nQtdeNFe",
                "nQtde",
                "nPrecoUnit",
                "nValorUnitario",
                "vTotalItem",
                "nValorTotal",
                "cNCM",
                "cEAN",
                "cCFOP",
            )
            if cab_item.get(campo) not in (None, "")
        }
        registrar(f"  item {indice}: {json.dumps(resumo, ensure_ascii=False)}")
    if itens and isinstance(itens[0], dict):
        registrar("  campos do item 0: " + ", ".join(sorted(itens[0])))
        cab_item = itens[0].get("itensCabec")
        if isinstance(cab_item, dict):
            registrar(
                "  campos de itensCabec: " + ", ".join(sorted(cab_item))
            )

    return {
        "nIdReceb": nid,
        "chave": chave,
        "numero": numero,
        "serie": serie,
        "consulta": resposta,
    }


def _primeira_lista(corpo: dict):
    """Primeira lista de dicionários do retorno (nome varia por endpoint)."""
    for valor in corpo.values():
        if isinstance(valor, list) and valor and isinstance(valor[0], dict):
            return valor
    return []


def secao_documentos(cliente: OmieClient) -> None:
    """Itens 2 e 3: ``ListarDocumentos`` sem data, entrada x saída e afins."""
    secao("2/3) ListarDocumentos (contador/xml) — variações de filtro")
    hoje = date.today()
    variantes = [
        (
            "cModelo=55 cOperacao=0 SEM datas",
            {"nPagina": 1, "nRegPorPagina": 20, "cModelo": "55", "cOperacao": "0"},
        ),
        (
            "cModelo=55 cOperacao=1 (Saída) SEM datas",
            {"nPagina": 1, "nRegPorPagina": 20, "cModelo": "55", "cOperacao": "1"},
        ),
        (
            "cModelo=55 SEM cOperacao SEM datas",
            {"nPagina": 1, "nRegPorPagina": 20, "cModelo": "55"},
        ),
        (
            "cModelo=55 cOperacao=0 cAmbiente=H",
            {
                "nPagina": 1,
                "nRegPorPagina": 20,
                "cModelo": "55",
                "cOperacao": "0",
                "cAmbiente": "H",
            },
        ),
        (
            "cModelo=55 cOperacao=0 janela ampla 01/01/2024..hoje",
            {
                "nPagina": 1,
                "nRegPorPagina": 20,
                "cModelo": "55",
                "cOperacao": "0",
                "dEmiInicial": "01/01/2024",
                "dEmiFinal": hoje.strftime("%d/%m/%Y"),
            },
        ),
        (
            "cModelo=65 (NFC-e) cOperacao=0 SEM datas",
            {"nPagina": 1, "nRegPorPagina": 20, "cModelo": "65", "cOperacao": "0"},
        ),
    ]
    for rotulo, param in variantes:
        sucesso, resposta = chamar(
            cliente, URL_DOCUMENTOS, "ListarDocumentos", param
        )
        mostrar_resultado(f"ListarDocumentos {rotulo}", sucesso, resposta)
        if not sucesso:
            continue
        documentos = resposta.get("documentosEncontrados") or []
        registrar(f"  documentosEncontrados: {len(documentos)} registro(s)")
        if not documentos:
            continue
        nome = re.sub(r"\W+", "_", rotulo)[:40]
        salvar_json(f"03_documentos_{nome}", documentos[:2])
        primeiro = documentos[0]
        registrar("  campos do 1º registro: " + ", ".join(sorted(primeiro)))
        xml = str(primeiro.get("cXml") or "")
        registrar(f"  cXml: {len(xml)} chars | início: {xml[:160]!r}")


def secao_nfconsultar(cliente: OmieClient, contexto: dict) -> None:
    """Item 4: ``ConsultarNF``/``ListarNF`` em ``produtos/nfconsultar``."""
    secao("4) ConsultarNF / ListarNF (produtos/nfconsultar)")
    chave = str(contexto.get("chave") or "")
    numero = str(contexto.get("numero") or "")
    serie = str(contexto.get("serie") or "")

    if numero:
        param = {"nNF": numero}
        if serie:
            param["serie"] = serie
        sucesso, resposta = chamar(
            cliente, URL_NFCONSULTAR, "ConsultarNF", param
        )
        mostrar_resultado(
            f"ConsultarNF nNF={numero} serie={serie or '-'}", sucesso, resposta
        )
        if sucesso:
            salvar_json("04_consultar_nf_por_numero", resposta)
            registrar("  XML no retorno: " + str(campos_xml(resposta) or "NENHUM"))
    else:
        registrar("Sem número de NF no recebimento; teste por número pulado.")

    if chave:
        sucesso, resposta = chamar(
            cliente, URL_NFCONSULTAR, "ConsultarNF", {"cChaveNFe": chave}
        )
        mostrar_resultado("ConsultarNF cChaveNFe=<44 dígitos>", sucesso, resposta)
        if sucesso:
            salvar_json("04_consultar_nf_por_chave", resposta)

    sucesso, resposta = chamar(
        cliente,
        URL_NFCONSULTAR,
        "ListarNF",
        {"pagina": 1, "registros_por_pagina": 5, "ordenar_por": "CODIGO"},
    )
    mostrar_resultado("ListarNF página 1 (5 registros)", sucesso, resposta)
    if not sucesso:
        return
    salvar_json("05_listar_nf", resposta)
    lista = _primeira_lista(resposta)
    registrar(f"  registros na 1ª página: {len(lista)}")
    registrar(f"  XML no retorno: {campos_xml(resposta) or 'NENHUM'}")
    if lista:
        registrar("  campos do 1º registro: " + ", ".join(sorted(lista[0])))


def main(argv=None) -> int:
    """Executa as quatro verificações e grava relatório + dumps em %TEMP%."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Diagnostica onde está o XML/itens das NF-e de entrada no Omie."
    )
    parser.add_argument(
        "--dias", type=int, default=15, help="Janela de recebimentos (padrão: 15)."
    )
    parser.add_argument(
        "--chave", default="", help="Chave de 44 dígitos a detalhar (opcional)."
    )
    args = parser.parse_args(argv)

    cliente = OmieClient()
    if not cliente.configurado:
        print("FALHOU: OMIE_APP_KEY/OMIE_APP_SECRET não configurados no .env.")
        return 1
    mascarar._segredo = cliente.app_secret

    secao("Configuração")
    registrar(
        f"app_key={cliente.app_key[:4]}*** | app_secret=*** "
        f"({len(cliente.app_secret)} chars) | timeout={cliente.timeout:g}s"
    )
    PASTA.mkdir(parents=True, exist_ok=True)
    registrar(f"Saída: {PASTA}")

    contexto: dict = {}
    try:
        contexto = secao_recebimentos(cliente, args.dias, args.chave)
    except ErroOmie as erro:
        registrar(f"[1] ERRO do Omie: {mascarar(str(erro))}")
    for funcao, argumentos in (
        (secao_documentos, (cliente,)),
        (secao_nfconsultar, (cliente, contexto)),
    ):
        try:
            funcao(*argumentos)
        except ErroOmie as erro:
            registrar(f"[{funcao.__name__}] ERRO do Omie: {mascarar(str(erro))}")

    relatorio = PASTA / "relatorio.txt"
    relatorio.write_text("\n".join(RELATORIO), encoding="utf-8")
    print(f"\nRelatório gravado em: {relatorio}")
    print(f"Dumps JSON em: {PASTA}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

