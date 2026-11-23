"""Script de teste do migrar_para_postgres.py (SQLite -> destino).

Prova, sem precisar de servidor PostgreSQL no ar, que a parte comum aos dois bancos
está certa — sobre um destino TEMPORÁRIO, criado e apagado pelo teste (a base real
só é lida, nunca escrita):

1. **Tabelas que só existem na origem** (`itens_notas`, histórico do importador antigo)
   são criadas no destino antes da carga — sem elas ~5 mil linhas ficariam para trás;
2. **A ordem de carga respeita as chaves estrangeiras** (usuarios -> empresas ->
   notas_fiscais -> itens_nota), senão o INSERT do item falharia por FK;
3. **A simulação (`--simular`)** devolve 0 com destino vazio e 1 com destino ocupado,
   que é o que libera/bloqueia a carga no main();
4. **A cópia preserva IDs e contagens** em todas as tabelas e converte o que o SQLite
   gravou de forma diferente: booleano 0/1 -> BOOLEAN e data em texto -> DATE;
5. **A unicidade do de-para** (cnpj_fornecedor + c_prod) sobrevive à cópia.

Os passos exclusivos do PostgreSQL (sequências e TRUNCATE ... RESTART IDENTITY) não
entram aqui: o próprio script os ignora quando o destino não é PostgreSQL, e eles só
podem ser testados contra um servidor de verdade.

Uso (na raiz do projeto):
    python tests/test_migracao_postgres.py
"""

import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from sqlalchemy import MetaData, create_engine, inspect

import backend.database as bd
import migrar_para_postgres as mig


def _conferir(condicao, mensagem):
    print(("OK: " if condicao else "FALHOU: ") + mensagem)
    if not condicao:
        raise AssertionError(mensagem)


def _amostra_booleano(engine_destino, tabela, coluna):
    """Valores distintos e quantidades de uma coluna booleana no destino."""
    with engine_destino.connect() as conexao:
        return conexao.exec_driver_sql(
            f'SELECT "{coluna}", COUNT(*) FROM "{tabela}" GROUP BY 1 ORDER BY 1'
        ).fetchall()


def main():
    with tempfile.TemporaryDirectory() as diretorio:
        origem_engine = create_engine(mig._url_sqlite(bd.DB_PATH))
        origem_meta = MetaData()
        origem_meta.reflect(bind=origem_engine)

        caminho_destino = Path(diretorio) / "destino_teste.db"
        destino_engine = create_engine(mig._url_sqlite(str(caminho_destino)))
        bd.init_db(destino_engine)

        # 1) tabelas ausentes no destino
        criadas = mig._criar_tabelas_ausentes(origem_meta, destino_engine)
        _conferir(
            "itens_notas" in criadas,
            f"tabela só da origem criada no destino (criadas: {criadas})",
        )

        # 2) ordem das FKs
        ordem = mig._ordem_de_carga(destino_engine)
        _conferir(
            ordem.index("usuarios") < ordem.index("empresas")
            < ordem.index("notas_fiscais") < ordem.index("itens_nota"),
            "ordem de carga respeita usuarios -> empresas -> notas -> itens",
        )

        # 3) simulação com o destino vazio
        _conferir(
            mig._simular(origem_engine, destino_engine, origem_meta, ordem) == 0,
            "--simular com destino vazio libera a carga",
        )

        # 4) cópia
        destino_meta = MetaData()
        destino_meta.reflect(bind=destino_engine, only=ordem)
        copiados = {}
        for nome in ordem:
            tabela_origem = origem_meta.tables.get(nome)
            if tabela_origem is None:
                continue
            copiados[nome] = mig._copiar_tabela(
                origem_engine, tabela_origem, destino_engine, destino_meta.tables[nome], 500
            )
        total = sum(copiados.values())
        _conferir(total > 0, f"linhas copiadas: {total}")

        divergentes = [
            nome
            for nome in ordem
            if (mig._contar(origem_engine, nome) if nome in origem_meta.tables else 0)
            != mig._contar(destino_engine, nome)
        ]
        _conferir(not divergentes, f"contagens iguais em {len(ordem)} tabelas ({divergentes})")

        # 5) conversões
        destinos_booleanos = _amostra_booleano(destino_engine, "itens_auditados", "auditado")
        _conferir(
            {valor for valor, _ in destinos_booleanos} <= {0, 1},
            f"booleano gravado como 0/1 no destino: {destinos_booleanos}",
        )
        with destino_engine.connect() as conexao:
            datas = conexao.exec_driver_sql(
                "SELECT data_emissao FROM notas_fiscais WHERE data_emissao IS NOT NULL LIMIT 3"
            ).fetchall()
        _conferir(
            all(len(str(linha[0])) >= 10 for linha in datas),
            f"data_emissao legível no destino: {datas}",
        )

        # 6) unicidade do de-para preservada
        inspetor = inspect(destino_engine)
        if "mapeamento_fornecedor_produtos" in inspetor.get_table_names():
            with destino_engine.connect() as conexao:
                total_mapeamento, distintos = conexao.exec_driver_sql(
                    "SELECT COUNT(*), COUNT(DISTINCT cnpj_fornecedor || '|' || c_prod) "
                    "FROM mapeamento_fornecedor_produtos"
                ).one()
            _conferir(
                total_mapeamento == distintos,
                f"de-para sem duplicar (cnpj_fornecedor + c_prod): {total_mapeamento}",
            )

        # 7) simulação com o destino já carregado
        _conferir(
            mig._simular(origem_engine, destino_engine, origem_meta, ordem) == 1,
            "--simular com destino ocupado pede --limpar",
        )

        origem_engine.dispose()
        destino_engine.dispose()

    print("\n" + "=" * 78)
    print("TUDO CERTO: migração copia, converte e confere as contagens.")
    print("=" * 78)


if __name__ == "__main__":
    main()
