"""Testes da Tabela Mestra e da cascata de extração de marcas.

Cobrem:
- o catálogo `popular_marcas_master.py` (204 nomes únicos, idempotente);
- a carga de `marcas_novas.txt` por `importar_marcas_novas.py` (idempotente e sem duplicar acentos/caixa);
- NÍVEL 1 da cascata (cache exato em `marcas_ia`);
- NÍVEL 2 (tokenização contra `marcas_master` e a lista curta `MARCAS_CONHECIDAS`);
- a memória `marcas_ia` é CACHE, não catálogo de aliases (palpite antigo não vira regra);
- a resposta da IA só é memorizada quando é verificável (catálogo mestre ou descrição);
- correção de cache antigo "SEM MARCA" quando o catálogo conhece a marca;
- NÍVEL 3 (DeepSeek) quando nenhuma fonte local encontra a marca;
- alias de marca composta não usa palavra genérica ("VERDE CAMPO" ≠ VERDE).

Roda sem backend e sem a base real:
    python tests/test_marcas_master.py
"""

import json
import sys
import tempfile
import traceback
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from backend import ia_consenso
from backend.models import Base, MarcaIA, MarcaMaster
from importar_marcas_novas import importar_marcas_novas
from popular_marcas_master import (
    MARCAS_POR_CATEGORIA,
    _lista_consolidada,
    sincronizar_marcas_master,
)


def _banco_temporario():
    pasta = Path(tempfile.mkdtemp(prefix="marcas_master_"))
    engine = create_engine("sqlite:///" + str(pasta / "marcas.db").replace("\\", "/"))
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)(), engine


def test_catalogo_master_idempotente():
    assert len(_lista_consolidada()) == 204
    assert {categoria: len(nomes) for categoria, nomes in MARCAS_POR_CATEGORIA.items()} == {
        "Alimentos": 77,
        "Bebidas": 60,
        "Embalagens": 24,
        "Limpeza": 43,
    }
    db, engine = _banco_temporario()
    try:
        primeiro = sincronizar_marcas_master(db=db, criar_tabelas=False)
        assert primeiro["inseridas"] == 204, primeiro
        assert primeiro["total_banco"] == 204, primeiro
        segundo = sincronizar_marcas_master(db=db, criar_tabelas=False)
        assert segundo["inseridas"] == 0, segundo
        assert segundo["total_banco"] == 204, segundo
        assert (
            db.query(MarcaMaster)
            .filter(MarcaMaster.nome_marca == "Sadia")
            .one()
            .categoria
            == "Alimentos"
        )
    finally:
        db.close()
        engine.dispose()


def test_importar_marcas_novas_idempotente():
    """`importar_marcas_novas.py` não duplica marcas (acento/caixa/espaços ignorados)."""
    db, engine = _banco_temporario()
    pasta = Path(tempfile.mkdtemp(prefix="marcas_novas_"))
    arquivo = pasta / "marcas.txt"
    arquivo.write_text(
        "Elegê\nVigor\n  elegê  \nMococa\n\n# comentário\n", encoding="utf-8"
    )
    try:
        primeiro = importar_marcas_novas(arquivo, db=db, criar_tabelas=False)
        assert primeiro["marcas_unicas_arquivo"] == 3, primeiro
        assert primeiro["repetidas_no_arquivo"] == 1, primeiro
        assert primeiro["inseridas"] == 3, primeiro
        assert primeiro["total_banco"] == 3, primeiro
        segundo = importar_marcas_novas(arquivo, db=db, criar_tabelas=False)
        assert segundo["inseridas"] == 0, segundo
        assert segundo["ja_existentes"] == 3, segundo
        assert db.query(MarcaMaster).count() == 3
        assert (
            db.query(MarcaMaster).filter(MarcaMaster.nome_marca == "Elegê").one().categoria
            == "Catálogo expandido"
        )
    finally:
        db.close()
        engine.dispose()


def test_cache_exato_responde_sem_ia():
    db, engine = _banco_temporario()
    try:
        db.add(
            MarcaIA(
                descricao_original="REFRIGERANTE COCA COLA 2L",
                marca_extraida="COCA-COLA",
            )
        )
        db.commit()
        with patch.object(
            ia_consenso,
            "_chamar_deepseek",
            side_effect=AssertionError("cache exato não pode chamar a IA"),
        ):
            assert (
                ia_consenso.obter_marca_inteligente(
                    "REFRIGERANTE COCA COLA 2L", db=db
                )
                == "COCA-COLA"
            )
    finally:
        db.close()
        engine.dispose()


def test_tokenizacao_usa_catalogo_master_e_memoriza_descricao():
    db, engine = _banco_temporario()
    try:
        db.add_all(
            [
                MarcaMaster(nome_marca="Sadia", categoria="Alimentos"),
                MarcaMaster(nome_marca="Coca-Cola", categoria="Bebidas"),
            ]
        )
        db.commit()
        with patch.object(
            ia_consenso,
            "_chamar_deepseek",
            side_effect=AssertionError("catálogo mestre não pode chamar a IA"),
        ):
            assert (
                ia_consenso.obter_marca_inteligente(
                    "FILE DE PEITO DE FRANGO SADIA CONG 1KG", db=db
                )
                == "SADIA"
            )
            assert (
                ia_consenso.obter_marca_inteligente(
                    "REFRIGERANTE COCA COLA LATA 350ML", db=db
                )
                == "COCA-COLA"
            )
        chave = "FILE DE PEITO DE FRANGO SADIA CONG 1KG"
        assert (
            db.query(MarcaIA)
            .filter(MarcaIA.descricao_original == chave)
            .one()
            .marca_extraida
            == "SADIA"
        )
    finally:
        db.close()
        engine.dispose()


def test_memoria_nao_e_catalogo_de_aliases():
    """`marcas_ia` é CACHE: um palpite antigo não pode virar regra fixa.

    O registro "OUTRO PRODUTO QUALQUER → ARRICO" não pode fazer qualquer "POLENTA
    PALITO CONG CX" virar ARRICO: sem catálogo mestre e sem a marca no texto, a
    cascata local não resolve e a IA é chamada (aqui devolve ``SEM MARCA``).
    """
    db, engine = _banco_temporario()
    try:
        db.add(
            MarcaIA(
                descricao_original="OUTRO PRODUTO QUALQUER",
                marca_extraida="ARRICO",
            )
        )
        db.commit()
        with patch.object(
            ia_consenso,
            "_chamar_deepseek",
            return_value=json.dumps({"sem_marca": True}),
        ) as chamada:
            assert (
                ia_consenso.obter_marca_inteligente(
                    "POLENTA PALITO CONG CX", db=db
                )
                == ia_consenso.MARCA_SEM_MARCA
            )
        assert chamada.call_count == 1, chamada.call_count
    finally:
        db.close()
        engine.dispose()


def test_alias_de_marca_composta_exige_palavra_distintiva():
    """Marca de 2+ palavras só gera alias de 1 palavra quando ele é distintivo."""
    composta = ia_consenso._aliases_da_marca("VERDE CAMPO")
    assert "VERDE CAMPO" in composta, composta
    assert "VERDE" not in composta and "CAMPO" not in composta, composta

    gin = ia_consenso._aliases_da_marca("GIN GORDONS")
    assert "GORDONS" in gin, gin
    assert "GIN" not in gin, gin

    # Marca de UMA palavra continua valendo isolada (é o nome inteiro).
    assert "SADIA" in ia_consenso._aliases_da_marca("SADIA")


def test_resposta_da_ia_sem_confirmacao_vira_sem_marca():
    """Palpite da IA fora do catálogo e fora do texto é gravado como SEM MARCA."""
    db, engine = _banco_temporario()
    try:
        with patch.object(
            ia_consenso,
            "_chamar_deepseek",
            return_value=json.dumps({"marca": "SUPERGRIFE"}, ensure_ascii=False),
        ):
            assert (
                ia_consenso.obter_marca_inteligente(
                    "PALHA DE MANDIOCA FINA 1KG", db=db
                )
                == ia_consenso.MARCA_SEM_MARCA
            )
        assert db.query(MarcaIA).one().marca_extraida == ia_consenso.MARCA_SEM_MARCA
    finally:
        db.close()
        engine.dispose()


def test_cache_sem_marca_e_corrigido_pelo_catalogo():
    db, engine = _banco_temporario()
    try:
        db.add(MarcaMaster(nome_marca="Hellmann's", categoria="Alimentos"))
        db.add(
            MarcaIA(
                descricao_original="MOLHO DE TOMATE HELLMANN'S",
                marca_extraida="SEM MARCA",
            )
        )
        db.commit()
        with patch.object(
            ia_consenso,
            "_chamar_deepseek",
            side_effect=AssertionError("SEM MARCA antigo deve ser corrigido localmente"),
        ):
            assert (
                ia_consenso.obter_marca_inteligente(
                    "MOLHO DE TOMATE HELLMANN'S", db=db
                )
                == "HELLMANN'S"
            )
        assert (
            db.query(MarcaIA)
            .filter(MarcaIA.descricao_original == "MOLHO DE TOMATE HELLMANN'S")
            .one()
            .marca_extraida
            == "HELLMANN'S"
        )
    finally:
        db.close()
        engine.dispose()


def test_fallback_deepseek_grava_na_memoria():
    db, engine = _banco_temporario()
    try:
        with patch.object(
            ia_consenso,
            "_chamar_deepseek",
            return_value=json.dumps({"marca": "YOKI"}, ensure_ascii=False),
        ) as chamada:
            assert (
                ia_consenso.obter_marca_inteligente(
                    "PALHA DE MANDIOCA FINA YOKI 1KG", db=db
                )
                == "YOKI"
            )
        assert chamada.call_count == 1, chamada.call_count
        assert db.query(MarcaIA).one().marca_extraida == "YOKI"
    finally:
        db.close()
        engine.dispose()


def test_marca_composta_so_de_termos_genericos_e_invalida():
    """Marca COMPOSTA é lixo quando TODAS as palavras são genéricas/de produto.

    O cache antigo gravou "PLAST PET" (embalagem), "VERDE CAMPO" (estado/origem) e
    "PRIME STEAK ORIGEM" (corte) como se fossem fabricante. A barreira só reconhecia
    o termo ISOLADO, então a composta passava e voltava em todas as rodadas.
    """
    for marca in (
        "PLAST PET",
        "VERDE CAMPO",
        "PRIME STEAK ORIGEM",
        "ZERO KAL",
        "IN NATURA",
    ):
        assert ia_consenso._marca_invalida(marca), marca
    # Nome de `MARCAS_CONHECIDAS` nunca é barrado, mesmo com palavra de produto.
    for marca in ("SADIA", "UNIAO", "COOPERBOI", "JOSE CUERVO"):
        assert not ia_consenso._marca_invalida(marca), marca


def test_cooperboi_e_marca_e_j_cuervo_e_abreviacao():
    """COOPERBOI é marca de carne (saiu do vocabulário de corte) e "J CUERVO" expande."""
    from backend import marcas as regras_marcas

    assert "COOPERBOI" not in regras_marcas.QUALIFICADORES
    assert (
        regras_marcas.extrair_marca("CARNE MOIDA COOPERBOI BLEND 1KG", {"CARNE", "MOIDA"})
        == "COOPERBOI"
    )
    assert ia_consenso._completar_marca("J CUERVO") == "JOSE CUERVO"
    assert ia_consenso._marca_na_descricao(
        "JOSE CUERVO", "TEQUILA J CUERVO SILVER 750 ML"
    )
    assert (
        ia_consenso._marca_conhecida_na_descricao("TEQUILA JOSE CUERVO REPOSADO 750ML")
        == "JOSE CUERVO"
    )


TESTS = [
    test_catalogo_master_idempotente,
    test_importar_marcas_novas_idempotente,
    test_cache_exato_responde_sem_ia,
    test_tokenizacao_usa_catalogo_master_e_memoriza_descricao,
    test_memoria_nao_e_catalogo_de_aliases,
    test_alias_de_marca_composta_exige_palavra_distintiva,
    test_cache_sem_marca_e_corrigido_pelo_catalogo,
    test_fallback_deepseek_grava_na_memoria,
    test_resposta_da_ia_sem_confirmacao_vira_sem_marca,
    test_marca_composta_so_de_termos_genericos_e_invalida,
    test_cooperboi_e_marca_e_j_cuervo_e_abreviacao,
]


def main():
    falhas = 0
    for funcao in TESTS:
        try:
            funcao()
        except Exception:
            falhas += 1
            print(f"❌ {funcao.__name__}")
            traceback.print_exc()
        else:
            print(f"✅ {funcao.__name__}")
    print(f"\n{len(TESTS) - falhas}/{len(TESTS)} testes passaram.")
    return 1 if falhas else 0


if __name__ == "__main__":
    raise SystemExit(main())

