"""Script de teste da inteligência de marcas (3 descrições do mesmo NCM).

Prova três coisas, sem precisar da API no ar e sobre uma CÓPIA do banco:

1. **A extração da marca funciona**: em "PALHA DE MANDIOCA FINA YOKI / MATHEUS / DECK" as
   palavras do produto se repetem entre as descrições do NCM e só a marca varia — é ela
   que o importador grava em `itens_nota.marca` (backend/marcas.py);
2. **A Média de Mercado de cada grupo continua exata**: cada produto usa o preço do seu
   EAN/descrição. As outras marcas do NCM não entram no cálculo — e a marca entra na
   chave Produto + Marca, então Arrico e El Chef têm médias separadas;
3. **As marcas substitutas** (mesmo NCM, mesma descrição sem a marca, > 80% parecida) são
   devolvidas no campo `mercado_marcas` — que o front usa SÓ no tooltip da coluna
   Mercado (Evolução e Comparativo).

Uso (na raiz do projeto):
    python tests/test_marcas.py
"""

import shutil
import sys
import tempfile
import uuid
from datetime import date
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import main as bmain
from backend import marcas as bmarcas
from backend.database import DB_PATH, init_db
from backend.models import Empresa, Fornecedor, ItemNota, NotaFiscal

NCM_TESTE = "19059090"
DESCRICOES = [
    "PALHA DE MANDIOCA FINA YOKI 1KG",
    "PALHA DE MANDIOCA FINA MATHEUS 500G",
    "PALHA MANDIOCA FINA DECK 1KG",
]
EANS = ["7891000100101", "7891000100102", "7891000100103"]
PRECOS = [10.0, 15.0, 20.0]


def _sessao_copia(diretorio):
    """Sessão SQLAlchemy sobre uma cópia do banco (a base real não é tocada)."""
    copia = Path(diretorio) / "copia_marcas.db"
    shutil.copy(DB_PATH, copia)
    engine = create_engine("sqlite:///" + str(copia).replace("\\", "/"))
    # A cópia pode ter sido tirada de uma base ainda sem as colunas de uma migração
    # aditiva (ex.: empresas.omie_app_key). O init_db roda o mesmo ALTER TABLE do
    # start da API — na cópia, nunca na base real.
    init_db(engine)
    return sessionmaker(bind=engine)(), engine


def _inserir_nota(db, descricoes, eans, precos, data_nota, ncm):
    """Nota de teste com um item por descrição (EAN e preço próprios)."""
    empresa = db.query(Empresa).first()
    fornecedor = db.query(Fornecedor).first()
    nota = NotaFiscal(
        chave_acesso=f"TESTE-MARCAS-{uuid.uuid4().hex[:12]}",
        numero_nota="1",
        serie="1",
        data_emissao=data_nota,
        valor_total=sum(precos),
        empresa_id=empresa.id if empresa else None,
    )
    db.add(nota)
    db.commit()
    db.refresh(nota)
    for descricao, ean, preco in zip(descricoes, eans, precos):
        db.add(
            ItemNota(
                nota_id=nota.id,
                fornecedor_id=fornecedor.id if fornecedor else None,
                ean=ean,
                descricao_produto=descricao,
                quantidade=1.0,
                valor_unitario=preco,
                valor_total=preco,
                ncm=ncm,
                cfop="5102",
            )
        )
    db.commit()


def _media(itens, descricao):
    """Média de mercado que o relatório usa: `_media_preco` dos itens do grupo."""
    grupo = [item for item in itens if item["descricao"] == descricao]
    return bmain._media_preco(grupo), len(grupo)


def main():
    print("=" * 78)
    print("1) EXTRAÇÃO DA MARCA — 3 descrições do mesmo NCM:", NCM_TESTE)
    print("=" * 78)
    genericos = bmarcas.termos_genericos(DESCRICOES)
    print(f"termos do produto (aparecem em >= 50% das descrições): {sorted(genericos)}")
    print(f"\n{'descrição':<40} {'palavras de conteúdo':<26} marca")
    for descricao in DESCRICOES:
        marca = bmarcas.extrair_marca(descricao, genericos)
        print(f"{descricao:<40} {' '.join(bmarcas.tokens(descricao)):<26} {marca}")
    extraidas = [bmarcas.extrair_marca(descricao, genericos) for descricao in DESCRICOES]
    assert extraidas == ["YOKI", "MATHEUS", "DECK"], extraidas
    assert bmarcas.extrair_marca("PALHA MANDIOCA FINA TOLETE 500G", genericos) is None
    print("OK: a marca é a palavra que varia; unidade/embalagem (KG, TOLETE, G...) é ignorada.")

    with tempfile.TemporaryDirectory() as diretorio:
        db, engine = _sessao_copia(diretorio)
        try:
            referencia = bmain._data_referencia_relatorios(db) or date(2026, 1, 15)
            _inserir_nota(db, DESCRICOES, EANS, PRECOS, referencia, NCM_TESTE)

            print()
            print("=" * 78)
            print("2) MÉDIA DE MERCADO (a principal) — antes e depois de extrair a marca")
            print("=" * 78)
            antes = {}
            itens = bmain._carregar_itens_relatorio(db)
            for descricao in DESCRICOES:
                antes[descricao] = _media(itens, descricao)

            print(f"recalcular_marcas: {bmarcas.recalcular_marcas(db)}")

            itens = bmain._carregar_itens_relatorio(db)
            depois = {descricao: _media(itens, descricao) for descricao in DESCRICOES}
            print(f"\n{'produto':<40} {'antes':>8} {'depois':>8} {'esperado':>9}")
            for descricao, preco in zip(DESCRICOES, PRECOS):
                print(
                    f"{descricao:<40} {antes[descricao][0]:>8.2f} "
                    f"{depois[descricao][0]:>8.2f} {preco:>9.2f}"
                )
                assert antes[descricao] == depois[descricao], (antes[descricao], depois[descricao])
                # Cada produto vale o próprio preço: as outras marcas NÃO entram na média.
                assert depois[descricao][0] == round(preco, 4), (descricao, depois[descricao])
            marcas_gravadas = {
                item.descricao_produto: item.marca
                for item in db.query(ItemNota).filter(ItemNota.ean.in_(EANS)).all()
            }
            print(f"\nmarca gravada em itens_nota.marca: {marcas_gravadas}")
            print("OK: a média principal é exatamente a do EAN/descrição — nada mudou.")

            print()
            print("=" * 78)
            print("3) MARCAS SUBSTITUTAS — vão apenas no tooltip da coluna Mercado")
            print("=" * 78)
            for descricao, preco in zip(DESCRICOES, PRECOS):
                dados = bmain.relatorio_comparativo(
                    lojas=None,
                    dias=360,
                    periodicidade="mensal",
                    apenas_divergentes=False,
                    produto=descricao,
                    limite=20000,
                    db=db,
                )
                assert len(dados["linhas"]) == 1, (descricao, dados["linhas"])
                linha = dados["linhas"][0]
                print(f"\n{descricao}  |  Mercado = R$ {linha['mercado_preco_medio']:.2f}")
                assert linha["mercado_preco_medio"] == round(preco, 4), linha
                for marca in linha["mercado_marcas"]:
                    print(
                        f"    -> {marca['marca']:<9} R$ {marca['preco_medio']:>7.2f} "
                        f"({marca['registros']} compra(s), similaridade {marca['similaridade']:.2f})"
                        f" — {marca['descricao']}"
                    )
                substitutas = {marca["marca"] for marca in linha["mercado_marcas"]}
                esperadas = {"YOKI", "MATHEUS", "DECK"} - {marcas_gravadas[descricao]}
                assert esperadas <= substitutas, (descricao, substitutas)
            print("\nOK: as outras marcas aparecem com o preço médio de cada uma.")
            print("\n" + "=" * 78)
            print("TUDO CERTO: extração de marca funcionando e média principal intocada.")
            print("=" * 78)
        finally:
            db.close()
            engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
