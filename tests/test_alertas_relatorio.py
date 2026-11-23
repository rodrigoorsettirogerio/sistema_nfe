"""Testes do relatório de alertas por e-mail e do agendador.

Cobre a regra central (toda variação contra a última compra, impacto em R$/mês,
itens novos/recomprados, notas atrasadas), o HTML/texto do e-mail e a decisão de
"quando enviar" do agendador — sem banco, rede ou SMTP.

Uso (na raiz do projeto):
    python tests/test_alertas_relatorio.py
"""

import os
import sys
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from backend import agendador
from backend import main as backend_main
from backend import relatorios_alerta


def _item(
    chave,
    data,
    preco,
    quantidade,
    nota_id,
    descricao=None,
    cnpj="1",
    loja="Loja A",
    fornecedor="Fornecedor X",
):
    """Item no formato de `main._carregar_itens_relatorio` (preço na unidade-base)."""
    return {
        "id": nota_id * 10,
        "chave": chave,
        "cnpj": cnpj,
        "descricao": descricao or chave,
        "data": data,
        "loja": loja,
        "fornecedor": fornecedor,
        "valor": preco,
        "quantidade": quantidade,
        "valor_unitario_base": preco,
        "quantidade_base": quantidade,
        "nota_id": nota_id,
    }


def _contexto(tipo="diario", notas_lote=(2,), atrasadas=(), inicio=None, fim=None):
    return {
        "tipo": tipo,
        "notas_lote": set(notas_lote),
        "atrasadas": set(atrasadas),
        "inicio": inicio or date(2026, 11, 1),
        "fim": fim or date(2026, 11, 1),
        "referencia": fim or date(2026, 11, 1),
        "lojas": {"1": "Loja A"},
    }


def test_variacao_contra_ultima_compra_com_impacto_mensal():
    """10 -> 11 na compra do lote: entra como aumento e o impacto usa 90 dias."""
    itens = [
        _item("ARROZ", date(2026, 10, 1), 10.0, 10, nota_id=1),
        _item("ARROZ", date(2026, 11, 1), 11.0, 10, nota_id=2),
    ]
    relatorio = relatorios_alerta.montar_relatorio(itens, _contexto())
    assert relatorio["resumo"]["aumentos"] == 1, relatorio["resumo"]
    assert relatorio["resumo"]["quedas"] == 0, relatorio["resumo"]
    linha = relatorio["variacoes"][0]
    assert linha["produto"] == "ARROZ", linha
    assert abs(linha["variacao_pct"] - 10.0) < 0.01, linha
    # Consumo de 90 dias = 20 unidades -> 6,67/mês; 1,00 de diferença = R$ 6,67.
    assert abs(linha["impacto_mensal"] - 6.67) < 0.01, linha
    assert linha["tipo"] == "aumento", linha


def test_preco_igual_nao_gera_linha():
    """Sem diferença de preço não há alerta (nada mudou para o cliente)."""
    itens = [
        _item("ARROZ", date(2026, 10, 1), 10.0, 10, nota_id=1),
        _item("ARROZ", date(2026, 11, 1), 10.0, 10, nota_id=2),
    ]
    relatorio = relatorios_alerta.montar_relatorio(itens, _contexto())
    assert relatorio["variacoes"] == [], relatorio["variacoes"]
    assert relatorio["resumo"]["variacoes"] == 0, relatorio["resumo"]


def test_produto_novo_e_recomprado():
    """Primeira compra vira 'novo'; volta depois de 40 dias vira 'recomprado'."""
    itens = [
        _item("NOVO", date(2026, 11, 1), 8.0, 5, nota_id=2, descricao="NOVO PRODUTO"),
        _item("ANTIGO", date(2026, 9, 20), 10.0, 10, nota_id=1),
        _item("ANTIGO", date(2026, 11, 1), 12.0, 10, nota_id=2),
    ]
    relatorio = relatorios_alerta.montar_relatorio(itens, _contexto())
    assert [linha["produto"] for linha in relatorio["novos"]] == ["NOVO PRODUTO"]
    assert relatorio["resumo"]["recomprados"] == 1, relatorio["resumo"]
    linha = relatorio["variacoes"][0]
    assert linha["recomprado"] is True, linha
    assert linha["dias_sem_compra"] == 42, linha


def test_variacao_brusca_recebe_selo_e_nao_e_escondida():
    """+60% continua na lista como aumento, só com o selo 'conferir'."""
    itens = [
        _item("OLEO", date(2026, 10, 1), 5.0, 10, nota_id=1),
        _item("OLEO", date(2026, 11, 1), 8.0, 10, nota_id=2),
    ]
    relatorio = relatorios_alerta.montar_relatorio(itens, _contexto())
    linha = relatorio["variacoes"][0]
    assert linha["tipo"] == "aumento", linha
    assert linha["conferir"] is True, linha
    assert relatorio["resumo"]["conferir"] == 1, relatorio["resumo"]


def test_nota_atrasada_marcada_no_relatorio():
    """Nota do lote importada depois da emissão aparece com o aviso de atraso."""
    itens = [
        _item("ARROZ", date(2026, 8, 1), 9.0, 10, nota_id=1),
        _item("ARROZ", date(2026, 9, 1), 10.0, 10, nota_id=2),  # emitida antes
    ]
    relatorio = relatorios_alerta.montar_relatorio(
        itens, _contexto(atrasadas=(2,))
    )
    assert relatorio["variacoes"][0]["atrasada"] is True, relatorio["variacoes"]
    assert relatorio["resumo"]["atrasadas"] == 1, relatorio["resumo"]


def test_lojas_diferentes_nunca_se_comparam():
    """O mesmo produto em outra loja não serve como 'compra anterior'."""
    itens = [
        _item("ARROZ", date(2026, 10, 1), 10.0, 10, nota_id=1, cnpj="2", loja="Loja B"),
        _item("ARROZ", date(2026, 11, 1), 11.0, 10, nota_id=2, cnpj="1", loja="Loja A"),
    ]
    relatorio = relatorios_alerta.montar_relatorio(itens, _contexto())
    assert relatorio["variacoes"] == [], relatorio["variacoes"]
    assert len(relatorio["novos"]) == 1, relatorio["novos"]


def test_html_e_texto_do_email():
    """HTML tem tabela/valores e escapa o texto; o texto puro tem o resumo."""
    itens = [
        _item("ARROZ <script>", date(2026, 10, 1), 10.0, 10, nota_id=1),
        _item("ARROZ <script>", date(2026, 11, 1), 11.0, 10, nota_id=2),
    ]
    relatorio = relatorios_alerta.montar_relatorio(itens, _contexto())
    html = relatorios_alerta.montar_html(relatorio)
    texto = relatorios_alerta.montar_texto(relatorio)
    assert "Relatório Diário de Compras" in html, html[:500]
    assert "R$ 11,00" in html, html[:2000]
    assert "<script>" not in html, "o produto precisa ser escapado no HTML"
    assert "&lt;script&gt;" in html, html[:2000]
    assert "Relatório Diário de Compras" in texto, texto
    assert "ARROZ <script>" in texto, texto
    assert "R$ 11,00" in texto, texto


def test_variacao_minima_de_um_centavo_por_unidade():
    """R$ 0,01 de diferença entra; ruído de arredondamento do XML fica de fora."""
    itens = [
        _item("CENTAVO", date(2026, 10, 1), 10.0, 10, nota_id=1),
        _item("CENTAVO", date(2026, 11, 1), 10.01, 10, nota_id=2),
        _item("RUIDO", date(2026, 10, 1), 10.0, 10, nota_id=1),
        _item("RUIDO", date(2026, 11, 1), 10.004, 10, nota_id=2),
    ]
    relatorio = relatorios_alerta.montar_relatorio(itens, _contexto())
    produtos = [linha["produto"] for linha in relatorio["variacoes"]]
    assert produtos == ["CENTAVO"], produtos
    linha = relatorio["variacoes"][0]
    assert abs(linha["variacao_abs"] - 0.01) < 1e-9, linha
    assert relatorios_alerta.VARIACAO_MINIMA_ABS == 0.01


def test_email_colore_negativo_de_vermelho_e_positivo_de_cinza_com_tooltip():
    """Queda em vermelho (#b42318), aumento em cinza (#1f2937) e tooltip das compras."""
    itens = [
        _item("ARROZ", date(2026, 9, 3), 10.0, 10, nota_id=1, fornecedor="FORNECEDOR A"),
        _item("ARROZ", date(2026, 11, 3), 8.0, 10, nota_id=2, fornecedor="FORNECEDOR B"),
        _item("FEIJAO", date(2026, 9, 3), 5.0, 10, nota_id=1, fornecedor="FORNECEDOR A"),
        _item("FEIJAO", date(2026, 11, 3), 6.0, 10, nota_id=2, fornecedor="FORNECEDOR B"),
    ]
    relatorio = relatorios_alerta.montar_relatorio(itens, _contexto())
    tipos = {linha["produto"]: linha["tipo"] for linha in relatorio["variacoes"]}
    assert tipos == {"ARROZ": "queda", "FEIJAO": "aumento"}, tipos
    html = relatorios_alerta.montar_html(relatorio)
    # A cor vai inline (Gmail/Outlook ignoram CSS externo).
    assert f"color:{relatorios_alerta.COR_NEGATIVA};font-weight:600" in html, html
    assert f"color:{relatorios_alerta.COR_POSITIVA};font-weight:600" in html, html
    assert relatorios_alerta.COR_NEGATIVA == "#b42318"
    assert relatorios_alerta.COR_POSITIVA == "#1f2937"
    # Data + fornecedor da compra anterior e da atual no tooltip dos preços.
    dica = (
        'title="Compra anterior: 03/09/2026 · FORNECEDOR A | '
        'Compra atual: 03/11/2026 · FORNECEDOR B"'
    )
    assert dica in html, html[:4000]
    assert "pelo menos R$ 0,01 por unidade" in html, html[:2000]

    texto = relatorios_alerta.montar_texto(relatorio)
    assert "[03/09/2026 · FORNECEDOR A]" in texto, texto
    assert "[03/11/2026 · FORNECEDOR B]" in texto, texto


def test_email_e_texto_usam_serie_historica_sem_indice_nem_cobertura():
    """A série vira "Série histórica" (mês + variação), sem índice/cobertura."""
    itens = [
        _item("ARROZ", date(2026, 9, 10), 10.0, 10, nota_id=1),
        _item("ARROZ", date(2026, 10, 10), 11.0, 10, nota_id=2),
    ]
    contexto = _contexto(
        tipo="mensal",
        notas_lote=(2,),
        inicio=date(2026, 10, 1),
        fim=date(2026, 10, 31),
    )
    relatorio = relatorios_alerta.montar_relatorio(itens, contexto)
    serie = relatorio["inflacao"]["serie"]
    assert any(item.get("variacao_pct") is not None for item in serie), serie
    assert relatorio["inflacao"]["fechado"]["variacao_pct"] is not None, relatorio

    html = relatorios_alerta.montar_html(relatorio)
    texto = relatorios_alerta.montar_texto(relatorio)
    assert "Série histórica" in html, html
    assert "Índice" not in html, html
    assert "Cobertura" not in html, html
    assert "Série histórica" in texto, texto
    assert "Índice" not in texto, texto
    assert "Cobertura" not in texto, texto


def test_agendador_respeita_hora_e_marcadores():
    """Envio só DENTRO da hora configurada, uma vez por dia/semana/mês."""
    config = SimpleNamespace(
        ativo=True,
        email="conta@exemplo.com",
        alerta_diario=True,
        alerta_semanal=True,
        alerta_mensal=True,
        ultimo_envio_diario=None,
        ultimo_envio_semanal=None,
        ultimo_envio_mensal=None,
        ultima_nota_alerta_id=0,
        ultima_nota_semanal_id=0,
        ultima_nota_mensal_id=0,
    )
    with patch.dict(os.environ, {"NFE_ALERTAS_HORA": "6"}):
        # Fora da hora (05:59 e 07:00) nada sai.
        assert agendador.tipos_devidos(config, datetime(2026, 11, 16, 5, 59)) == []
        assert agendador.tipos_devidos(config, datetime(2026, 11, 16, 7, 0)) == []
        # Segunda 06:05: diário + semanal.
        tipos = agendador.tipos_devidos(config, datetime(2026, 11, 16, 6, 5))
        assert tipos == ["diario", "semanal"], tipos
        # Depois do envio confirmado, os marcadores impedem repetir.
        config.ultimo_envio_diario = "2026-11-16"
        config.ultimo_envio_semanal = "2026-11-16"
        assert agendador.tipos_devidos(config, datetime(2026, 11, 16, 6, 30)) == []
        # Terça: só o diário.
        assert agendador.tipos_devidos(config, datetime(2026, 11, 17, 6, 0)) == [
            "diario"
        ]
        # Dia 1º: diário + mensal do mês que fechou (novembro/2026).
        config.ultimo_envio_diario = None
        assert agendador.tipos_devidos(config, datetime(2026, 12, 1, 6, 0)) == [
            "diario",
            "mensal",
        ]
        config.ultimo_envio_mensal = "2026-11"
        assert agendador.tipos_devidos(config, datetime(2026, 12, 1, 6, 0)) == [
            "diario"
        ]
        # Conta com o alerta desligado não recebe nada.
        config.ativo = False
        assert agendador.tipos_devidos(config, datetime(2026, 12, 2, 6, 0)) == []


def test_agendador_nao_dispara_fora_do_tipo_ligado():
    """Relatório desligado na tela não é enviado pelo agendador."""
    config = SimpleNamespace(
        ativo=True,
        email="conta@exemplo.com",
        alerta_diario=False,
        alerta_semanal=False,
        alerta_mensal=False,
        ultimo_envio_diario=None,
        ultimo_envio_semanal=None,
        ultimo_envio_mensal=None,
    )
    with patch.dict(os.environ, {"NFE_ALERTAS_HORA": "6"}):
        assert agendador.tipos_devidos(config, datetime(2026, 12, 1, 6, 0)) == []


def test_periodos_do_relatorio():
    """Diário = hoje; semanal = seg-dom anterior; mensal = mês fechado."""
    diario = backend_main._periodo_do_relatorio("diario", date(2026, 11, 18))
    assert diario["inicio"] == date(2026, 11, 18), diario
    assert diario["fim"] == date(2026, 11, 18), diario

    semanal = backend_main._periodo_do_relatorio("semanal", date(2026, 11, 18))
    assert semanal["inicio"] == date(2026, 11, 9), semanal
    assert semanal["fim"] == date(2026, 11, 15), semanal

    mensal = backend_main._periodo_do_relatorio("mensal", date(2026, 12, 1))
    assert mensal["inicio"] == date(2026, 11, 1), mensal
    assert mensal["fim"] == date(2026, 11, 30), mensal


def test_lote_do_relatorio_por_ordem_de_importacao():
    """Cursor compara `notas_fiscais.id`; atrasada só existe após o 1º envio."""
    notas = [
        SimpleNamespace(id=1, data_emissao=date(2026, 10, 1)),
        SimpleNamespace(id=2, data_emissao=date(2026, 11, 1)),
        SimpleNamespace(id=3, data_emissao=date(2026, 10, 15)),
    ]
    periodo = backend_main._periodo_do_relatorio("diario", date(2026, 11, 2))
    # Cursor 2: só a nota importada depois (id 3, emitida em outubro) entra.
    config = SimpleNamespace(ultima_nota_alerta_id=2)
    lote = backend_main._lote_do_relatorio(notas, config, "diario", periodo)
    assert lote["notas_lote"] == {3}, lote

    # Primeira execução (cursor 0): apenas o último dia com notas (id 2).
    config_zero = SimpleNamespace(ultima_nota_alerta_id=0)
    lote_zero = backend_main._lote_do_relatorio(
        notas, config_zero, "diario", periodo
    )
    assert lote_zero["notas_lote"] == {2}, lote_zero

    # Semanal: janela seg-dom + notas importadas depois do cursor e emitidas antes.
    periodo_semanal = backend_main._periodo_do_relatorio(
        "semanal", date(2026, 11, 2)
    )
    config_semanal = SimpleNamespace(ultima_nota_semanal_id=1)
    lote_semanal = backend_main._lote_do_relatorio(
        notas, config_semanal, "semanal", periodo_semanal
    )
    assert lote_semanal["notas_lote"] == {2, 3}, lote_semanal
    assert lote_semanal["atrasadas"] == {3}, lote_semanal


def test_periodo_usa_media_do_mes_e_tooltip_lista_todas_as_compras():
    """Mensal: Antes/Agora = média do mês x mês anterior; tooltip lista as compras."""
    itens = [
        _item("ARROZ", date(2026, 10, 1), 10.0, 10, nota_id=1, fornecedor="Atacadao"),
        _item("ARROZ", date(2026, 10, 20), 12.0, 10, nota_id=2, fornecedor="Assai"),
    ]
    # 12 compras em novembro (R$ 13 a R$ 24): o tooltip mostra 10 e resume as outras 2.
    for indice, dia in enumerate(range(1, 25, 2), start=11):
        itens.append(
            _item(
                "ARROZ",
                date(2026, 11, dia),
                12.0 + (indice - 10),
                10,
                nota_id=indice,
                fornecedor="Carrefour" if indice % 2 else "Atacadao",
            )
        )
    relatorio = relatorios_alerta.montar_relatorio(
        itens,
        _contexto(
            tipo="mensal",
            notas_lote=range(11, 23),
            inicio=date(2026, 11, 1),
            fim=date(2026, 11, 30),
        ),
    )
    linha = relatorio["variacoes"][0]
    # Média de outubro = (10 + 12) / 2; média de novembro = (13 + ... + 24) / 12.
    assert abs(linha["preco_anterior"] - 11.0) < 1e-9, linha
    assert abs(linha["preco_atual"] - 18.5) < 1e-9, linha
    assert len(linha["compras_anterior"]) == 2, linha
    assert len(linha["compras_atual"]) == 12, linha
    assert linha["dias_sem_compra"] == 12, linha
    html = relatorios_alerta.montar_html(relatorio)
    assert (
        'title="Média de 01/10 a 31/10 - Compras: Atacadao (R$ 10,00), '
        'Assai (R$ 12,00)"'
    ) in html, html
    assert "Média de 01/11 a 30/11 - Compras:" in html, html
    assert "... e mais 2 compras" in html, html
    texto = relatorios_alerta.montar_texto(relatorio)
    assert "média de 01/11 a 30/11 · 12 compra(s)" in texto, texto


def test_periodo_com_nota_atrasada_unica_entra_como_novo():
    """Mensal com uma única nota atrasada: o produto é novo, não comparação consigo."""
    itens = [_item("ATRASADO", date(2026, 10, 25), 9.0, 10, nota_id=5)]
    relatorio = relatorios_alerta.montar_relatorio(
        itens,
        _contexto(
            tipo="mensal",
            notas_lote=(5,),
            atrasadas=(5,),
            inicio=date(2026, 11, 1),
            fim=date(2026, 11, 30),
        ),
    )
    assert [linha["produto"] for linha in relatorio["novos"]] == ["ATRASADO"], relatorio
    assert relatorio["novos"][0]["atrasada"] is True, relatorio["novos"][0]
    assert relatorio["variacoes"] == [], relatorio["variacoes"]


def main() -> int:
    testes = (
        test_variacao_contra_ultima_compra_com_impacto_mensal,
        test_preco_igual_nao_gera_linha,
        test_produto_novo_e_recomprado,
        test_variacao_brusca_recebe_selo_e_nao_e_escondida,
        test_nota_atrasada_marcada_no_relatorio,
        test_lojas_diferentes_nunca_se_comparam,
        test_html_e_texto_do_email,
        test_variacao_minima_de_um_centavo_por_unidade,
        test_email_colore_negativo_de_vermelho_e_positivo_de_cinza_com_tooltip,
        test_email_e_texto_usam_serie_historica_sem_indice_nem_cobertura,
        test_agendador_respeita_hora_e_marcadores,
        test_agendador_nao_dispara_fora_do_tipo_ligado,
        test_periodos_do_relatorio,
        test_lote_do_relatorio_por_ordem_de_importacao,
        test_periodo_usa_media_do_mes_e_tooltip_lista_todas_as_compras,
        test_periodo_com_nota_atrasada_unica_entra_como_novo,
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
