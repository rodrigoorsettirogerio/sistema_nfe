"""Diagnóstico do kanban de Compras (``cEtapa``) e do XML recente no Omie.

Somente leitura: não grava no banco, não altera o cliente e não imprime
segredos (``OMIE_APP_SECRET`` nunca aparece; a app_key sai mascarada).

Uso:
    python diagnostico_omie_cetapa.py --fase a
    python diagnostico_omie_cetapa.py --fase b
    python diagnostico_omie_cetapa.py --fase c
    python diagnostico_omie_cetapa.py --fase tudo

Cada fase grava o seu resultado em ``%TEMP%\\omie_cetapa`` assim que a
chamada volta, porque o Omie bloqueia a conta por 300 s (HTTP 425) quando
recebe muitas chamadas seguidas — assim nada se perde no meio.

Fase A: ``ListarRecebimentos`` por ``cEtapa`` (40/50/60/70/80), sem data,
        com ``cExibirDetalhes="S"`` para medir também a presença de itens.
Fase B: ``ConsultarRecebimento`` da nota de cada etapa + ``ConsultarNF`` por
        chave (entrada vs. saída).
Fase C: ``ListarDocumentos`` de entrada sem filtro de data até a última
        página (nota mais recente) + ``ObterNfe`` (produtos/dfedocs) e
        ``ListarNFe`` (produtos/nfe) como fontes alternativas de XML.
"""

from __future__ import annotations

import argparse
import html
import json
import sys
import tempfile
import time
from datetime import date, datetime
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
    chave_do_xml,
)

URL_NFCONSULTAR = "https://app.omie.com.br/api/v1/produtos/nfconsultar/"
URL_DFEDOCS = "https://app.omie.com.br/api/v1/produtos/dfedocs/"
URL_NFE = "https://app.omie.com.br/api/v1/produtos/nfe/"
PASTA = Path(tempfile.gettempdir()) / "omie_cetapa"
RELATORIO: list = []
ETAPAS = ("40", "50", "60", "70", "80")
INTERVALO = 2.0
TAMANHO_PAGINA = 100
# nIdNF do documento de 30/09/2026 (controle que sabemos existir no
# contador/xml) — usado para testar o ``ObterNfe`` na fase C.
NID_NFE_CONTROLE = 7997145344


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


def salvar_relatorio() -> Path:
    """Grava o relatório da fase em texto (UTF-8)."""
    PASTA.mkdir(parents=True, exist_ok=True)
    arquivo = PASTA / "relatorio_cetapa.txt"
    arquivo.write_text("\n".join(RELATORIO), encoding="utf-8")
    return arquivo


def chamar(cliente: OmieClient, url: str, call: str, param: dict):
    """Chama o Omie devolvendo ``(ok, corpo_ou_erro)``, sem derrubar o script."""
    time.sleep(INTERVALO)
    try:
        return True, cliente._chamar(url, call, param)
    except ErroOmie as erro:
        return False, erro


def _http(resultado) -> object:
    return getattr(resultado, "status", None)


def bloqueado(resultado) -> bool:
    """True quando o Omie respondeu 425 (consumo indevido, 300 s de castigo)."""
    return str(_http(resultado) or "") == "425" or "consumo indevido" in str(
        resultado
    ).lower()


def relatar_erro(rotulo: str, resultado) -> None:
    """Registra o erro do Omie mascarado, com código/HTTP."""
    registrar(
        f"[{rotulo}] ERRO (código={getattr(resultado, 'codigo', '') or '-'}, "
        f"HTTP={_http(resultado) or '-'}): {mascarar(str(resultado))}"
    )


def _resumo_cabec(cabec: dict) -> dict:
    """Campos de identificação do recebimento (``cabec``)."""
    return {
        campo: cabec.get(campo)
        for campo in (
            "cChaveNFe",
            "nIdReceb",
            "cNumeroNFe",
            "cSerieNFe",
            "dEmissaoNFe",
            "cEtapa",
            "cNome",
            "cCNPJ_CPF",
            "nValorNFe",
        )
        if cabec.get(campo) not in (None, "")
    }


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


def _resumo_itens(recebimento: dict) -> dict:
    """Quantidade de itens e campos do primeiro item (``itensCabec``)."""
    itens = recebimento.get("itensRecebimento") or []
    resumo = {"qtd_itens": len(itens)}
    if itens and isinstance(itens[0], dict):
        cab_item = itens[0].get("itensCabec")
        if not isinstance(cab_item, dict):
            cab_item = itens[0]
        resumo["campos_itensCabec"] = sorted(cab_item)
        resumo["item_0"] = {
            campo: cab_item.get(campo)
            for campo in (
                "cCodigoProduto",
                "cDescricaoProduto",
                "cUnidadeNfe",
                "nQtdeNFe",
                "nPrecoUnit",
                "vTotalItem",
                "cNCM",
                "cEAN",
                "cCFOP",
            )
            if cab_item.get(campo) not in (None, "")
        }
    return resumo


def _data_br(valor: str) -> date:
    """Converte ``dd/mm/aaaa`` em ``date`` (data mínima se vazio/inválido)."""
    try:
        return datetime.strptime(str(valor).strip(), "%d/%m/%Y").date()
    except (TypeError, ValueError):
        return date.min


def _carregar(nome: str):
    """Lê um resultado já gravado por uma fase anterior."""
    arquivo = PASTA / f"{nome}.json"
    if not arquivo.exists():
        return None
    try:
        return json.loads(arquivo.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def fase_a(cliente: OmieClient) -> dict:
    """Item 1: nTotalRegistros de cada ``cEtapa``, sem filtro de data."""
    secao("A) ListarRecebimentos por cEtapa — SEM data (1 registro, detalhes S)")
    resultado: dict = {}
    for etapa in ETAPAS:
        param = {
            "nPagina": 1,
            "nRegistrosPorPagina": 1,
            "cEtapa": etapa,
            "cExibirDetalhes": "S",
        }
        rotulo = f"cEtapa={etapa}"
        ok, corpo = chamar(cliente, URL_RECEBIMENTOS, "ListarRecebimentos", param)
        if not ok:
            relatar_erro(rotulo, corpo)
            resultado[etapa] = {"erro": mascarar(str(corpo))}
            if bloqueado(corpo):
                registrar("Omie bloqueou a sequência (HTTP 425); parando a fase A.")
                break
            continue
        lote = corpo.get("recebimentos") or []
        amostra = lote[0] if lote and isinstance(lote[0], dict) else {}
        cabec = amostra.get("cabec") or {}
        info = {
            "nTotalRegistros": corpo.get("nTotalRegistros"),
            "nTotalPaginas": corpo.get("nTotalPaginas"),
            "chaves_resposta": sorted(corpo),
            "exemplo": _resumo_cabec(cabec),
            "exemplo_itens": _resumo_itens(amostra),
        }
        resultado[etapa] = info
        registrar(
            f"[{rotulo}] OK — nTotalRegistros={info['nTotalRegistros']} "
            f"nTotalPaginas={info['nTotalPaginas']} "
            f"itens={info['exemplo_itens'].get('qtd_itens')} "
            f"exemplo={json.dumps(info['exemplo'], ensure_ascii=False)}"
        )
    # Sem cEtapa, para conferir o total geral da tela.
    param = {"nPagina": 1, "nRegistrosPorPagina": 1}
    ok, corpo = chamar(cliente, URL_RECEBIMENTOS, "ListarRecebimentos", param)
    if ok:
        resultado["sem_cetapa"] = {
            "nTotalRegistros": corpo.get("nTotalRegistros"),
            "nTotalPaginas": corpo.get("nTotalPaginas"),
        }
        registrar(
            "[sem cEtapa] OK — nTotalRegistros="
            f"{corpo.get('nTotalRegistros')} "
            f"nTotalPaginas={corpo.get('nTotalPaginas')}"
        )
    else:
        relatar_erro("sem cEtapa", corpo)
        resultado["sem_cetapa"] = {"erro": mascarar(str(corpo))}
    registrar(f"Dump: {salvar_json('fase_a', resultado)}")
    return resultado


def _etapa_mais_proxima(dados: dict, alvo: int, padrao: str) -> str:
    """Etapa cujo total mais se aproxima da contagem lida na tela.

    A tela do Omie muda a cada sincronização (743/158/990 no momento do
    teste), então o valor exibido pelo usuário serve de referência, não de
    igualdade. Etapas com erro/vazias são ignoradas.
    """
    melhor_etapa, menor_distancia = padrao, None
    for etapa, info in (dados or {}).items():
        if etapa == "sem_cetapa" or not isinstance(info, dict):
            continue
        total = info.get("nTotalRegistros")
        if total in (None, "", 0, "0"):
            continue
        distancia = abs(int(total) - int(alvo))
        if menor_distancia is None or distancia < menor_distancia:
            melhor_etapa, menor_distancia = str(etapa), distancia
    return melhor_etapa



def _nota_da_etapa(dados: dict, etapa: str) -> dict:
    """Exemplo de nota capturado na fase A para a etapa informada."""
    info = (dados or {}).get(str(etapa)) or {}
    return info.get("exemplo") or {}


def fase_b(cliente: OmieClient) -> dict:
    """Item 2: a nota de cada coluna já traz ``itensRecebimento``?"""
    secao("B) ConsultarRecebimento por coluna — itens disponíveis?")
    dados = _carregar("fase_a")
    if not dados:
        registrar("fase_a.json ausente; rodando a fase A antes.")
        dados = fase_a(cliente)
    mapa = {
        "faturado": _etapa_mais_proxima(dados, 741, "40"),
        "recebido": _etapa_mais_proxima(dados, 138, "60"),
        "conferido": _etapa_mais_proxima(dados, 905, "80"),
    }
    registrar(
        "Mapa por contagem mais próxima da tela (741/138/905): "
        + json.dumps(mapa, ensure_ascii=False)
    )
    resultado: dict = {"mapa": mapa}
    for coluna, etapa in mapa.items():
        exemplo = _nota_da_etapa(dados, etapa)
        nid = exemplo.get("nIdReceb")
        if not nid:
            registrar(f"[{coluna}] cEtapa={etapa}: sem exemplo na fase A; pulando.")
            continue
        rotulo = f"{coluna} cEtapa={etapa} nIdReceb={nid}"
        ok, corpo = chamar(
            cliente, URL_RECEBIMENTOS, "ConsultarRecebimento", {"nIdReceb": nid}
        )
        if not ok:
            relatar_erro(rotulo, corpo)
            resultado[coluna] = {"erro": mascarar(str(corpo))}
            if bloqueado(corpo):
                break
            continue
        receb = corpo if isinstance(corpo, dict) else {}
        cabec = receb.get("cabec") or {}
        itens = _resumo_itens(receb)
        info = {
            "etapa": etapa,
            "nIdReceb": nid,
            "chave": cabec.get("cChaveNFe"),
            "cEtapa_cabec": cabec.get("cEtapa"),
            "cabec": _resumo_cabec(cabec),
            "itens": itens,
            "nIdNF": _achar_chave(receb, "nIdNF"),
            "infoCadastro": receb.get("infoCadastro"),
        }
        resultado[coluna] = info
        registrar(
            f"[{rotulo}] OK — cEtapa(cabec)={info['cEtapa_cabec']} "
            f"itensRecebimento={itens['qtd_itens']} "
            f"nIdNF={info['nIdNF']} chave={info['chave']}"
        )
        if itens.get("item_0"):
            registrar(
                "   item 0: " + json.dumps(itens["item_0"], ensure_ascii=False)
            )
        elif itens.get("campos_itensCabec"):
            registrar(
                "   item 0 sem campos esperados; campos: "
                + ", ".join(itens["campos_itensCabec"])
            )
        salvar_json(f"fase_b_consultar_{etapa}", receb)
        # A mesma chave é encontrada como ENTRADA (tpNF=0) no nfconsultar?
        # Só na coluna "faturado": é a que a fase 1 precisa importar.
        chave = str(info["chave"] or "").strip()
        if chave and coluna == "faturado":
            for tp in ("0", "1"):
                ok_nf, nf = chamar(
                    cliente,
                    URL_NFCONSULTAR,
                    "ConsultarNF",
                    {"cChaveNFe": chave, "tpNF": tp},
                )
                rot_nf = f"ConsultarNF chave={chave} tpNF={tp}"
                if ok_nf:
                    registrar(
                        f"[{rot_nf}] OK — nCodNF={_achar_chave(nf, 'nCodNF')} "
                        f"nNF={_achar_chave(nf, 'nNF')}"
                    )
                else:
                    relatar_erro(rot_nf, nf)
                    if bloqueado(nf):
                        break
    # Controle: sem cExibirDetalhes a listagem não traz itens.
    etapa_fat = mapa["faturado"]
    ok, corpo = chamar(
        cliente,
        URL_RECEBIMENTOS,
        "ListarRecebimentos",
        {"nPagina": 1, "nRegistrosPorPagina": 1, "cEtapa": etapa_fat},
    )
    if ok:
        lote = corpo.get("recebimentos") or []
        itens = _resumo_itens(lote[0]) if lote else {"qtd_itens": 0}
        resultado["sem_detalhes"] = itens
        registrar(
            f"[cEtapa={etapa_fat} SEM cExibirDetalhes] itensRecebimento="
            f"{itens['qtd_itens']} (confirma que os itens vêm do parâmetro)"
        )
    else:
        relatar_erro("sem cExibirDetalhes", corpo)
    registrar(f"Dump: {salvar_json('fase_b', resultado)}")
    return resultado


def fase_c(cliente: OmieClient) -> dict:
    """Item 3: até onde vai o XML do Omie e quais fontes ainda respondem."""
    secao("C) ListarDocumentos entrada SEM data — varredura completa")
    documentos: list = []
    total_informado = None
    for pagina in range(1, 21):
        param = {
            "nPagina": pagina,
            "nRegPorPagina": TAMANHO_PAGINA,
            "cModelo": "55",
            "cOperacao": "0",
        }
        ok, corpo = chamar(cliente, URL_DOCUMENTOS, "ListarDocumentos", param)
        if not ok:
            relatar_erro(f"ListarDocumentos pág. {pagina}", corpo)
            if bloqueado(corpo):
                registrar("Omie bloqueou (HTTP 425); a varredura para aqui.")
            break
        total_informado = corpo.get("nTotRegistros", total_informado)
        lote = corpo.get("documentosEncontrados") or []
        registrar(
            f"[pág. {pagina}] {len(lote)} doc(s) — "
            f"nTotRegistros={corpo.get('nTotRegistros')} "
            f"nTotPaginas={corpo.get('nTotPaginas')}"
        )
        documentos.extend(lote)
        if len(lote) < TAMANHO_PAGINA:
            break
    resumo = [
        {
            "nChave": doc.get("nChave"),
            "nNumero": doc.get("nNumero"),
            "cSerie": doc.get("cSerie"),
            "dEmissao": doc.get("dEmissao"),
            "nIdNF": doc.get("nIdNF"),
            "nIdReceb": doc.get("nIdReceb"),
            "cStatus": doc.get("cStatus"),
            "cXml_chars": len(str(doc.get("cXml") or "")),
        }
        for doc in documentos
        if isinstance(doc, dict)
    ]
    ordenados = sorted(
        resumo, key=lambda doc: _data_br(doc.get("dEmissao")), reverse=True
    )
    registrar(
        f"Total varrido: {len(resumo)} documento(s) "
        f"(nTotRegistros={total_informado})"
    )
    if ordenados:
        registrar("5 mais recentes por dEmissao:")
        for doc in ordenados[:5]:
            registrar("   " + json.dumps(doc, ensure_ascii=False))
    por_mes: dict = {}
    for doc in resumo:
        mes = str(doc.get("dEmissao") or "")[-7:]
        por_mes[mes] = por_mes.get(mes, 0) + 1
    registrar(
        "Docs por mês (aaaa-mm): "
        + json.dumps(dict(sorted(por_mes.items())), ensure_ascii=False)
    )
    registrar(f"Dump: {salvar_json('fase_c_documentos', ordenados)}")
    return {"documentos": ordenados, "ultimo": ordenados[0] if ordenados else {}}


def _primeira_lista(corpo: dict):
    """Primeira lista de dicionários da resposta (nome do campo varia)."""
    if not isinstance(corpo, dict):
        return []
    for valor in corpo.values():
        if isinstance(valor, list) and valor and isinstance(valor[0], dict):
            return valor
    return []


def _obter_nfe(cliente: OmieClient, rotulo: str, nid) -> dict:
    """Chama ``ObterNfe`` e resume o XML devolvido (sem gravar o XML inteiro)."""
    ok, corpo = chamar(cliente, URL_DFEDOCS, "ObterNfe", {"nIdNfe": nid})
    if not ok:
        relatar_erro(rotulo, corpo)
        return {"erro": mascarar(str(corpo))}
    xml = str(corpo.get("cXmlNfe") or "")
    info = {
        "nIdNfe": nid,
        "nChaveNfe": corpo.get("nChaveNfe"),
        "cNumNfe": corpo.get("cNumNfe"),
        "dDataEmisNfe": corpo.get("dDataEmisNfe"),
        "cXmlNfe_chars": len(xml),
        "xml_escapado": xml.lstrip().startswith("&lt;"),
        "chave_do_xml": chave_do_xml(xml),
        "chave_do_xml_unescape": chave_do_xml(html.unescape(xml)),
        "tem_pdf": bool(corpo.get("cPdf")),
        "cCodStatus": corpo.get("cCodStatus"),
    }
    registrar(f"[{rotulo}] OK — " + json.dumps(info, ensure_ascii=False))
    return info


def fase_c2(cliente: OmieClient, ultimo: dict) -> dict:
    """Item 3b: ``ObterNfe`` (dfedocs) e ``ListarNFe`` (nfe) como fontes."""
    secao("C2) ObterNfe (produtos/dfedocs) + ListarNFe (produtos/nfe)")
    resultado: dict = {}
    # 1) Controle: documento que sabemos existir no contador/xml.
    resultado["controle"] = _obter_nfe(
        cliente, f"ObterNfe nIdNfe={NID_NFE_CONTROLE} (controle 30/09/2026)",
        NID_NFE_CONTROLE,
    )
    # 2) Nota mais recente do contador/xml (mesmo nIdNF, outra origem).
    nid_contador = (ultimo or {}).get("nIdNF")
    if nid_contador and str(nid_contador) != str(NID_NFE_CONTROLE):
        resultado["ultimo_contador"] = _obter_nfe(
            cliente, f"ObterNfe nIdNfe={nid_contador} (mais recente do contador)",
            nid_contador,
        )
    # 3) nIdNF vindo do ConsultarRecebimento (fase B) — a nota de compra.
    fase_b = _carregar("fase_b") or {}
    nid_receb = None
    for coluna in ("faturado", "recebido", "conferido"):
        info = fase_b.get(coluna) or {}
        if info.get("nIdNF"):
            nid_receb = (coluna, info["nIdNF"])
            break
    if nid_receb:
        coluna, nid = nid_receb
        resultado["recebimento"] = _obter_nfe(
            cliente, f"ObterNfe nIdNfe={nid} (recebimento {coluna})", nid
        )
    else:
        registrar(
            "ConsultarRecebimento não devolveu nIdNF (fase B); "
            "não há ponte direta recebimento → ObterNfe."
        )
    # 4) ListarNFe (produtos/nfe): NFes importadas na janela de novembro.
    ok, corpo = chamar(
        cliente,
        URL_NFE,
        "ListarNFe",
        {
            "nPagina": 1,
            "nRegPorPagina": 20,
            "dDataEmissaoInicial": "01/11/2026",
            "dDataEmissaoFinal": "24/11/2026",
        },
    )
    if not ok:
        relatar_erro("ListarNFe 01–24/11/2026", corpo)
        resultado["listar_nfe"] = {"erro": mascarar(str(corpo))}
    else:
        lote = _primeira_lista(corpo)
        registrar(f"[ListarNFe 01–24/11/2026] OK — chaves: {sorted(corpo)}")
        registrar(
            f"[ListarNFe] {len(lote)} registro(s) na página; "
            f"nTotRegistros={corpo.get('nTotRegistros')}"
        )
        if lote:
            registrar(
                "   campos do 1º: " + ", ".join(sorted(lote[0]))[:600]
            )
            registrar(
                "   1º registro: "
                + json.dumps(lote[0], ensure_ascii=False, default=str)[:800]
            )
        resultado["listar_nfe"] = {
            "chaves_resposta": sorted(corpo),
            "qtd": len(lote),
            "nTotRegistros": corpo.get("nTotRegistros"),
        }
        salvar_json("fase_c2_listar_nfe", corpo)
        # Primeiro nIdNfe da listagem: o XML existe fora do contador/xml?
        nid_nfe = _achar_chave(lote, "nIdNfe") or _achar_chave(lote, "nIdNF")
        if nid_nfe:
            resultado["listar_nfe_obter"] = _obter_nfe(
                cliente, f"ObterNfe nIdNfe={nid_nfe} (1º do ListarNFe)", nid_nfe
            )
    registrar(f"Dump: {salvar_json('fase_c2', resultado)}")
    return resultado


def fase_d(cliente: OmieClient) -> dict:
    """Item 3c: ``ListarNF`` decrescente — ``compl.nIdNF`` é o ``nIdNfe``.

    O ``produtos/nfconsultar`` lista notas com ``ide.tpNF=0`` (entrada), então
    o ``nIdNF`` delas alimenta o ``ObterNfe`` (produtos/dfedocs) e devolve o
    XML — inclusive para notas que não estão no ``contador/xml``.
    """
    secao("D) ListarNF decrescente (entradas incluídas?) + ObterNfe")
    resultado: dict = {}
    ok, corpo = chamar(
        cliente,
        URL_NFCONSULTAR,
        "ListarNF",
        {
            "pagina": 1,
            "registros_por_pagina": 50,
            "apenas_importado_api": "N",
            "ordem_decrescente": "S",
        },
    )
    if not ok:
        relatar_erro("ListarNF decrescente", corpo)
        resultado["listar_nf"] = {"erro": mascarar(str(corpo))}
        registrar(f"Dump: {salvar_json('fase_d', resultado)}")
        return resultado

    notas = corpo.get("nfCadastro") or []
    resumo = []
    for nota in notas:
        ide = nota.get("ide") or {}
        compl = nota.get("compl") or {}
        resumo.append(
            {
                "nIdNF": compl.get("nIdNF"),
                "nNF": ide.get("nNF"),
                "serie": ide.get("serie"),
                "dEmi": ide.get("dEmi"),
                "tpNF": ide.get("tpNF"),
                "chave": (nota.get("infNFe") or {}).get("chave")
                or (nota.get("compl") or {}).get("cChaveNFe"),
            }
        )
    registrar(
        f"[ListarNF desc] total_de_registros={corpo.get('total_de_registros')} "
        f"total_de_paginas={corpo.get('total_de_paginas')} "
        f"notas na página={len(notas)}"
    )
    registrar("10 primeiras (mais recentes por código):")
    for nota in resumo[:10]:
        registrar("   " + json.dumps(nota, ensure_ascii=False))
    resultado["listar_nf"] = {
        "total_de_registros": corpo.get("total_de_registros"),
        "total_de_paginas": corpo.get("total_de_paginas"),
        "primeiras": resumo[:10],
    }
    entrada = next((n for n in resumo if str(n.get("tpNF")) == "0"), None)
    if entrada and entrada.get("nIdNF"):
        resultado["obter_nfe_entrada"] = _obter_nfe(
            cliente,
            f"ObterNfe nIdNfe={entrada['nIdNF']} (entrada do ListarNF)",
            entrada["nIdNF"],
        )
    novembro = [
        n for n in resumo if _data_br(n.get("dEmi")) >= date(2026, 11, 1)
    ]
    registrar(
        f"Notas de novembro/2026 na 1ª página: {len(novembro)}"
    )
    if novembro:
        registrar("   " + json.dumps(novembro[:10], ensure_ascii=False))
        nota = novembro[0]
        if nota.get("nIdNF"):
            resultado["obter_nfe_novembro"] = _obter_nfe(
                cliente,
                f"ObterNfe nIdNfe={nota['nIdNF']} (novembro/2026)",
                nota["nIdNF"],
            )
    registrar(f"Dump: {salvar_json('fase_d', resultado)}")
    return resultado



def _chaves_recebimentos_novembro() -> set:
    """Chaves das NF-e capturadas pelo diagnóstico anterior de recebimentos."""
    arquivo = (
        Path(tempfile.gettempdir()) / "omie_diag" / "01_listar_recebimentos.json"
    )
    try:
        dados = json.loads(arquivo.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    chaves = set()
    for item in dados if isinstance(dados, list) else []:
        cabec = (item or {}).get("cabec") or {}
        chave = str(cabec.get("cChaveNFe") or "").strip()
        if chave:
            chaves.add(chave)
    return chaves


def _chave_da_nota(nota: dict) -> str:
    """Chave de acesso dentro de um item de ``nfCadastro``."""
    candidatos = (
        (nota.get("infNFe") or {}).get("chave"),
        (nota.get("compl") or {}).get("cChaveNFe"),
        nota.get("cChaveNFe"),
    )
    for valor in candidatos:
        if valor not in (None, ""):
            return str(valor).strip()
    return ""


def fase_e(cliente: OmieClient) -> dict:
    """Item 3d: varre o ``ListarNF`` inteiro e cruza com as notas de novembro."""
    secao("E) ListarNF — todas as páginas (alguma nota de novembro/2026?)")
    notas: list = []
    total_informado = None
    for pagina in range(1, 21):
        ok, corpo = chamar(
            cliente,
            URL_NFCONSULTAR,
            "ListarNF",
            {
                "pagina": pagina,
                "registros_por_pagina": TAMANHO_PAGINA,
                "apenas_importado_api": "N",
                "ordem_decrescente": "S",
            },
        )
        if not ok:
            relatar_erro(f"ListarNF pág. {pagina}", corpo)
            if bloqueado(corpo):
                registrar("Omie bloqueou (HTTP 425); a varredura para aqui.")
            break
        total_informado = corpo.get("total_de_registros", total_informado)
        lote = corpo.get("nfCadastro") or []
        registrar(
            f"[pág. {pagina}] {len(lote)} nota(s) — "
            f"total_de_registros={corpo.get('total_de_registros')}"
        )
        notas.extend(lote)
        if len(lote) < TAMANHO_PAGINA:
            break
    resumo = []
    for nota in notas:
        ide = nota.get("ide") or {}
        compl = nota.get("compl") or {}
        resumo.append(
            {
                "nIdNF": compl.get("nIdNF"),
                "nNF": ide.get("nNF"),
                "serie": ide.get("serie"),
                "dEmi": ide.get("dEmi"),
                "tpNF": ide.get("tpNF"),
                "chave": _chave_da_nota(nota),
            }
        )
    ordenadas = sorted(
        resumo, key=lambda nota: _data_br(nota.get("dEmi")), reverse=True
    )
    registrar(
        f"Total varrido: {len(resumo)} nota(s) "
        f"(total_de_registros={total_informado})"
    )
    registrar("5 mais recentes por dEmi:")
    for nota in ordenadas[:5]:
        registrar("   " + json.dumps(nota, ensure_ascii=False))
    por_mes: dict = {}
    for nota in resumo:
        if str(nota.get("dEmi") or "").endswith("2026"):
            mes = str(nota.get("dEmi"))[-7:]
            por_mes[mes] = por_mes.get(mes, 0) + 1
    registrar(
        "Notas com emissão em 2026, por mês: "
        + json.dumps(dict(sorted(por_mes.items())), ensure_ascii=False)
    )
    esperadas = _chaves_recebimentos_novembro()
    presentes = {str(nota.get("chave") or "") for nota in resumo}
    encontradas = sorted(esperadas & presentes)
    registrar(
        f"Chaves de novembro/2026 presentes no ListarNF: "
        f"{len(encontradas)} de {len(esperadas)}"
    )
    resultado = {
        "total": len(resumo),
        "total_de_registros": total_informado,
        "mais_recentes": ordenadas[:5],
        "por_mes_2026": dict(sorted(por_mes.items())),
        "chaves_novembro": encontradas,
        "chaves_novembro_esperadas": len(esperadas),
    }
    salvar_json("fase_e_listar_nf", ordenadas)
    if ordenadas and ordenadas[0].get("nIdNF"):
        resultado["obter_nfe_mais_recente"] = _obter_nfe(
            cliente,
            f"ObterNfe nIdNfe={ordenadas[0]['nIdNF']} (nota mais recente)",
            ordenadas[0]["nIdNF"],
        )
    registrar(f"Dump: {salvar_json('fase_e', resultado)}")
    return resultado



def main(argv=None) -> int:
    """Executa a(s) fase(s) escolhidas e grava relatório + dumps em %TEMP%."""
    parser = argparse.ArgumentParser(
        description="Diagnostica cEtapa do kanban de Compras e o XML no Omie."
    )
    parser.add_argument(
        "--fase",
        choices=("a", "b", "c", "d", "e", "tudo"),
        default="tudo",
        help=(
            "a=cEtapa, b=itens, c=XML recente, d=ListarNF+ObterNfe, "
            "e=ListarNF completo (padrão: tudo)."
        ),
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
    registrar(f"Saída: {PASTA}")

    ultimo: dict = {}
    if args.fase in ("a", "tudo"):
        try:
            fase_a(cliente)
        except ErroOmie as erro:
            registrar(f"[A] ERRO do Omie: {mascarar(str(erro))}")
    if args.fase in ("b", "tudo"):
        try:
            fase_b(cliente)
        except ErroOmie as erro:
            registrar(f"[B] ERRO do Omie: {mascarar(str(erro))}")
    if args.fase in ("c", "tudo"):
        try:
            ultimo = (fase_c(cliente) or {}).get("ultimo") or {}
        except ErroOmie as erro:
            registrar(f"[C] ERRO do Omie: {mascarar(str(erro))}")
        try:
            fase_c2(cliente, ultimo)
        except ErroOmie as erro:
            registrar(f"[C2] ERRO do Omie: {mascarar(str(erro))}")
    if args.fase in ("d", "tudo"):
        try:
            fase_d(cliente)
        except ErroOmie as erro:
            registrar(f"[D] ERRO do Omie: {mascarar(str(erro))}")
    if args.fase in ("e", "tudo"):
        try:
            fase_e(cliente)
        except ErroOmie as erro:
            registrar(f"[E] ERRO do Omie: {mascarar(str(erro))}")

    relatorio = salvar_relatorio()
    print(f"\nRelatório gravado em: {relatorio}")
    print(f"Dumps JSON em: {PASTA}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

