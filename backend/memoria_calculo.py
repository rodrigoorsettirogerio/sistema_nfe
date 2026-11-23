"""Memória de cálculo da inflação em Excel (.xlsx) — conferência do cliente.

O relatório por e-mail mostra só o resultado (ex.: "out/26: +2,92%"). Esta planilha
abre a conta do mês fechado:

- **Resumo**: os totais da conta (gasto da cesta, peso comparado, soma ponderada,
  inflação do mês) com a explicação de cada um;
- **Comparação**: um produto por linha, com quantidade, gasto e preço médio do mês
  anterior e do mês atual, peso no gasto, variação e contribuição — todas as células
  finais são FÓRMULAS do Excel, então o cliente confere clique a clique;
- **Série histórica**: a variação de cada mês fechado (a mesma do e-mail);
- **Metodologia**: cada passo da conta escrito em português, com os números do mês.

Fonte dos números: `backend.inflacao.memoria_calculo` (mesma base e mesmas regras de
`main._carregar_itens_relatorio`, ou seja, bonificação/CFOP 5910/6910 e descrições
pendentes na Auditoria ficam fora). O arquivo é montado em memória: nada é gravado no
disco do servidor.
"""

from io import BytesIO

from openpyxl import Workbook
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

AZUL = "1F4E78"
VERMELHO = "B42318"
BORDA = Border(bottom=Side(style="thin", color="D1D5DB"))

FORMATO_MOEDA = 'R$ #,##0.00'
FORMATO_PRECO = '0.0000'
FORMATO_PERCENTUAL = '0.00%'
FORMATO_PONTOS_PERCENTUAIS = '0.00" p.p."'

CABECALHO_COMPARACAO = [
    "Produto",
    "Lojas",
    "Quantidade (mês anterior)",
    "Gasto (mês anterior)",
    "Preço médio (mês anterior)",
    "Peso no gasto anterior",
    "Quantidade (mês atual)",
    "Gasto (mês atual)",
    "Preço médio (mês atual)",
    "Variação do preço",
    "Contribuição no mês",
    "Entra na conta?",
]


def _titulo_aba(texto: str) -> str:
    """Nome de aba válido no Excel: 'set/26 x out/26' -> 'set-26 x out-26'.

    O Excel recusa ``: \\ / ? * [ ]`` e limita o nome a 31 caracteres; a barra vira
    hífen para o rótulo continuar legível (é só o nome da aba).
    """
    limpo = str(texto or "").replace("/", "-")
    limpo = "".join(c for c in limpo if c not in ':\\?*[]').strip()
    return (limpo or "Planilha")[:31]


def _formatar_data(iso: str) -> str:
    """'2026-09-30' -> '30/09/2026' (padrão brasileiro da planilha)."""
    texto = str(iso or "")
    if len(texto) >= 10 and texto[4] == "-" and texto[7] == "-":
        return f"{texto[8:10]}/{texto[5:7]}/{texto[0:4]}"
    return texto


def _escrever_cabecalho(planilha, linha: int, colunas) -> None:
    """Cabeçalho azul em negrito, centralizado e com quebra de linha."""
    for coluna, titulo in enumerate(colunas, start=1):
        celula = planilha.cell(row=linha, column=coluna, value=titulo)
        celula.font = Font(bold=True, color="FFFFFF")
        celula.fill = PatternFill("solid", fgColor=AZUL)
        celula.alignment = Alignment(
            horizontal="center", vertical="center", wrap_text=True
        )
        celula.border = BORDA


def _coluna(indice: int) -> str:
    return get_column_letter(indice)


def _aba_comparacao(planilha, memoria) -> int:
    """Produto a produto, com fórmulas reais do Excel (conferência clique a clique).

    Devolve a linha da última linha de dados (0 quando não há produtos).
    """
    anterior = memoria["mes_anterior"]
    atual = memoria["mes_atual"]
    planilha["A1"] = (
        f"Comparação {anterior['rotulo']} (cesta e pesos) x {atual['rotulo']} (preços)"
    )
    planilha["A1"].font = Font(bold=True, size=14, color=AZUL)
    planilha["A2"] = (
        "Preço médio = gasto / quantidade. Peso = gasto do produto / gasto total da "
        "cesta. Variação = preço atual / preço anterior - 1. Contribuição = peso x "
        "variação (em pontos percentuais). As células dessas colunas são fórmulas."
    )
    planilha["A2"].font = Font(color="6B7280")
    planilha["A2"].alignment = Alignment(wrap_text=True)
    planilha.merge_cells("A2:L2")
    planilha.row_dimensions[2].height = 30

    _escrever_cabecalho(planilha, 4, CABECALHO_COMPARACAO)

    linhas = memoria.get("linhas") or []
    primeira = 5
    if not linhas:
        planilha.cell(
            row=primeira,
            column=1,
            value="Nenhum produto na cesta do mês anterior — nada para comparar.",
        )
        return 0

    ultima = primeira + len(linhas) - 1
    total_linha = ultima + 1
    for deslocamento, item in enumerate(linhas):
        linha = primeira + deslocamento
        planilha.cell(row=linha, column=1, value=item.get("produto"))
        planilha.cell(row=linha, column=2, value=item.get("lojas"))
        planilha.cell(
            row=linha, column=3, value=round(item.get("quantidade_anterior") or 0, 3)
        ).number_format = "0.000"
        planilha.cell(
            row=linha, column=4, value=round(item.get("gasto_anterior") or 0, 2)
        ).number_format = FORMATO_MOEDA
        planilha.cell(
            row=linha,
            column=5,
            value=f'=IF(C{linha}>0,D{linha}/C{linha},"")',
        ).number_format = FORMATO_PRECO
        planilha.cell(
            row=linha,
            column=6,
            value=f'=IF($D${total_linha}=0,"",D{linha}/$D${total_linha})',
        ).number_format = FORMATO_PERCENTUAL
        planilha.cell(
            row=linha, column=7, value=round(item.get("quantidade_atual") or 0, 3)
        ).number_format = "0.000"
        planilha.cell(
            row=linha, column=8, value=round(item.get("gasto_atual") or 0, 2)
        ).number_format = FORMATO_MOEDA
        planilha.cell(
            row=linha,
            column=9,
            value=f'=IF(G{linha}>0,H{linha}/G{linha},"")',
        ).number_format = FORMATO_PRECO
        planilha.cell(
            row=linha,
            column=10,
            value=f'=IF(AND(E{linha}>0,I{linha}>0),I{linha}/E{linha}-1,"")',
        ).number_format = FORMATO_PERCENTUAL
        planilha.cell(
            row=linha,
            column=11,
            value=f'=IF(J{linha}="","",F{linha}*J{linha}*100)',
        ).number_format = FORMATO_PONTOS_PERCENTUAIS
        planilha.cell(
            row=linha,
            column=12,
            value=(
                "Sim"
                if item.get("comparado")
                else "Não — produto não comprado no mês atual"
            ),
        )
        for coluna in range(1, 13):
            planilha.cell(row=linha, column=coluna).border = BORDA

    # Linha de total: só somas (a variação ponderada aparece logo abaixo).
    planilha.cell(row=total_linha, column=1, value="TOTAL").font = Font(bold=True)
    for coluna, formato in (
        (4, FORMATO_MOEDA),
        (6, FORMATO_PERCENTUAL),
        (8, FORMATO_MOEDA),
        (11, FORMATO_PONTOS_PERCENTUAIS),
    ):
        letra = _coluna(coluna)
        celula = planilha.cell(
            row=total_linha,
            column=coluna,
            value=f"=SUM({letra}{primeira}:{letra}{ultima})",
        )
        celula.font = Font(bold=True)
        celula.number_format = formato
        celula.fill = PatternFill("solid", fgColor="E5E7EB")

    peso_linha = total_linha + 2
    soma_linha = total_linha + 3
    inflacao_linha = total_linha + 4
    indice_linha = total_linha + 5
    passos = [
        (
            peso_linha,
            "Peso comparado (produtos com 'Sim')",
            f'=SUMIF($L${primeira}:$L${ultima},"Sim",$F${primeira}:$F${ultima})',
            FORMATO_PERCENTUAL,
        ),
        (
            soma_linha,
            "Soma ponderada (soma das contribuições)",
            f"=SUM($K${primeira}:$K${ultima})",
            FORMATO_PONTOS_PERCENTUAIS,
        ),
        (
            inflacao_linha,
            "Inflação do mês = soma ponderada / (peso comparado x 100)",
            f'=IFERROR(B{soma_linha}/(B{peso_linha}*100),"")',
            FORMATO_PERCENTUAL,
        ),
        (
            indice_linha,
            "Índice do mês (base 100)",
            f'=IF(B{inflacao_linha}="","",100*(1+B{inflacao_linha}))',
            "0.00",
        ),
    ]
    for linha, rotulo, formula, formato in passos:
        planilha.cell(row=linha, column=1, value=rotulo).font = Font(bold=True)
        celula = planilha.cell(row=linha, column=2, value=formula)
        celula.number_format = formato
        celula.font = Font(bold=True)
        celula.fill = PatternFill("solid", fgColor="FEF3C7")

    # Negativos em vermelho também na planilha (variação e contribuição).
    for coluna in (10, 11):
        letra = _coluna(coluna)
        planilha.conditional_formatting.add(
            f"{letra}{primeira}:{letra}{ultima}",
            CellIsRule(
                operator="lessThan",
                formula=["0"],
                font=Font(color=VERMELHO, bold=True),
            ),
        )

    larguras = {
        "A": 52,
        "B": 26,
        "C": 14,
        "D": 16,
        "E": 16,
        "F": 14,
        "G": 14,
        "H": 16,
        "I": 16,
        "J": 13,
        "K": 15,
        "L": 38,
    }
    for coluna, largura in larguras.items():
        planilha.column_dimensions[coluna].width = largura
    planilha.freeze_panes = f"A{primeira}"
    planilha.auto_filter.ref = f"A4:L{ultima}"
    return ultima


def _aba_resumo(planilha, memoria, linhas_aba: str) -> None:

    planilha["A1"] = "Memória de cálculo da inflação das suas compras"
    planilha["A1"].font = Font(bold=True, size=14, color=AZUL)

    anterior = memoria["mes_anterior"]
    atual = memoria["mes_atual"]
    planilha["A2"] = f"Período comparado: {anterior['rotulo']} -> {atual['rotulo']}"
    planilha["A2"].font = Font(bold=True)
    planilha["A3"] = (
        f"Cesta: {_formatar_data(anterior['inicio'])} a "
        f"{_formatar_data(anterior['fim'])} | "
        f"Preços: {_formatar_data(atual['inicio'])} a {_formatar_data(atual['fim'])}"
    )
    planilha["A3"].font = Font(color="6B7280")

    _escrever_cabecalho(planilha, 5, ["Item", "Valor", "Como foi calculado"])

    variacao = memoria.get("variacao_pct")
    valores = [
        (
            f"Gasto total da cesta ({anterior['rotulo']})",
            memoria.get("gasto_total_anterior"),
            "Soma do valor total dos itens válidos do mês (bonificação e descrições "
            "pendentes na Auditoria ficam fora)",
            FORMATO_MOEDA,
        ),
        (
            f"Gasto total ({atual['rotulo']})",
            memoria.get("gasto_total_atual"),
            "Serve só de referência: o peso de cada produto é o do mês anterior",
            FORMATO_MOEDA,
        ),
        (
            "Produtos na cesta",
            memoria.get("itens_cesta"),
            "Um item por produto (mesma chave do relatório: EAN/descrição + marca + "
            "embalagem)",
            "0",
        ),
        (
            "Produtos comparados",
            memoria.get("itens_comparados"),
            "Produtos da cesta que também foram comprados no mês seguinte",
            "0",
        ),
        (
            "Peso comparado",
            (memoria.get("peso_comparado_pct") or 0) / 100,
            "Soma do peso dos produtos comparados (o resto não tinha preço no mês "
            "seguinte)",
            FORMATO_PERCENTUAL,
        ),
        (
            "Soma ponderada (peso x variação)",
            memoria.get("soma_ponderada_pp"),
            f"Soma da coluna 'Contribuição no mês' da aba '{linhas_aba}'",
            FORMATO_PONTOS_PERCENTUAIS,
        ),
        (
            "Inflação do mês fechado",
            (variacao / 100) if variacao is not None else None,
            "Soma ponderada / (Peso comparado x 100) — é a conta que renormaliza a "
            "amostra",
            FORMATO_PERCENTUAL,
        ),
        (
            "Índice do mês (base 100)",
            memoria.get("indice"),
            "100 x (1 + inflação do mês). Não aparece no relatório: serve só para "
            "encadear a série histórica",
            "0.00",
        ),
    ]
    linha = 6
    for item, valor, explicacao, formato in valores:
        planilha.cell(row=linha, column=1, value=item).font = Font(bold=True)
        celula = planilha.cell(row=linha, column=2, value=valor)
        celula.number_format = formato
        celula.alignment = Alignment(horizontal="right")
        planilha.cell(row=linha, column=3, value=explicacao).alignment = Alignment(
            wrap_text=True, vertical="top"
        )
        linha += 1

    if memoria.get("amostra_pequena"):
        planilha.cell(
            row=linha + 1,
            column=1,
            value=(
                "⚠️ Amostra pequena: menos de 60% do gasto da cesta tinha preço no mês "
                "seguinte. O número pode variar bastante de um mês para o outro."
            ),
        ).font = Font(bold=True, color=VERMELHO)

    planilha.cell(
        row=linha + 3,
        column=1,
        value=(
            f"As linhas produto a produto estão na aba '{linhas_aba}'. As células de "
            "preço, peso, variação e contribuição são fórmulas do Excel: clique em "
            "qualquer uma para ver a conta."
        ),
    ).alignment = Alignment(wrap_text=True)

    planilha.column_dimensions["A"].width = 42
    planilha.column_dimensions["B"].width = 18
    planilha.column_dimensions["C"].width = 90


def _aba_serie(planilha, memoria) -> None:
    """Série histórica dos meses fechados — só mês e variação, como no e-mail."""
    planilha["A1"] = "Série histórica da inflação das suas compras"
    planilha["A1"].font = Font(bold=True, size=14, color=AZUL)
    planilha["A2"] = (
        "Cada mês compara os preços do mês com a cesta do mês anterior. É a mesma "
        "série do relatório por e-mail (o índice base 100 fica só na conta interna, "
        "na aba de comparação)."
    )
    planilha["A2"].font = Font(color="6B7280")
    planilha["A2"].alignment = Alignment(wrap_text=True)
    planilha.merge_cells("A2:C2")
    planilha.row_dimensions[2].height = 30

    _escrever_cabecalho(planilha, 4, ["Mês", "Variação do preço", "Observação"])

    linha = 5
    for registro in memoria.get("serie") or []:
        planilha.cell(row=linha, column=1, value=registro.get("rotulo"))
        variacao = registro.get("variacao_pct")
        celula = planilha.cell(
            row=linha,
            column=2,
            value=(variacao / 100) if variacao is not None else None,
        )
        celula.number_format = FORMATO_PERCENTUAL
        celula.alignment = Alignment(horizontal="center")
        if variacao is not None and variacao < 0:
            celula.font = Font(color=VERMELHO, bold=True)
        if registro.get("variacao_pct") is None:
            observacao = (
                "Primeiro mês da série (base)"
                if registro.get("indice") is not None
                else "Sem cesta para comparar neste mês"
            )
        elif registro.get("amostra_pequena"):
            observacao = "Amostra pequena: pouca parte do gasto tinha preço comparável"
        else:
            observacao = ""
        planilha.cell(row=linha, column=3, value=observacao)
        for coluna in range(1, 4):
            planilha.cell(row=linha, column=coluna).border = BORDA
        linha += 1

    planilha.column_dimensions["A"].width = 14
    planilha.column_dimensions["B"].width = 20
    planilha.column_dimensions["C"].width = 62
    planilha.freeze_panes = "A5"


def _aba_metodologia(planilha, memoria) -> None:
    """Cada passo da conta escrito em português, com os números do mês."""
    anterior = memoria["mes_anterior"]
    atual = memoria["mes_atual"]
    variacao = memoria.get("variacao_pct")
    peso = memoria.get("peso_comparado_pct")
    passos = [
        (
            "1. Qual mês está sendo medido",
            f"O mês fechado é {atual['rotulo']} ({_formatar_data(atual['inicio'])} a "
            f"{_formatar_data(atual['fim'])}). Ele é comparado com {anterior['rotulo']} "
            f"({_formatar_data(anterior['inicio'])} a {_formatar_data(anterior['fim'])}), "
            "que entra com a cesta (quanto se comprou) e com o preço de referência.",
        ),
        (
            "2. O que entra na conta",
            "Somente compras de mercadoria para revenda com preço e quantidade válidos. "
            "Bonificação, brinde, CFOP 5910/6910 e produtos com descrição pendente na "
            "Auditoria ficam fora — eles distorceriam o preço médio.",
        ),
        (
            "3. Preço usado",
            "Preço por unidade-base = valor do item / quantidade convertida "
            "(CX24 vira 24 unidades). Quando o mesmo produto é comprado mais de uma vez "
            "no mês, vale a média ponderada: gasto total / quantidade total.",
        ),
        (
            "4. Peso de cada produto",
            f"Peso = gasto do produto em {anterior['rotulo']} / gasto total da cesta "
            f"({_formatar_data(anterior['inicio'])} a {_formatar_data(anterior['fim'])}). "
            "O peso é sempre do mês anterior — é a cesta que o cliente já comprava.",
        ),
        (
            "5. Produtos comparados",
            "Só entram na conta os produtos da cesta anterior que foram comprados de "
            "novo no mês seguinte. Produto que não voltou fica fora (coluna 'Entra na "
            "conta?') e não é tratado como 0% nem como aumento.",
        ),
        (
            "6. Variação e contribuição",
            "Variação = preço atual / preço anterior - 1. Contribuição = peso x "
            "variação, em pontos percentuais. A soma das contribuições é a soma "
            "ponderada.",
        ),
        (
            "7. Inflação do mês",
            f"Inflação = soma ponderada / peso comparado. Neste mês: "
            f"{memoria.get('soma_ponderada_pp') or 0:.2f} p.p. / {peso or 0:.2f}% = "
            f"{f'{variacao:+.2f}%' if variacao is not None else 'sem número (nenhum produto comparável)'}. "
            "Dividir pelo peso comparado é o que renormaliza a amostra e permite "
            "comparar meses com coberturas diferentes.",
        ),
        (
            "8. O que NÃO é feito",
            "O índice não mistura lojas (a mesma compra em lojas diferentes não vira "
            "comparação entre lojas), não usa preço de tabela, não estima preço faltante "
            "e não inventa número quando não há produto comparável: nesse caso o mês "
            "aparece 'sem dado' na série histórica.",
        ),
    ]
    planilha["A1"] = "Como o número é calculado"
    planilha["A1"].font = Font(bold=True, size=14, color=AZUL)
    planilha["A2"] = (
        "Mesma regra usada no relatório por e-mail — esta aba existe para o cliente "
        "conferir sem depender do sistema."
    )
    planilha["A2"].font = Font(color="6B7280")
    planilha["A2"].alignment = Alignment(wrap_text=True)
    planilha.merge_cells("A2:B2")

    linha = 4
    for titulo, texto in passos:
        plano = planilha.cell(row=linha, column=1, value=titulo)
        plano.font = Font(bold=True, color=AZUL)
        plano.alignment = Alignment(vertical="top")
        corpo = planilha.cell(row=linha, column=2, value=texto)
        corpo.alignment = Alignment(wrap_text=True, vertical="top")
        planilha.row_dimensions[linha].height = max(
            30, 15 * (len(texto) // 95 + 1)
        )
        linha += 1

    planilha.column_dimensions["A"].width = 30
    planilha.column_dimensions["B"].width = 105


def montar_planilha(memoria) -> tuple:
    """Monta o .xlsx da memória de cálculo e devolve ``(bytes, nome_arquivo)``.

    Recebe o dicionário de `inflacao.memoria_calculo` (``None`` quando não há gasto
    no mês anterior). O arquivo sai em memória (`BytesIO`), com as abas Resumo,
    Comparação, Série histórica e Metodologia.
    """
    if not memoria:
        raise ValueError(
            "Sem gasto no mês anterior: não há cesta para abrir a memória de cálculo."
        )
    anterior = memoria["mes_anterior"]
    atual = memoria["mes_atual"]
    nome_aba = _titulo_aba(f"{anterior['rotulo']} x {atual['rotulo']}")

    pasta = Workbook()
    resumo = pasta.active
    resumo.title = "Resumo"
    comparacao = pasta.create_sheet(nome_aba)
    _aba_comparacao(comparacao, memoria)
    _aba_serie(pasta.create_sheet("Série histórica"), memoria)
    _aba_metodologia(pasta.create_sheet("Metodologia"), memoria)
    _aba_resumo(resumo, memoria, nome_aba)
    resumo.sheet_view.showGridLines = False

    buffer = BytesIO()
    pasta.save(buffer)
    nome_arquivo = (
        f"memoria_calculo_inflacao_{anterior['ano']}-{anterior['mes']:02d}_"
        f"{atual['ano']}-{atual['mes']:02d}.xlsx"
    )
    return buffer.getvalue(), nome_arquivo



