"""Relatório de compras que vira e-mail (diário, semanal e mensal).

Regra central definida com o cliente: **toda variação de preço de pelo menos R$ 0,01
por unidade-base, em relação a uma referência do MESMO produto na MESMA loja, entra no
relatório**, sempre na unidade-base (CX24 -> unidade). A referência depende do tipo:

- **diário**: a compra imediatamente anterior daquele produto (última x penúltima);
- **semanal/mensal (período)**: a **média simples das compras do período** contra a
  média simples das compras do período anterior de mesma duração (semana retrasada /
  mês anterior). Quando um dos lados não tem compra no período, ele cai na compra
  única mais recente (a última compra antes do período), para o produto não sumir do
  relatório.

O relatório mostra:

- preço anterior x preço atual, variação (%) e o **impacto mensal em R$**, calculado
  sobre o consumo médio dos últimos 90 dias daquele produto (R$/mês = diferença de
  preço x consumo médio mensal);
- **data e fornecedor** de cada lado (no e-mail a informação aparece no tooltip do
  preço, ao passar o mouse). Nos relatórios de período o tooltip lista **todas as
  compras que compuseram a média** (até `LIMITE_COMPRAS_TOOLTIP`, com "... e mais N
  compras" quando passar do limite);
- itens **novos** (primeira compra do produto na loja) e **recomprados** (voltaram a
  ser comprados depois de mais de 30 dias sem compra);
- itens com variação de 50% ou mais **não são escondidos**: recebem o selo
  "⚠️ conferir" (unidade/cadastro podem explicar a variação brusca);
- notas que chegaram depois da data de emissão (importação atrasada) são marcadas com
  "⚠️ recebida com atraso";
- **série histórica** da inflação da cesta de compras: Laspeyres mensal (índice
  fechado) e prévia de 30 dias (`backend/inflacao.py`).

Este módulo é PURO: recebe os itens de `main._carregar_itens_relatorio` e um contexto
com o lote de notas do relatório. Não consulta banco nem envia e-mail — isso fica no
`main.py` (rotas e agendador), o que evita import circular.
"""

import html
from datetime import date, timedelta

from backend import inflacao

TIPOS = ("diario", "semanal", "mensal")
ROTULOS_TIPOS = {
    "diario": "Diário",
    "semanal": "Semanal",
    "mensal": "Mensal",
}
# Selo (não filtro): variação brusca continua no relatório, só pede conferência.
LIMIAR_CONFERIR_PCT = 50.0
# Variação mínima (em R$ por unidade-base) para o item entrar no relatório: 1 centavo.
# Diferença menor que isso é ruído de arredondamento do XML, não mudança de preço.
VARIACAO_MINIMA_ABS = 0.01
# Acima disso o produto é "recomprado" (voltou depois de um mês parado).
DIAS_RECOMPRA = 30
# Janela do consumo médio usado no impacto mensal em R$.
JANELA_CONSUMO_DIAS = 90
# Teto de linhas por tabela no e-mail (o relatório completo continua na prévia da tela).
LIMITE_LINHAS_EMAIL = 150
# Teto de compras listadas no tooltip de uma média: protege o layout do e-mail
# (o `title` de uma célula do Gmail não rola). Acima disso o texto termina em
# "... e mais N compras".
LIMITE_COMPRAS_TOOLTIP = 10
# Abaixo disso o índice é "amostra pequena" (mesmo critério de `inflacao`).
COBERTURA_MINIMA_PCT = inflacao.COBERTURA_MINIMA_PCT


def _preco(item) -> float:
    """Preço por unidade-base da compra."""
    return inflacao.preco_unitario(item)


def _quantidade(item) -> float:
    """Quantidade em unidades-base da compra."""
    return inflacao.quantidade_base(item)


def formatar_moeda(valor) -> str:
    """8.5 -> 'R$ 8,50' (padrão brasileiro, igual ao restante do sistema)."""
    texto = f"{float(valor or 0.0):,.2f}"
    return "R$ " + texto.replace(",", "X").replace(".", ",").replace("X", ".")


def formatar_pct(valor) -> str:
    """18.123 -> '+18,1%'; None -> '—'."""
    if valor is None:
        return "—"
    return f"{float(valor):+.1f}%".replace(".", ",")


def formatar_data(valor) -> str:
    """date/ISO -> 'DD/MM/AAAA' (string vazia quando não há data)."""
    if not valor:
        return ""
    if isinstance(valor, date):
        return f"{valor:%d/%m/%Y}"
    texto = str(valor)
    try:
        return f"{date.fromisoformat(texto[:10]):%d/%m/%Y}"
    except ValueError:
        return texto


def formatar_data_curta(valor) -> str:
    """date/ISO -> 'DD/MM' (formato curto usado nos tooltips de período)."""
    if not valor:
        return ""
    if isinstance(valor, date):
        return f"{valor:%d/%m}"
    texto = str(valor)
    try:
        return f"{date.fromisoformat(texto[:10]):%d/%m}"
    except ValueError:
        return texto


def periodo_anterior(inicio, fim, tipo: str) -> dict:
    """Período de comparação do relatório de período (semana/mês anterior).

    - ``mensal``: mês civil imediatamente anterior (01/10 a 31/10 para novembro);
    - ``semanal``: a semana anterior com a MESMA duração do período (seg-dom).
    """
    if inicio is None or fim is None:
        return {}
    if tipo == "mensal":
        fim_anterior = inicio - timedelta(days=1)
        inicio_anterior = fim_anterior.replace(day=1)
    else:
        duracao = (fim - inicio).days + 1
        fim_anterior = inicio - timedelta(days=1)
        inicio_anterior = fim_anterior - timedelta(days=duracao - 1)
    return {"inicio": inicio_anterior, "fim": fim_anterior}


def compras_no_periodo(compras, inicio, fim) -> list:
    """Compras da série com data dentro de ``[inicio, fim]`` (ordem cronológica)."""
    if inicio is None or fim is None:
        return []
    return [
        compra
        for compra in compras
        if compra.get("data") is not None and inicio <= compra["data"] <= fim
    ]


def media_precos(compras):
    """Média SIMPLES dos preços por unidade-base das compras (None se não houver)."""
    precos = [_preco(compra) for compra in compras]
    precos = [preco for preco in precos if preco > 0]
    if not precos:
        return None
    return sum(precos) / len(precos)


def _compras_para_tooltip(compras):
    """Serializa as compras que compõem a média (data, fornecedor e preço unitário)."""
    if compras is None:
        return None
    return [
        {
            "data": compra["data"].isoformat(),
            "fornecedor": str(compra.get("fornecedor") or ""),
            "preco": round(_preco(compra), 4),
        }
        for compra in compras
    ]


def _periodo_para_tooltip(periodo):
    """Serializa ``{inicio, fim}`` para o tooltip (None quando não há período)."""
    if not periodo:
        return None
    return {
        "inicio": periodo["inicio"].isoformat() if periodo.get("inicio") else None,
        "fim": periodo["fim"].isoformat() if periodo.get("fim") else None,
    }


def _chave_produto(item) -> tuple:
    """(loja, chave do grupo) — o preço NUNCA é comparado entre lojas diferentes."""
    return (str(item.get("cnpj") or ""), str(item.get("chave") or ""))


def _ordenado(item) -> tuple:
    """Ordem cronológica estável (data da nota + id do item)."""
    return (item.get("data") or date.min, item.get("id") or 0)


def _consumo_mensal(compras, fim: date, janela_dias: int = JANELA_CONSUMO_DIAS) -> float:
    """Consumo médio mensal em unidades-base nos últimos `janela_dias`."""
    inicio = fim - timedelta(days=janela_dias - 1)
    total = sum(
        _quantidade(item)
        for item in compras
        if item.get("data") is not None and inicio <= item["data"] <= fim
    )
    return total / (janela_dias / 30.0) if total else 0.0


def mes_fechado(referencia):
    """Último dia do último mês COMPLETO antes/dentro da referência.

    O relatório mensal já usa a referência no fim do mês fechado (devolve a própria
    data). Diário/semanal usam "hoje": o mês corrente ainda está aberto e a variação
    dele contra o mês anterior não seria comparável.

    É pública porque o Excel de memória de cálculo (rota
    `/api/alertas/relatorio/memoria-calculo`) precisa apontar exatamente o mesmo mês
    fechado do relatório.
    """
    if referencia is None:
        return None
    ultimo_dia_mes = (referencia.replace(day=1) + timedelta(days=32)).replace(
        day=1
    ) - timedelta(days=1)
    if referencia == ultimo_dia_mes:
        return referencia
    return referencia.replace(day=1) - timedelta(days=1)


def _bloco_inflacao(itens, referencia, tipo: str) -> dict:
    """Série histórica: mês fechado + prévia de 30 dias + evolução mensal."""
    fechado = mes_fechado(referencia)
    serie = inflacao.serie_mensal(itens, fechado) if fechado else []
    indice_fechado = None
    for registro in reversed(serie):
        if registro.get("indice") is not None:
            indice_fechado = registro
            break
    return {
        "fechado": indice_fechado,
        "serie": serie,
        "previa_30_dias": inflacao.previa_30_dias(itens, referencia),
        "metodologia": (
            "Série histórica (Laspeyres): cada mês compara os MESMOS produtos com o mês "
            "anterior, usando como peso o gasto do mês anterior. Produtos novos ficam "
            "fora da conta até serem comprados em dois meses seguidos."
        ),
    }


def _total_por_loja(variacoes, nomes_lojas) -> list:
    """Resumo por loja (aumentos, quedas e impacto), ordenado pelo impacto absoluto."""
    resumo = {}
    for linha in variacoes:
        cnpj = str(linha.get("cnpj") or "")
        registro = resumo.setdefault(
            cnpj,
            {
                "cnpj": cnpj,
                "loja": nomes_lojas.get(cnpj) or linha.get("loja") or "Sem loja",
                "aumentos": 0,
                "quedas": 0,
                "impacto": 0.0,
                "itens": 0,
            },
        )
        registro["itens"] += 1
        if linha["tipo"] == "aumento":
            registro["aumentos"] += 1
        else:
            registro["quedas"] += 1
        registro["impacto"] += linha["impacto_mensal"]
    saida = []
    for registro in resumo.values():
        registro["impacto"] = round(registro["impacto"], 2)
        saida.append(registro)
    saida.sort(key=lambda registro: abs(registro["impacto"]), reverse=True)
    return saida


def montar_relatorio(itens, contexto) -> dict:
    """Monta o relatório (dict pronto para JSON/HTML) a partir dos itens do período.

    `contexto` traz:
    - ``tipo``: diario/semanal/mensal;
    - ``notas_lote``: ids das notas que formam o lote do relatório;
    - ``atrasadas``: ids das notas do lote recebidas depois da emissão;
    - ``inicio``/``fim``: período do lote;
    - ``referencia``: data usada no consumo e na inflação;
    - ``lojas``: {cnpj: nome} da conta (para o resumo por loja).
    """
    itens = list(itens or [])
    contexto = contexto or {}
    tipo = str(contexto.get("tipo") or "diario").lower()
    if tipo not in TIPOS:
        tipo = "diario"
    notas_lote = {
        int(nota) for nota in (contexto.get("notas_lote") or []) if nota is not None
    }
    atrasadas = {
        int(nota) for nota in (contexto.get("atrasadas") or []) if nota is not None
    }
    nomes_lojas = {
        str(cnpj or ""): str(nome or "")
        for cnpj, nome in (contexto.get("lojas") or {}).items()
    }
    inicio = contexto.get("inicio")
    fim = contexto.get("fim") or contexto.get("referencia")
    referencia = contexto.get("referencia") or fim

    # Série completa por (loja, produto): é dela que sai a referência do relatório
    # (a compra anterior no diário; as compras do período e do período anterior nas
    # médias do semanal/mensal) — mesmo que a compra seja de antes do lote.
    series = {}
    for item in itens:
        if item.get("data") is None:
            continue
        series.setdefault(_chave_produto(item), []).append(item)
    for compras in series.values():
        compras.sort(key=_ordenado)

    lote = [
        item
        for item in itens
        if item.get("nota_id") is not None and int(item["nota_id"]) in notas_lote
    ]
    ultima_do_lote = {}
    for item in lote:
        if item.get("data") is None:
            continue
        chave = _chave_produto(item)
        atual = ultima_do_lote.get(chave)
        if atual is None or _ordenado(item) > _ordenado(atual):
            ultima_do_lote[chave] = item

    variacoes = []
    novos = []
    for chave, atual in ultima_do_lote.items():
        compras = series.get(chave, [])
        inicio_periodo = inicio or atual["data"]
        fim_periodo = fim or atual["data"]
        periodo_atual_ref = None
        periodo_anterior_ref = None
        compras_atual = None
        compras_anterior = None
        if tipo == "diario":
            preco_atual = _preco(atual)
            anteriores = [
                compra for compra in compras if _ordenado(compra) < _ordenado(atual)
            ]
        else:
            periodo_atual_ref = {"inicio": inicio_periodo, "fim": fim_periodo}
            periodo_anterior_ref = periodo_anterior(inicio_periodo, fim_periodo, tipo)
            compras_atual = compras_no_periodo(compras, inicio_periodo, fim_periodo)
            compras_anterior = compras_no_periodo(
                compras,
                periodo_anterior_ref.get("inicio"),
                periodo_anterior_ref.get("fim"),
            )
            if compras_atual:
                # "Agora" = média simples das compras do período (o valor da célula).
                atual = compras_atual[-1]
                preco_atual = media_precos(compras_atual)
            else:
                # Só nota atrasada (emitida antes do período): vira compra única.
                preco_atual = _preco(atual)
            # Qualquer compra anterior ao período serve de referência quando o período
            # anterior não teve compra — assim o produto não some do relatório. A
            # própria compra atrasada (única do lote, emitida antes do período) fica
            # de fora: sem histórico ela é "novo", não comparação consigo mesma.
            anteriores = [
                compra
                for compra in compras
                if compra["data"] < inicio_periodo
                and _ordenado(compra) < _ordenado(atual)
            ]
        if preco_atual is None or preco_atual <= 0:
            continue
        consumo = _consumo_mensal(compras, fim or atual["data"])
        if not anteriores:
            novos.append(
                {
                    "produto": str(atual.get("descricao") or "Produto sem descrição"),
                    "loja": nomes_lojas.get(chave[0]) or atual.get("loja") or "Sem loja",
                    "cnpj": chave[0],
                    "fornecedor": str(atual.get("fornecedor") or ""),
                    "preco_atual": round(preco_atual, 4),
                    "consumo_mensal": round(consumo, 3),
                    "quantidade_compra": round(_quantidade(atual), 3),
                    "data_atual": atual["data"].isoformat(),
                    "atrasada": atual.get("nota_id") in atrasadas,
                }
            )
            continue
        anterior = anteriores[-1]
        if tipo == "diario":
            preco_anterior = _preco(anterior)
        elif compras_anterior:
            # "Antes" = média simples das compras do período anterior.
            anterior = compras_anterior[-1]
            preco_anterior = media_precos(compras_anterior)
        else:
            # Sem compra no período anterior: última compra antes do período.
            preco_anterior = _preco(anterior)
        if preco_anterior is None or preco_anterior <= 0:
            continue
        # Regra do cliente: só entra variação de PELO MENOS 1 centavo por unidade-base.
        # Abaixo disso é arredondamento do XML (ex.: 0,0001 de diferença), não mudança
        # de preço — e o relatório ficaria cheio de linhas irrelevantes.
        if round(abs(preco_atual - preco_anterior), 4) < VARIACAO_MINIMA_ABS:
            continue
        variacao = (preco_atual / preco_anterior - 1) * 100
        impacto = (preco_atual - preco_anterior) * consumo
        if tipo == "diario":
            dias = (atual["data"] - anterior["data"]).days
        else:
            # "Recompra" olha a PRIMEIRA compra do período contra a última anterior a
            # ele: a média pode ter compras recentes mesmo depois de meses parado.
            primeira_atual = compras_atual[0] if compras_atual else atual
            dias = (primeira_atual["data"] - anteriores[-1]["data"]).days
        variacoes.append(
            {
                "produto": str(atual.get("descricao") or "Produto sem descrição"),
                "loja": nomes_lojas.get(chave[0]) or atual.get("loja") or "Sem loja",
                "cnpj": chave[0],
                # `fornecedor` continua sendo o da compra ATUAL (compatibilidade com a
                # tela e com o motor de alertas); os dois lados ficam explícitos abaixo
                # porque o e-mail mostra data + fornecedor de cada compra no tooltip.
                "fornecedor": str(atual.get("fornecedor") or ""),
                "fornecedor_atual": str(atual.get("fornecedor") or ""),
                "fornecedor_anterior": str(anterior.get("fornecedor") or ""),
                "preco_anterior": round(preco_anterior, 4),
                "preco_atual": round(preco_atual, 4),
                "variacao_abs": round(preco_atual - preco_anterior, 4),
                "variacao_pct": round(variacao, 2),
                "impacto_mensal": round(impacto, 2),
                "consumo_mensal": round(consumo, 3),
                "quantidade_compra": round(_quantidade(atual), 3),
                "data_atual": atual["data"].isoformat(),
                "data_anterior": anterior["data"].isoformat(),
                "dias_sem_compra": dias,
                # Lados do tooltip. No diário ficam None (compra única) e o e-mail usa
                # o texto clássico "Compra anterior/atual". No período, a lista mostra
                # todas as compras que compuseram a média de cada lado.
                "compras_atual": _compras_para_tooltip(compras_atual),
                "compras_anterior": _compras_para_tooltip(compras_anterior),
                "periodo_atual": _periodo_para_tooltip(periodo_atual_ref),
                "periodo_anterior": _periodo_para_tooltip(periodo_anterior_ref),
                "tipo": "aumento" if variacao > 0 else "queda",
                # Selos (não filtros): nada é escondido do relatório.
                "conferir": abs(variacao) >= LIMIAR_CONFERIR_PCT,
                "recomprado": dias >= DIAS_RECOMPRA,
                "atrasada": atual.get("nota_id") in atrasadas,
            }
        )

    variacoes.sort(
        key=lambda linha: (abs(linha["impacto_mensal"]), abs(linha["variacao_pct"])),
        reverse=True,
    )
    novos.sort(key=lambda linha: linha["consumo_mensal"], reverse=True)
    recomprados = [linha for linha in variacoes if linha["recomprado"]]

    impacto_aumentos = sum(
        linha["impacto_mensal"] for linha in variacoes if linha["tipo"] == "aumento"
    )
    impacto_quedas = sum(
        linha["impacto_mensal"] for linha in variacoes if linha["tipo"] == "queda"
    )
    resumo = {
        "aumentos": sum(1 for linha in variacoes if linha["tipo"] == "aumento"),
        "quedas": sum(1 for linha in variacoes if linha["tipo"] == "queda"),
        "variacoes": len(variacoes),
        "conferir": sum(1 for linha in variacoes if linha["conferir"]),
        "novos": len(novos),
        "recomprados": len(recomprados),
        "atrasadas": len(atrasadas),
        "impacto_aumentos": round(impacto_aumentos, 2),
        "impacto_quedas": round(impacto_quedas, 2),
        "impacto_liquido": round(impacto_aumentos + impacto_quedas, 2),
        "itens_lote": len(lote),
        "notas_lote": len(notas_lote),
    }
    return {
        "tipo": tipo,
        "titulo": f"Relatório {ROTULOS_TIPOS[tipo]} de Compras",
        "inicio": inicio.isoformat() if inicio else None,
        "fim": fim.isoformat() if fim else None,
        "referencia": referencia.isoformat() if referencia else None,
        "resumo": resumo,
        "variacoes": variacoes,
        "novos": novos,
        "recomprados": recomprados,
        "lojas": _total_por_loja(variacoes, nomes_lojas),
        "inflacao": _bloco_inflacao(itens, referencia, tipo),
        # Ids do lote: usados pelo main para registrar a cobertura (alertas_notas) e
        # avançar o cursor SÓ depois do envio confirmado.
        "notas_lote_ids": sorted(notas_lote),
    }


def _selos(linha) -> str:
    """Selos textuais da linha (não filtram nada)."""
    selos = []
    if linha.get("conferir"):
        selos.append("⚠️ conferir")
    if linha.get("atrasada"):
        selos.append("⚠️ recebida com atraso")
    if linha.get("recomprado"):
        selos.append("🔁 recompra")
    return " · ".join(selos)


# Cores do e-mail (inline, porque Gmail/Outlook ignoram CSS externo).
# Regra do cliente: valor NEGATIVO sempre em vermelho; positivo em cinza-escuro.
COR_NEGATIVA = "#b42318"
COR_POSITIVA = "#1f2937"


def _cor_do_valor(valor) -> str:
    """Vermelho para número negativo, cinza-escuro para positivo/zero."""
    try:
        numero = float(valor or 0.0)
    except (TypeError, ValueError):
        numero = 0.0
    return COR_NEGATIVA if numero < 0 else COR_POSITIVA


def _celula_variacao(valor) -> str:
    """Variação com cor: negativa em vermelho, positiva em cinza-escuro."""
    return (
        '<td style="padding:4px 6px;border-bottom:1px solid #e5e7eb;text-align:right;'
        f'color:{_cor_do_valor(valor)};font-weight:600;white-space:nowrap">'
        f"{formatar_pct(valor)}</td>"
    )


def _dica_das_compras(linha) -> str:
    """Tooltip do preço no relatório DIÁRIO: data + fornecedor da compra anterior e da atual.

    No e-mail o cliente vê só os preços; ao passar o mouse aparece de onde veio cada
    um ("Compra anterior: 01/09/2026 · FORNECEDOR A | Compra atual: 03/10/2026 ·
    FORNECEDOR B"). O atributo `title` funciona no Gmail, Outlook e Apple Mail.
    Semanal/mensal usam `_dica_do_lado` (média do período com todas as compras).
    """
    anterior_data = formatar_data(linha.get("data_anterior")) or "data não informada"
    atual_data = formatar_data(linha.get("data_atual")) or "data não informada"
    anterior_fornecedor = (
        str(linha.get("fornecedor_anterior") or "").strip() or "fornecedor não informado"
    )
    atual_fornecedor = str(
        linha.get("fornecedor_atual") or linha.get("fornecedor") or ""
    ).strip() or "fornecedor não informado"
    return (
        f"Compra anterior: {anterior_data} · {anterior_fornecedor} | "
        f"Compra atual: {atual_data} · {atual_fornecedor}"
    )


def _lista_de_compras(compras) -> str:
    """'Atacadão (R$ 84,50), Assaí (R$ 85,00), ... e mais 5 compras'.

    O teto `LIMITE_COMPRAS_TOOLTIP` evita um `title` gigante no e-mail; o restante é
    resumido em vez de sumir sem aviso.
    """
    total = len(compras)
    exibidas = list(compras)[:LIMITE_COMPRAS_TOOLTIP]
    partes = [
        f"{compra.get('fornecedor') or 'fornecedor não informado'} "
        f"({formatar_moeda(compra.get('preco'))})"
        for compra in exibidas
    ]
    if total > len(exibidas):
        partes.append(f"... e mais {total - len(exibidas)} compras")
    return ", ".join(partes)


def _dica_do_lado(linha, lado: str) -> str:
    """Tooltip de uma célula de preço (``antes`` ou ``atual``).

    Relatório de período: "Média de 01/11 a 30/11 - Compras: Fornecedor A (R$ 84,50),
    Fornecedor B (R$ 85,00), ... e mais N compras". Compra única (diário, ou período
    sem compra no intervalo): data e fornecedor da compra, como no e-mail diário.
    """
    compras = linha.get(f"compras_{lado}") or []
    periodo = linha.get(f"periodo_{lado}") or {}
    if compras and periodo.get("inicio") and periodo.get("fim"):
        return (
            f"Média de {formatar_data_curta(periodo['inicio'])} a "
            f"{formatar_data_curta(periodo['fim'])} - Compras: "
            f"{_lista_de_compras(compras)}"
        )
    rotulo = "Compra anterior" if lado == "anterior" else "Compra atual"
    data = formatar_data(linha.get(f"data_{lado}")) or "data não informada"
    fornecedor = str(linha.get(f"fornecedor_{lado}") or "").strip()
    if not fornecedor:
        fornecedor = (
            str(linha.get("fornecedor") or "").strip() or "fornecedor não informado"
        )
    return f"{rotulo}: {data} · {fornecedor}"


def _tabela_variacoes(variacoes, limite: int = LIMITE_LINHAS_EMAIL) -> str:
    """Tabela de variações (produto x última compra x impacto mensal)."""
    exibidas = variacoes[:limite]
    linhas = []
    for linha in exibidas:
        selos = _selos(linha)
        produto = html.escape(str(linha.get("produto") or ""))
        if selos:
            produto += (
                '<br><span style="font-size:11px;color:#93370d">'
                f"{html.escape(selos)}</span>"
            )
        # Relatório de período: cada preço recebe o tooltip da SUA média (com todas as
        # compras do intervalo). Diário: mantém o texto único com as duas compras.
        if linha.get("periodo_atual") or linha.get("periodo_anterior"):
            dica_antes = html.escape(_dica_do_lado(linha, "anterior"), quote=True)
            dica_agora = html.escape(_dica_do_lado(linha, "atual"), quote=True)
        else:
            dica_antes = dica_agora = html.escape(
                _dica_das_compras(linha), quote=True
            )
        # `title` + `cursor:help`: data e fornecedor de cada compra aparecem ao passar
        # o mouse sobre os preços Antes/Agora.
        linhas.append(
            "<tr>"
            f'<td style="padding:4px 6px;border-bottom:1px solid #e5e7eb">{produto}</td>'
            f'<td style="padding:4px 6px;border-bottom:1px solid #e5e7eb">'
            f'{html.escape(str(linha.get("loja") or ""))}</td>'
            f'<td title="{dica_antes}" style="padding:4px 6px;border-bottom:1px solid #e5e7eb;'
            f'text-align:right;cursor:help">'
            f'{formatar_moeda(linha["preco_anterior"])}</td>'
            f'<td title="{dica_agora}" style="padding:4px 6px;border-bottom:1px solid #e5e7eb;'
            f'text-align:right;cursor:help">'
            f'{formatar_moeda(linha["preco_atual"])}</td>'
            + _celula_variacao(linha["variacao_pct"])
            + '<td style="padding:4px 6px;border-bottom:1px solid #e5e7eb;'
            f"text-align:right;color:{_cor_do_valor(linha['impacto_mensal'])};"
            f'white-space:nowrap">'
            f'{formatar_moeda(linha["impacto_mensal"])}</td>'
            f'<td style="padding:4px 6px;border-bottom:1px solid #e5e7eb;text-align:right">'
            f'{linha["consumo_mensal"]:.2f}</td>'
            "</tr>"
        )
    if not linhas:
        return "<p>Nenhuma variação de preço nas compras deste período.</p>"
    resto = ""
    if len(variacoes) > len(exibidas):
        resto = (
            f'<p style="font-size:12px;color:#6b7280">… e mais '
            f"{len(variacoes) - len(exibidas)} produto(s) com variação "
            f"(lista limitada a {limite} itens no e-mail).</p>"
        )
    return (
        '<table style="width:100%;border-collapse:collapse;font-size:13px">'
        "<thead><tr>"
        '<th style="text-align:left;padding:6px;border-bottom:2px solid #d1d5db">Produto</th>'
        '<th style="text-align:left;padding:6px;border-bottom:2px solid #d1d5db">Loja</th>'
        '<th style="text-align:right;padding:6px;border-bottom:2px solid #d1d5db">Antes</th>'
        '<th style="text-align:right;padding:6px;border-bottom:2px solid #d1d5db">Agora</th>'
        '<th style="text-align:right;padding:6px;border-bottom:2px solid #d1d5db">Var.</th>'
        '<th style="text-align:right;padding:6px;border-bottom:2px solid #d1d5db">R$/mês</th>'
        '<th style="text-align:right;padding:6px;border-bottom:2px solid #d1d5db">Cons./mês</th>'
        "</tr></thead><tbody>" + "".join(linhas) + "</tbody></table>" + resto
    )


def _cartao(rotulo: str, valor: str, cor: str = "#1f2937") -> str:
    """Cartão de métrica do resumo (table, porque Outlook ignora flex/grid)."""
    return (
        '<td style="padding:10px 12px;border:1px solid #e5e7eb;border-radius:6px;'
        'vertical-align:top">'
        f'<div style="font-size:11px;color:#6b7280">{html.escape(rotulo)}</div>'
        f'<div style="font-size:18px;font-weight:700;color:{cor}">'
        f"{html.escape(valor)}</div></td>"
    )


def _tabela_novos(novos, limite: int = LIMITE_LINHAS_EMAIL) -> str:
    """Produtos comprados pela primeira vez na loja (sem preço anterior)."""
    if not novos:
        return ""
    linhas = []
    for linha in novos[:limite]:
        produto = html.escape(str(linha.get("produto") or ""))
        if linha.get("atrasada"):
            produto += (
                '<br><span style="font-size:11px;color:#93370d">'
                "⚠️ recebida com atraso</span>"
            )
        linhas.append(
            "<tr>"
            f'<td style="padding:4px 6px;border-bottom:1px solid #e5e7eb">{produto}</td>'
            f'<td style="padding:4px 6px;border-bottom:1px solid #e5e7eb">'
            f'{html.escape(str(linha.get("loja") or ""))}</td>'
            f'<td style="padding:4px 6px;border-bottom:1px solid #e5e7eb;text-align:right">'
            f'{formatar_moeda(linha["preco_atual"])}</td>'
            f'<td style="padding:4px 6px;border-bottom:1px solid #e5e7eb;text-align:right">'
            f'{linha["consumo_mensal"]:.2f}</td>'
            "</tr>"
        )
    return (
        "<h3 style=\"margin:22px 0 8px;font-size:15px\">🆕 Produtos novos "
        f"({len(novos)})</h3>"
        '<table style="width:100%;border-collapse:collapse;font-size:13px">'
        '<thead><tr><th style="text-align:left;padding:6px;border-bottom:2px solid #d1d5db">'
        'Produto</th><th style="text-align:left;padding:6px;border-bottom:2px solid #d1d5db">'
        'Loja</th><th style="text-align:right;padding:6px;border-bottom:2px solid #d1d5db">'
        'Preço</th><th style="text-align:right;padding:6px;border-bottom:2px solid #d1d5db">'
        "Cons./mês</th></tr></thead><tbody>"
        + "".join(linhas)
        + "</tbody></table>"
    )


def _inflacao_html(bloco) -> str:
    """Bloco da inflação: mês fechado, prévia de 30 dias e série histórica."""
    bloco = bloco or {}
    fechado = bloco.get("fechado") or {}
    previa = bloco.get("previa_30_dias") or {}
    partes = ['<h3 style="margin:22px 0 8px;font-size:15px">📈 Inflação das suas compras</h3>']
    if fechado.get("variacao_pct") is not None:
        aviso = " (amostra pequena)" if fechado.get("amostra_pequena") else ""
        partes.append(
            '<p style="margin:4px 0">Mês fechado <b>'
            f'{html.escape(str(fechado.get("rotulo") or ""))}</b>: '
            f'<b style="color:{_cor_do_valor(fechado.get("variacao_pct"))}">'
            f'{formatar_pct(fechado.get("variacao_pct"))}</b>{aviso}</p>'
        )
    else:
        partes.append("<p>Índice mensal ainda sem dois meses comparáveis.</p>")
    if previa.get("disponivel"):
        partes.append(
            '<p style="margin:4px 0">Últimos 30 dias (prévia): '
            f'<b style="color:{_cor_do_valor(previa.get("variacao_pct"))}">'
            f'{formatar_pct(previa.get("variacao_pct"))}</b></p>'
        )
    elif previa.get("motivo"):
        partes.append(
            f'<p style="margin:4px 0;color:#6b7280">{html.escape(previa["motivo"])}</p>'
        )
    serie = bloco.get("serie") or []
    if serie:
        # Só mês e variação: o índice/cobertura saiu do e-mail (pedido do cliente).
        celulas = "".join(
            "<tr>"
            f'<td style="padding:3px 6px;border-bottom:1px solid #e5e7eb">'
            f'{html.escape(str(item.get("rotulo") or ""))}</td>'
            f'<td style="padding:3px 6px;border-bottom:1px solid #e5e7eb;text-align:right;'
            f'color:{_cor_do_valor(item.get("variacao_pct"))}">'
            f'{formatar_pct(item.get("variacao_pct"))}</td>'
            "</tr>"
            for item in serie
        )
        partes.append(
            "<p style='margin:10px 0 4px'><b>Série histórica</b></p>"
            '<table style="width:100%;border-collapse:collapse;font-size:12px;margin-top:2px">'
            '<thead><tr><th style="text-align:left;padding:4px 6px;border-bottom:2px solid #d1d5db">'
            'Mês</th><th style="text-align:right;padding:4px 6px;border-bottom:2px solid #d1d5db">'
            "Variação</th></tr></thead><tbody>" + celulas + "</tbody></table>"
        )
    contribuicoes = (fechado or {}).get("contribuicoes") or []
    if contribuicoes:
        itens = "".join(
            "<li>"
            f'{html.escape(str(linha.get("produto") or ""))} '
            f'({formatar_pct(linha.get("variacao_pct"))}, peso '
            f'{str(linha.get("peso_pct")).replace(".", ",")}%)'
            "</li>"
            for linha in contribuicoes[:5]
        )
        partes.append(
            "<p style='margin:10px 0 4px'><b>Maiores responsáveis no mês</b></p>"
            f'<ul style="margin:0;padding-left:18px;font-size:12px">{itens}</ul>'
        )
    if bloco.get("metodologia"):
        partes.append(
            f'<p style="font-size:11px;color:#6b7280;margin-top:8px">'
            f'{html.escape(bloco["metodologia"])}</p>'
        )
    return "".join(partes)


def montar_html(relatorio) -> str:
    """HTML do e-mail (tabelas simples com CSS inline: Gmail/Outlook não usam o app)."""
    relatorio = relatorio or {}
    resumo = relatorio.get("resumo") or {}
    inicio = formatar_data(relatorio.get("inicio"))
    fim = formatar_data(relatorio.get("fim"))
    periodo = f"{inicio} a {fim}" if inicio and fim else "período atual"
    lojas = relatorio.get("lojas") or []
    nomes_lojas = ", ".join(
        html.escape(str(loja.get("loja") or "")) for loja in lojas
    ) or "todas as lojas da conta"
    liquido = float(resumo.get("impacto_liquido") or 0.0)
    cor_liquido = _cor_do_valor(liquido)

    cartoes = "".join(
        (
            '<tr valign="top">'
            + _cartao("Aumentos", str(resumo.get("aumentos") or 0), COR_NEGATIVA)
            + _cartao("Quedas", str(resumo.get("quedas") or 0), "#067647")
            + _cartao("Impacto estimado/mês", formatar_moeda(liquido), cor_liquido)
            + "</tr>"
        )
    )
    lojas_html = ""
    if lojas:
        linhas_lojas = "".join(
            "<tr>"
            f'<td style="padding:4px 6px;border-bottom:1px solid #e5e7eb">'
            f'{html.escape(str(loja.get("loja") or ""))}</td>'
            f'<td style="padding:4px 6px;border-bottom:1px solid #e5e7eb;text-align:right">'
            f'{loja.get("aumentos")}</td>'
            f'<td style="padding:4px 6px;border-bottom:1px solid #e5e7eb;text-align:right">'
            f'{loja.get("quedas")}</td>'
            f'<td style="padding:4px 6px;border-bottom:1px solid #e5e7eb;text-align:right;'
            f'font-weight:600">{formatar_moeda(loja.get("impacto"))}</td>'
            "</tr>"
            for loja in lojas
        )
        lojas_html = (
            "<h3 style='margin:22px 0 8px;font-size:15px'>🏬 Resumo por loja</h3>"
            '<table style="width:100%;border-collapse:collapse;font-size:13px">'
            '<thead><tr><th style="text-align:left;padding:6px;border-bottom:2px solid #d1d5db">'
            'Loja</th><th style="text-align:right;padding:6px;border-bottom:2px solid #d1d5db">'
            'Aumentos</th><th style="text-align:right;padding:6px;border-bottom:2px solid #d1d5db">'
            'Quedas</th><th style="text-align:right;padding:6px;border-bottom:2px solid #d1d5db">'
            "Impacto/mês</th></tr></thead><tbody>" + linhas_lojas + "</tbody></table>"
        )

    aviso_atrasadas = ""
    if resumo.get("atrasadas"):
        aviso_atrasadas = (
            '<p style="margin:8px 0;padding:8px 10px;background:#fffaeb;'
            'border-left:4px solid #f79009;font-size:12px">'
            f"⚠️ {resumo['atrasadas']} nota(s) deste relatório foram recebidas com "
            "atraso (importadas depois da data de emissão).</p>"
        )

    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;max-width:760px;'
        'margin:0 auto;color:#1f2937;line-height:1.45">'
        f"<h2 style=\"margin:0 0 4px;font-size:20px\">🔔 {html.escape(str(relatorio.get('titulo') or ''))}</h2>"
        f'<p style="margin:0 0 2px;color:#4b5563;font-size:13px">Período: <b>{periodo}</b>'
        f" • Lojas: <b>{nomes_lojas}</b></p>"
        '<p style="margin:0 0 14px;color:#6b7280;font-size:12px">Toda variação de preço '
        "de pelo menos R$ 0,01 por unidade, em relação à última compra do mesmo produto "
        "na mesma loja (passe o mouse sobre o preço para ver a data e o fornecedor de "
        "cada compra). O R$/mês usa o consumo médio dos últimos 90 dias. Valores "
        "negativos aparecem em vermelho.</p>"
        f'<table style="width:100%;border-collapse:separate;border-spacing:6px 0">{cartoes}</table>'
        f"{aviso_atrasadas}"
        "<h3 style='margin:22px 0 8px;font-size:15px'>💰 Variações de preço</h3>"
        f"{_tabela_variacoes(relatorio.get('variacoes') or [])}"
        f"{lojas_html}"
        f"{_tabela_novos(relatorio.get('novos') or [])}"
        f"{_inflacao_html(relatorio.get('inflacao') or {})}"
        '<p style="margin-top:20px;font-size:11px;color:#6b7280">'
        "Relatório gerado automaticamente a partir das notas fiscais importadas. "
        "Bonificação, doação e descrições pendentes na Auditoria ficam de fora, como "
        "nos relatórios do sistema.</p>"
        "</div>"
    )


def _resumo_do_lado_texto(item, lado: str) -> str:
    """Preço + origem no texto puro: média do período ou data/fornecedor da compra."""
    preco = formatar_moeda(item.get(f"preco_{lado}"))
    compras = item.get(f"compras_{lado}") or []
    periodo = item.get(f"periodo_{lado}") or {}
    if compras and periodo.get("inicio") and periodo.get("fim"):
        return (
            f"{preco} [média de {formatar_data_curta(periodo['inicio'])} a "
            f"{formatar_data_curta(periodo['fim'])} · {len(compras)} compra(s)]"
        )
    data = formatar_data(item.get(f"data_{lado}")) or "sem data"
    fornecedor = str(item.get(f"fornecedor_{lado}") or "").strip()
    if not fornecedor:
        fornecedor = (
            str(item.get("fornecedor") or "").strip() or "fornecedor não informado"
        )
    return f"{preco} [{data} · {fornecedor}]"


def montar_texto(relatorio) -> str:
    """Versão texto puro (clientes de e-mail que não abrem HTML)."""
    relatorio = relatorio or {}
    resumo = relatorio.get("resumo") or {}
    linhas = [
        str(relatorio.get("titulo") or "Relatório de Compras"),
        f"Período: {formatar_data(relatorio.get('inicio'))} a "
        f"{formatar_data(relatorio.get('fim'))}",
        "",
        f"Aumentos: {resumo.get('aumentos') or 0} | "
        f"Quedas: {resumo.get('quedas') or 0} | "
        f"Impacto estimado/mês: {formatar_moeda(resumo.get('impacto_liquido'))}",
        "",
        "Variações de preço:",
    ]
    variacoes = relatorio.get("variacoes") or []
    for item in variacoes[:LIMITE_LINHAS_EMAIL]:
        selos = _selos(item)
        linhas.append(
            f"- {item['produto']} ({item['loja']}): "
            f"{_resumo_do_lado_texto(item, 'anterior')} -> "
            f"{_resumo_do_lado_texto(item, 'atual')} "
            f"({formatar_pct(item['variacao_pct'])}) | "
            f"{formatar_moeda(item['impacto_mensal'])}/mês"
            + (f" [{selos}]" if selos else "")
        )
    if not variacoes:
        linhas.append("- Nenhuma variação de preço nas compras deste período.")
    inflacao = relatorio.get("inflacao") or {}
    fechado = inflacao.get("fechado") or {}
    previa = inflacao.get("previa_30_dias") or {}
    serie = inflacao.get("serie") or []
    linhas.extend(["", "Inflação das suas compras:"])
    if fechado.get("variacao_pct") is not None:
        linhas.append(
            f"- Mês fechado {fechado.get('rotulo')}: "
            f"{formatar_pct(fechado.get('variacao_pct'))}"
        )
    if previa.get("disponivel"):
        linhas.append(
            f"- Últimos 30 dias: {formatar_pct(previa.get('variacao_pct'))}"
        )
    elif previa.get("motivo"):
        linhas.append(f"- {previa['motivo']}")
    if serie:
        linhas.append(
            "- Série histórica: "
            + " | ".join(
                f"{registro.get('rotulo')}: {formatar_pct(registro.get('variacao_pct'))}"
                for registro in serie
            )
        )
    return "\n".join(linhas)







