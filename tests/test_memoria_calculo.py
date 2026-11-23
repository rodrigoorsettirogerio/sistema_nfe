"""Testes do Excel de memória de cálculo da inflação (backend/memoria_calculo.py).

Garante que o arquivo baixado abre (é um .xlsx válido), que as abas existem, que a
conta produto a produto está em FÓRMULAS do Excel (o cliente confere clique a clique),
que os negativos ficam vermelhos e que a série histórica não mostra Índice/Cobertura.
Sem banco, rede ou API.

Uso (na raiz do projeto):
    python tests/test_memoria_calculo.py
"""

import sys
from datetime import date
from io import BytesIO
from pathlib import Path

from openpyxl import load_workbook

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from backend import inflacao, memoria_calculo


def _item(chave, data, preco, quantidade):
    """Item no formato de `main._carregar_itens_relatorio` (preço na unidade-base)."""
    return {
        "chave": chave,
        "cnpj": "1",
        "descricao": chave,
        "data": data,
        "valor": preco,
        "quantidade": quantidade,
        "valor_unitario_base": preco,
        "quantidade_base": quantidade,
    }


def _memoria_do_exemplo():
    """Set/26 -> out/26: A +20% (peso 2/3) e B -20% (peso 1/3) => +6,67%."""
    itens = [
        _item("PRODUTO A", date(2026, 9, 10), 10.0, 100),
        _item("PRODUTO B", date(2026, 9, 12), 5.0, 100),
        _item("PRODUTO A", date(2026, 10, 10), 12.0, 100),
        _item("PRODUTO B", date(2026, 10, 12), 4.0, 100),
    ]
    return inflacao.memoria_calculo(itens, date(2026, 10, 31))


def test_planilha_abre_com_as_abas_da_conferencia():
    conteudo, nome = memoria_calculo.montar_planilha(_memoria_do_exemplo())
    assert nome == "memoria_calculo_inflacao_2026-09_2026-10.xlsx", nome
    pasta = load_workbook(BytesIO(conteudo))
    assert pasta.sheetnames == [
        "Resumo",
        "set-26 x out-26",
        "Série histórica",
        "Metodologia",
    ], pasta.sheetnames


def test_comparacao_tem_formulas_e_os_numeros_do_mes():
    memoria = _memoria_do_exemplo()
    conteudo, _ = memoria_calculo.montar_planilha(memoria)
    planilha = load_workbook(BytesIO(conteudo))["set-26 x out-26"]
    assert planilha["A1"].value.startswith("Comparação set/26")
    assert planilha["A4"].value == "Produto"
    # Duas linhas de produto (A maior contribuição primeiro, ordem do relatório).
    assert planilha["A5"].value == "PRODUTO A"
    assert planilha["A6"].value == "PRODUTO B"
    # Quantidade e gasto vêm "crus" da base; preço, peso, variação e contribuição
    # são fórmulas — é isso que deixa o cliente conferir a conta.
    assert planilha["C5"].value == 100
    assert planilha["D5"].value == 1000
    assert planilha["E5"].value == '=IF(C5>0,D5/C5,"")'
    assert planilha["F5"].value == '=IF($D$7=0,"",D5/$D$7)'
    assert planilha["G5"].value == 100
    assert planilha["H5"].value == 1200
    assert planilha["I5"].value == '=IF(G5>0,H5/G5,"")'
    assert planilha["J5"].value == '=IF(AND(E5>0,I5>0),I5/E5-1,"")'
    assert planilha["K5"].value == '=IF(J5="","",F5*J5*100)'
    assert planilha["L5"].value == "Sim"
    # Total (somas) e os passos da conta logo abaixo.
    assert planilha["D7"].value == "=SUM(D5:D6)"
    assert planilha["A9"].value == "Peso comparado (produtos com 'Sim')"
    assert planilha["B9"].value == '=SUMIF($L$5:$L$6,"Sim",$F$5:$F$6)'
    assert planilha["B10"].value == "=SUM($K$5:$K$6)"
    assert planilha["B11"].value == '=IFERROR(B10/(B9*100),"")'


def test_produto_sem_preco_no_mes_seguinte_fica_fora_da_conta():
    memoria = _memoria_do_exemplo()
    memoria["linhas"][1]["comparado"] = False
    memoria["linhas"][1]["variacao_pct"] = None
    memoria["linhas"][1]["contribuicao_pct"] = None
    conteudo, _ = memoria_calculo.montar_planilha(memoria)
    planilha = load_workbook(BytesIO(conteudo))["set-26 x out-26"]
    assert planilha["L6"].value.startswith("Não")
    # A variação continua sendo fórmula: ela devolve "" sozinha quando falta preço.
    assert planilha["J6"].value.startswith("=IF(AND(")


def test_negativos_em_vermelho_e_serie_sem_indice():
    conteudo, _ = memoria_calculo.montar_planilha(_memoria_do_exemplo())
    pasta = load_workbook(BytesIO(conteudo))
    comparacao = pasta["set-26 x out-26"]
    regras = [
        regra
        for faixa in comparacao.conditional_formatting
        for regra in faixa.rules
        if str(faixa.sqref).startswith("J5")
    ]
    assert regras and regras[0].operator == "lessThan", regras
    fonte = getattr(regras[0], "font", None) or regras[0].dxf.font
    assert str(fonte.color.rgb).endswith("B42318"), fonte.color.rgb

    serie = pasta["Série histórica"]
    assert serie["A4"].value == "Mês"
    assert serie["B4"].value == "Variação do preço"
    cabecalho = [serie.cell(row=4, column=coluna).value for coluna in range(1, 4)]
    assert "Índice" not in cabecalho and "Cobertura" not in cabecalho, cabecalho
    rotulos = [serie.cell(row=linha, column=1).value for linha in range(5, 20)]
    assert "out/26" in rotulos, rotulos


def test_resumo_mostra_a_conta_e_a_metodologia():
    conteudo, _ = memoria_calculo.montar_planilha(_memoria_do_exemplo())
    pasta = load_workbook(BytesIO(conteudo))
    resumo = pasta["Resumo"]
    itens = [resumo.cell(row=linha, column=1).value for linha in range(6, 14)]
    assert "Inflação do mês fechado" in itens, itens
    linha_inflacao = itens.index("Inflação do mês fechado") + 6
    # +6,67% do exemplo, como fração (o formato da célula é percentual).
    assert abs(resumo.cell(row=linha_inflacao, column=2).value - 0.0667) < 0.0001

    metodologia = pasta["Metodologia"]
    titulos = [metodologia.cell(row=linha, column=1).value for linha in range(4, 12)]
    assert any("Inflação do mês" in str(titulo) for titulo in titulos), titulos


def test_sem_cesta_no_mes_anterior_explica_em_vez_de_gerar_arquivo():
    try:
        memoria_calculo.montar_planilha(None)
    except ValueError as erro:
        assert "cesta" in str(erro)
    else:
        raise AssertionError("deveria recusar memória vazia")


def main() -> int:
    testes = (
        test_planilha_abre_com_as_abas_da_conferencia,
        test_comparacao_tem_formulas_e_os_numeros_do_mes,
        test_produto_sem_preco_no_mes_seguinte_fica_fora_da_conta,
        test_negativos_em_vermelho_e_serie_sem_indice,
        test_resumo_mostra_a_conta_e_a_metodologia,
        test_sem_cesta_no_mes_anterior_explica_em_vez_de_gerar_arquivo,
    )
    falhas = 0
    for teste in testes:
        try:
            teste()
        except Exception as erro:  # noqa: BLE001 - relatório do script
            falhas += 1
            print(f"FALHOU {teste.__name__}: {erro}")
        else:
            print(f"OK  {teste.__name__}")
    print(f"\n{len(testes) - falhas} testes passaram.")
    return 1 if falhas else 0


if __name__ == "__main__":
    raise SystemExit(main())

