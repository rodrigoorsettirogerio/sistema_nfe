"""Índice de inflação das compras (Laspeyres encadeado).

O índice responde "quanto os MESMOS produtos que eu comprava antes custam mais hoje?".
A metodologia foi fechada com o cliente:

1. **Cesta do período ANTERIOR**: cada produto (chave do grupo dos relatórios) entra
   com o peso que ele tinha no gasto do período anterior — preço médio pago x
   quantidade comprada. Produto novo, que não existia na cesta, NÃO entra no índice
   (senão a inflação mediria mudança de mix, não de preço).
2. **Laspeyres**: para cada produto da cesta com preço nos dois períodos,
   ``razão = preço_atual / preço_anterior``; o índice é a média das razões ponderada
   pelo peso. Produto que sumiu do período atual fica FORA da conta, mas reduz a
   **cobertura** (peso comparado / peso total da cesta) — cobertura baixa é sinal de
   amostra pequena e vai marcada no relatório.
3. **Série histórica**: a série de 6 meses usa, em cada mês, a cesta do mês
   anterior. O índice oficial é mensal (fechado); a "prévia semanal" usa janelas
   móveis de 30 dias e serve só como tendência.

`memoria_calculo` devolve a conta ABERTA (produto a produto) do mês fechado, para o
cliente conferir no Excel o mesmo número que aparece no relatório.

Funções puras: recebem os itens de `main._carregar_itens_relatorio` (dicts com
``data``, ``chave``, ``cnpj``, ``valor``/``valor_unitario_base`` e
``quantidade``/``quantidade_base``) e não tocam no banco.
"""

from datetime import date, timedelta

# Abaixo disso o índice é "amostra pequena": menos de 60% do gasto da cesta anterior
# encontrou o mesmo produto no período atual.
COBERTURA_MINIMA_PCT = 60.0
# Quantos produtos entram na lista de "maiores responsáveis" do relatório.
LIMITE_CONTRIBUINTES = 5
# Meses exibidos na série encadeada (inclui o mês fechado mais recente).
MESES_SERIE = 6

ROTULOS_MESES = (
    "jan",
    "fev",
    "mar",
    "abr",
    "mai",
    "jun",
    "jul",
    "ago",
    "set",
    "out",
    "nov",
    "dez",
)


def preco_unitario(item) -> float:
    """Preço por unidade-base (usa a conversão de embalagem quando existe)."""
    base = float(item.get("valor_unitario_base") or 0.0)
    if base > 0:
        return base
    return float(item.get("valor") or 0.0)


def quantidade_base(item) -> float:
    """Quantidade em unidades-base (CX24 -> 24 unidades), nunca negativa."""
    base = float(item.get("quantidade_base") or 0.0)
    if base > 0:
        return base
    return max(0.0, float(item.get("quantidade") or 0.0))


def chave_cesta(item, por_loja: bool = False) -> str:
    """Chave do produto na cesta (o mesmo grupo dos relatórios).

    ``por_loja=True`` separa a cesta por CNPJ — usado no relatório por loja, para o
    preço de uma loja não ser comparado com o de outra.
    """
    chave = str(item.get("chave") or "").strip()
    if not por_loja:
        return chave
    return f"{item.get('cnpj') or ''}|{chave}"


def cesta(itens, por_loja: bool = False) -> dict:
    """Peso (gasto) e preço médio de cada produto do período.

    Devolve ``{chave: {"descricao", "peso", "quantidade", "preco", "loja"}}``.
    ``preco`` é o preço médio pago por unidade-base no período (gasto / quantidade),
    que é o valor correto para Laspeyres (não a média simples dos itens).
    """
    grupos = {}
    for item in itens or []:
        preco = preco_unitario(item)
        quantidade = quantidade_base(item)
        if preco <= 0 or quantidade <= 0:
            continue
        chave = chave_cesta(item, por_loja)
        if not chave:
            continue
        registro = grupos.setdefault(
            chave,
            {
                "descricao": str(item.get("descricao") or "").strip(),
                "loja": str(item.get("loja") or ""),
                "gasto": 0.0,
                "quantidade": 0.0,
            },
        )
        registro["gasto"] += preco * quantidade
        registro["quantidade"] += quantidade
        # Descrição mais recente do grupo (os itens vêm em ordem de compra).
        if item.get("descricao"):
            registro["descricao"] = str(item["descricao"]).strip()
    for registro in grupos.values():
        registro["peso"] = registro["gasto"]
        registro["preco"] = (
            registro["gasto"] / registro["quantidade"]
            if registro["quantidade"] > 0
            else 0.0
        )
    return grupos


def indice_entre_periodos(itens_anterior, itens_atual, por_loja: bool = False) -> dict:
    """Variação (%) dos produtos da cesta anterior entre os dois períodos.

    Devolve ``None`` quando não há nenhum produto comparável. ``cobertura_pct`` é a
    fatia do gasto da cesta anterior que encontrou o mesmo produto no período atual;
    ``contribuicoes`` lista os produtos que mais pesaram na variação (peso x variação).
    """
    anterior = cesta(itens_anterior, por_loja)
    atual = cesta(itens_atual, por_loja)
    gasto_total = sum(registro["peso"] for registro in anterior.values())
    if gasto_total <= 0:
        return None

    soma_ponderada = 0.0
    peso_comparado = 0.0
    comparados = 0
    contribuicoes = []
    for chave, registro in anterior.items():
        preco_anterior = float(registro.get("preco") or 0.0)
        registro_atual = atual.get(chave)
        preco_atual = float((registro_atual or {}).get("preco") or 0.0)
        if preco_anterior <= 0 or preco_atual <= 0:
            continue
        peso = registro["peso"] / gasto_total  # participação no gasto anterior
        variacao = (preco_atual / preco_anterior - 1) * 100
        soma_ponderada += peso * variacao
        peso_comparado += peso
        comparados += 1
        contribuicoes.append(
            {
                "produto": registro.get("descricao") or "Produto sem descrição",
                "loja": registro.get("loja") or "",
                "preco_anterior": round(preco_anterior, 4),
                "preco_atual": round(preco_atual, 4),
                "variacao_pct": round(variacao, 2),
                "peso_pct": round(peso * 100, 2),
                "contribuicao_pct": round(peso * variacao, 2),
            }
        )

    if comparados == 0 or peso_comparado <= 0:
        return None
    # Renormaliza pelo peso COMPARADO: produtos que sumiram não "diluem" a inflação,
    # apenas reduzem a cobertura (informada separadamente).
    variacao = soma_ponderada / peso_comparado
    cobertura = peso_comparado * 100
    contribuicoes.sort(key=lambda linha: abs(linha["contribuicao_pct"]), reverse=True)
    return {
        "indice": round(100 + variacao, 2),
        "variacao_pct": round(variacao, 2),
        "cobertura_pct": round(cobertura, 1),
        "amostra_pequena": cobertura < COBERTURA_MINIMA_PCT,
        "itens_cesta": len(anterior),
        "itens_comparados": comparados,
        "contribuicoes": contribuicoes[:LIMITE_CONTRIBUINTES],
    }


def memoria_calculo(itens, referencia: date = None, por_loja: bool = False) -> dict:
    """Conta ABERTA do índice do mês fechado, produto a produto (conferência).

    Refaz exatamente o cálculo de `indice_entre_periodos` entre o mês anterior (cesta
    e pesos) e o mês fechado (preços), mas devolve TODAS as linhas — inclusive as que
    ficaram fora do índice por não terem preço no mês seguinte — com quantidade,
    gasto, preço médio, peso, variação e contribuição de cada produto.

    É a base do Excel de memória de cálculo (`backend/memoria_calculo.py`): com esses
    números o cliente reproduz o índice na planilha (preço = gasto / quantidade; peso =
    gasto do produto / gasto total; variação = preço atual / preço anterior - 1;
    variação do mês = soma ponderada / peso comparado).

    Devolve ``None`` quando não há gasto no mês anterior (nada para indexar).
    """
    if referencia is None:
        datas = [item["data"] for item in itens or [] if item.get("data")]
        referencia = max(datas) if datas else None
    if referencia is None:
        return None
    ano, mes = referencia.year, referencia.month
    ano_ant, mes_ant = mes_anterior(ano, mes)
    inicio_ant, fim_ant = intervalo_mes(ano_ant, mes_ant)
    inicio, fim = intervalo_mes(ano, mes)
    itens_anterior = itens_no_intervalo(itens, inicio_ant, fim_ant)
    itens_atual = itens_no_intervalo(itens, inicio, fim)
    anterior = cesta(itens_anterior, por_loja)
    atual = cesta(itens_atual, por_loja)
    gasto_total = sum(float(registro["peso"]) for registro in anterior.values())
    if gasto_total <= 0:
        return None

    # Lojas de cada produto: o índice agrega a mesma chave em todas as lojas.
    lojas_por_chave = {}
    for item in itens_anterior:
        chave = chave_cesta(item, por_loja)
        if not chave:
            continue
        lojas = lojas_por_chave.setdefault(chave, [])
        loja = str(item.get("loja") or "").strip()
        if loja and loja not in lojas:
            lojas.append(loja)

    linhas = []
    soma_ponderada = 0.0  # em pontos percentuais (peso x variação)
    peso_comparado = 0.0  # fração do gasto da cesta que entrou na conta
    comparados = 0
    for chave, registro in anterior.items():
        preco_anterior = float(registro.get("preco") or 0.0)
        registro_atual = atual.get(chave) or {}
        preco_atual = float(registro_atual.get("preco") or 0.0)
        peso = float(registro["peso"]) / gasto_total
        comparado = preco_anterior > 0 and preco_atual > 0
        variacao = (preco_atual / preco_anterior - 1) * 100 if comparado else None
        contribuicao = peso * variacao if comparado else None
        if comparado:
            soma_ponderada += peso * variacao
            peso_comparado += peso
            comparados += 1
        linhas.append(
            {
                "produto": registro.get("descricao") or "Produto sem descrição",
                "lojas": " · ".join(lojas_por_chave.get(chave) or []),
                "quantidade_anterior": round(
                    float(registro.get("quantidade") or 0.0), 3
                ),
                "gasto_anterior": round(float(registro.get("gasto") or 0.0), 4),
                "preco_anterior": round(preco_anterior, 4),
                "peso_pct": round(peso * 100, 4),
                "quantidade_atual": round(
                    float(registro_atual.get("quantidade") or 0.0), 3
                ),
                "gasto_atual": round(float(registro_atual.get("gasto") or 0.0), 4),
                "preco_atual": round(preco_atual, 4),
                "variacao_pct": round(variacao, 4) if comparado else None,
                "contribuicao_pct": round(contribuicao, 4) if comparado else None,
                "comparado": comparado,
                "motivo_fora": (
                    None if comparado else "Sem preço no mês seguinte (produto não comprado)"
                ),
            }
        )
    # Maiores contribuições primeiro; produtos fora do índice vão para o fim.
    linhas.sort(
        key=lambda linha: (
            linha["contribuicao_pct"] is None,
            -abs(linha["contribuicao_pct"] or 0.0),
            -linha["peso_pct"],
        )
    )

    variacao_periodo = None
    if comparados and peso_comparado > 0:
        variacao_periodo = soma_ponderada / peso_comparado
    return {
        "mes_anterior": {
            "ano": ano_ant,
            "mes": mes_ant,
            "rotulo": rotulo_mes(ano_ant, mes_ant),
            "inicio": inicio_ant.isoformat(),
            "fim": fim_ant.isoformat(),
        },
        "mes_atual": {
            "ano": ano,
            "mes": mes,
            "rotulo": rotulo_mes(ano, mes),
            "inicio": inicio.isoformat(),
            "fim": fim.isoformat(),
        },
        "gasto_total_anterior": round(gasto_total, 2),
        "gasto_total_atual": round(
            sum(float(registro["peso"]) for registro in atual.values()), 2
        ),
        "itens_cesta": len(anterior),
        "itens_comparados": comparados,
        # Soma ponderada em pontos percentuais (peso x variação, produto a produto).
        "soma_ponderada_pp": round(soma_ponderada, 4),
        "peso_comparado_pct": round(peso_comparado * 100, 2),
        "cobertura_pct": round(peso_comparado * 100, 1),
        "amostra_pequena": comparados > 0
        and peso_comparado * 100 < COBERTURA_MINIMA_PCT,
        "variacao_pct": (
            round(variacao_periodo, 2) if variacao_periodo is not None else None
        ),
        "indice": (
            round(100 + variacao_periodo, 2) if variacao_periodo is not None else None
        ),
        "linhas": linhas,
        "serie": serie_mensal(itens, referencia),
    }


def intervalo_mes(ano: int, mes: int) -> tuple:
    """(primeiro dia, último dia) de um mês."""
    primeiro = date(ano, mes, 1)
    if mes == 12:
        proximo = date(ano + 1, 1, 1)
    else:
        proximo = date(ano, mes + 1, 1)
    return primeiro, proximo - timedelta(days=1)


def mes_anterior(ano: int, mes: int) -> tuple:
    """(ano, mês) imediatamente anterior."""
    if mes == 1:
        return ano - 1, 12
    return ano, mes - 1


def rotulo_mes(ano: int, mes: int) -> str:
    """'2026-03' -> 'mar/26' (rótulo curto dos gráficos/tabelas)."""
    return f"{ROTULOS_MESES[mes - 1]}/{str(ano)[-2:]}"


def itens_no_intervalo(itens, inicio: date, fim: date) -> list:
    return [
        item
        for item in itens or []
        if item.get("data") is not None and inicio <= item["data"] <= fim
    ]


def serie_mensal(itens, referencia: date = None, meses: int = MESES_SERIE) -> list:
    """Série histórica dos últimos `meses` meses fechados (base 100 no primeiro).

    Cada elo compara o mês com o anterior usando a cesta do mês anterior. Mês sem
    cesta/comparação aparece com ``indice=None`` (a corrente segue no mês seguinte
    com dado), para o relatório nunca "inventar" um número.
    """
    if referencia is None:
        datas = [item["data"] for item in itens or [] if item.get("data")]
        referencia = max(datas) if datas else None
    if referencia is None:
        return []
    ano, mes = referencia.year, referencia.month
    periodos = []
    for _ in range(meses):
        ano_ant, mes_ant = mes_anterior(ano, mes)
        periodos.append((ano_ant, mes_ant, ano, mes))
        ano, mes = ano_ant, mes_ant
    periodos.reverse()  # do mais antigo para o mais recente

    serie = []
    indice_acumulado = 100.0
    primeiro = True
    for ano_ant, mes_ant, ano, mes in periodos:
        inicio_ant, fim_ant = intervalo_mes(ano_ant, mes_ant)
        inicio, fim = intervalo_mes(ano, mes)
        if primeiro:
            # O primeiro mês é a base da série (não existe comparação anterior).
            serie.append(
                {
                    "mes": f"{ano_ant:04d}-{mes_ant:02d}",
                    "rotulo": rotulo_mes(ano_ant, mes_ant),
                    "indice": round(indice_acumulado, 2),
                    "variacao_pct": None,
                    "cobertura_pct": None,
                    "amostra_pequena": False,
                }
            )
            primeiro = False
        resultado = indice_entre_periodos(
            itens_no_intervalo(itens, inicio_ant, fim_ant),
            itens_no_intervalo(itens, inicio, fim),
        )
        if resultado is None:
            serie.append(
                {
                    "mes": f"{ano:04d}-{mes:02d}",
                    "rotulo": rotulo_mes(ano, mes),
                    "indice": None,
                    "variacao_pct": None,
                    "cobertura_pct": None,
                    "amostra_pequena": True,
                }
            )
            continue
        indice_acumulado *= 1 + resultado["variacao_pct"] / 100
        serie.append(
            {
                "mes": f"{ano:04d}-{mes:02d}",
                "rotulo": rotulo_mes(ano, mes),
                "indice": round(indice_acumulado, 2),
                "variacao_pct": resultado["variacao_pct"],
                "cobertura_pct": resultado["cobertura_pct"],
                "amostra_pequena": resultado["amostra_pequena"],
            }
        )
    return serie


def previa_30_dias(itens, referencia: date = None) -> dict:
    """Tendência das últimas 4 semanas contra as 4 anteriores (janelas de 30 dias).

    É a "prévia semanal": mostra para onde a inflação está indo entre um índice
    mensal fechado e o próximo, comparando os mesmos produtos nas duas janelas.
    """
    if referencia is None:
        datas = [item["data"] for item in itens or [] if item.get("data")]
        referencia = max(datas) if datas else None
    if referencia is None:
        return {"disponivel": False, "motivo": "Sem compras no período."}
    inicio_atual = referencia - timedelta(days=29)
    fim_anterior = inicio_atual - timedelta(days=1)
    inicio_anterior = fim_anterior - timedelta(days=29)
    resultado = indice_entre_periodos(
        itens_no_intervalo(itens, inicio_anterior, fim_anterior),
        itens_no_intervalo(itens, inicio_atual, referencia),
    )
    janelas = {
        "inicio": inicio_atual.isoformat(),
        "fim": referencia.isoformat(),
        "inicio_anterior": inicio_anterior.isoformat(),
        "fim_anterior": fim_anterior.isoformat(),
    }
    if resultado is None:
        return {
            "disponivel": False,
            "motivo": (
                "Ainda não há o mesmo produto comprado nas duas janelas de 30 dias."
            ),
            **janelas,
        }
    resultado.update({"disponivel": True, **janelas})
    return resultado
