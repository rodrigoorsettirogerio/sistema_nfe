"""Testes do índice de inflação das compras (backend/inflacao.py).

Valida a metodologia fechada com o cliente: cesta do período anterior como peso,
Laspeyres renormalizado pela cobertura, série mensal encadeada e prévia de 30 dias.
Sem banco, rede ou API.

Uso (na raiz do projeto):
    python tests/test_inflacao.py
"""

import sys
from datetime import date
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from backend import inflacao


def _item(chave, data, preco, quantidade, descricao=None, cnpj="1"):
    """Item no formato de `main._carregar_itens_relatorio` (preço na unidade-base)."""
    return {
        "chave": chave,
        "cnpj": cnpj,
        "descricao": descricao or chave,
        "data": data,
        "valor": preco,
        "quantidade": quantidade,
        "valor_unitario_base": preco,
        "quantidade_base": quantidade,
    }


def test_cesta_usa_preco_da_unidade_base():
    """CX24: R$ 120 a caixa = R$ 5,00 a unidade; o peso é o gasto (R$ 120)."""
    item = _item("CAIXA", date(2026, 10, 1), 5.0, 24)
    item["valor"] = 120.0
    item["quantidade"] = 1.0
    cesta = inflacao.cesta([item])
    registro = cesta["CAIXA"]
    assert abs(registro["preco"] - 5.0) < 1e-9, registro
    assert abs(registro["peso"] - 120.0) < 1e-9, registro


def test_indice_laspeyres_ponderado_pelo_gasto_anterior():
    """Produto com 3x o gasto pesa 3x mais na variação."""
    anterior = [
        _item("A", date(2026, 9, 1), 10.0, 10),  # peso 100
        _item("B", date(2026, 9, 1), 10.0, 30),  # peso 300
    ]
    atual = [
        _item("A", date(2026, 10, 1), 11.0, 10),  # +10%
        _item("B", date(2026, 10, 1), 10.0, 30),  # 0%
    ]
    resultado = inflacao.indice_entre_periodos(anterior, atual)
    assert resultado is not None, resultado
    assert abs(resultado["variacao_pct"] - 2.5) < 0.01, resultado
    assert resultado["cobertura_pct"] == 100.0, resultado
    assert resultado["itens_comparados"] == 2, resultado
    assert resultado["amostra_pequena"] is False, resultado
    # O produto A é o maior responsável (peso 25% x +10%).
    assert resultado["contribuicoes"][0]["produto"] == "A", resultado["contribuicoes"]


def test_produto_novo_nao_entra_no_indice():
    """Produto que não existia na cesta anterior não altera o índice."""
    anterior = [_item("A", date(2026, 9, 1), 10.0, 10)]
    atual = [
        _item("A", date(2026, 10, 1), 11.0, 10),
        _item("NOVO", date(2026, 10, 1), 50.0, 10),  # não estava na cesta
    ]
    resultado = inflacao.indice_entre_periodos(anterior, atual)
    assert abs(resultado["variacao_pct"] - 10.0) < 0.01, resultado
    assert resultado["itens_cesta"] == 1, resultado
    assert resultado["itens_comparados"] == 1, resultado


def test_produto_que_sumiu_reduz_cobertura_sem_diluir():
    """Produto sem compra no período atual fica fora, mas reduz a cobertura."""
    anterior = [
        _item("A", date(2026, 9, 1), 10.0, 10),  # peso 100
        _item("SUMIU", date(2026, 9, 1), 10.0, 90),  # peso 900
    ]
    atual = [_item("A", date(2026, 10, 1), 11.0, 10)]
    resultado = inflacao.indice_entre_periodos(anterior, atual)
    assert abs(resultado["variacao_pct"] - 10.0) < 0.01, resultado
    assert abs(resultado["cobertura_pct"] - 10.0) < 0.01, resultado
    assert resultado["amostra_pequena"] is True, resultado


def test_sem_produto_comparavel_nao_inventa_indice():
    """Cesta anterior só com produtos que não voltaram: índice indisponível."""
    anterior = [_item("A", date(2026, 9, 1), 10.0, 10)]
    atual = [_item("B", date(2026, 10, 1), 10.0, 10)]
    assert inflacao.indice_entre_periodos(anterior, atual) is None
    assert inflacao.indice_entre_periodos([], atual) is None


def test_serie_mensal_encadeada():
    """10,00 -> 11,00 -> 12,10: base 100, 110, 121 (juros compostos dos elos)."""
    itens = [
        _item("A", date(2026, 8, 10), 10.0, 10),
        _item("A", date(2026, 9, 10), 11.0, 10),
        _item("A", date(2026, 10, 10), 12.1, 10),
    ]
    serie = inflacao.serie_mensal(itens, date(2026, 10, 31), meses=2)
    assert [registro["indice"] for registro in serie] == [100.0, 110.0, 121.0], serie
    assert [registro["rotulo"] for registro in serie] == ["ago/26", "set/26", "out/26"], serie
    assert serie[0]["variacao_pct"] is None, serie[0]
    assert abs(serie[1]["variacao_pct"] - 10.0) < 0.01, serie[1]
    assert abs(serie[2]["variacao_pct"] - 10.0) < 0.01, serie[2]


def test_previa_30_dias_compara_janelas_moveis():
    """Últimas 4 semanas (10 % mais caras) contra as 4 anteriores."""
    itens = [
        _item("A", date(2026, 9, 20), 10.0, 10),  # janela anterior
        _item("A", date(2026, 10, 20), 11.0, 10),  # janela atual
    ]
    resultado = inflacao.previa_30_dias(itens, date(2026, 11, 10))
    assert resultado["disponivel"] is True, resultado
    assert abs(resultado["variacao_pct"] - 10.0) < 0.01, resultado
    assert resultado["inicio"] == "2026-10-12", resultado
    assert resultado["fim"] == "2026-11-10", resultado


def test_previa_sem_historico_explica_o_motivo():
    """Conta nova: a prévia avisa que ainda não há duas janelas comparáveis."""
    resultado = inflacao.previa_30_dias([], date(2026, 11, 10))
    assert resultado["disponivel"] is False, resultado
    assert resultado.get("motivo"), resultado


def test_intervalo_e_rotulo_de_mes():
    """Virada de ano e rótulo curto dos meses."""
    assert inflacao.intervalo_mes(2026, 2) == (
        date(2026, 2, 1),
        date(2026, 2, 28),
    )
    assert inflacao.intervalo_mes(2026, 12) == (
        date(2026, 12, 1),
        date(2026, 12, 31),
    )
    assert inflacao.mes_anterior(2026, 1) == (2025, 12)
    assert inflacao.rotulo_mes(2026, 3) == "mar/26"


def main() -> int:
    testes = (
        test_cesta_usa_preco_da_unidade_base,
        test_indice_laspeyres_ponderado_pelo_gasto_anterior,
        test_produto_novo_nao_entra_no_indice,
        test_produto_que_sumiu_reduz_cobertura_sem_diluir,
        test_sem_produto_comparavel_nao_inventa_indice,
        test_serie_mensal_encadeada,
        test_previa_30_dias_compara_janelas_moveis,
        test_previa_sem_historico_explica_o_motivo,
        test_intervalo_e_rotulo_de_mes,
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
