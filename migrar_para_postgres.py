"""Copia os dados do SQLite local para o PostgreSQL (nuvem).

A API passou a aceitar `DATABASE_URL` (backend/database.py), mas o banco em uso até
agora é o arquivo `../sistema_nfe_pro.db` (SQLite). Este script leva TODO o conteúdo
desse arquivo para o PostgreSQL, preservando os IDs, as datas e os cursores dos alertas —
depois da migração basta apontar `DATABASE_URL` para a nuvem e nada muda na aplicação.

O que ele faz, na ordem:

1. lê a origem com o SQLAlchemy (somente SELECT — o SQLite não é alterado);
2. cria/atualiza o esquema no destino com o próprio `init_db()` da aplicação
   (models.py + tabelas legadas + migrações aditivas);
3. cria no destino as tabelas que só existem na origem (hoje `itens_notas`, histórica);
4. limpa o destino se `--limpar` (TRUNCATE ... RESTART IDENTITY CASCADE);
5. copia tabela por tabela na ordem das chaves estrangeiras, normalizando o que o
   SQLite gravou de forma diferente: booleano 0/1 -> BOOLEAN, data em texto -> DATE,
   texto vazio em data -> NULL;
6. reposiciona cada sequência do PostgreSQL (`setval`) para o próximo INSERT da API
   não colidir com os IDs copiados;
7. confere as contagens origem x destino.

Uso (na raiz do projeto):

    python migrar_para_postgres.py --simular
    python migrar_para_postgres.py --destino postgresql://usuario:senha@host:5432/banco
    python migrar_para_postgres.py            # usa DATABASE_URL do .env/ambiente
    python migrar_para_postgres.py --limpar   # refaz a carga do zero

O destino precisa estar VAZIO na primeira execução; se já houver dados, o script para
e pede `--limpar` em vez de duplicar registros.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Integer,
    MetaData,
    Table,
    create_engine,
    inspect,
    select,
    text,
)
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from backend.database import DB_PATH, criar_engine, init_db, mascarar_url

# Tabela interna do SQLite (controla AUTOINCREMENT) — não existe no PostgreSQL.
TABELAS_IGNORADAS = {"sqlite_sequence"}


# ---------------------------------------------------------------------------
# Argumentos e utilidades
# ---------------------------------------------------------------------------
def _argumentos(argv=None):
    parser = argparse.ArgumentParser(
        description="Copia os dados do SQLite local para o PostgreSQL (nuvem).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Exemplos (na raiz do projeto):\n"
            "  python migrar_para_postgres.py --simular\n"
            "  python migrar_para_postgres.py --destino postgresql://u:s@host:5432/banco\n"
            "  python migrar_para_postgres.py            # usa DATABASE_URL\n"
            "  python migrar_para_postgres.py --limpar   # apaga o destino antes de copiar\n"
        ),
    )
    parser.add_argument(
        "--destino",
        help="URL do PostgreSQL (padrão: DATABASE_URL do .env/ambiente).",
    )
    parser.add_argument(
        "--origem",
        default=DB_PATH,
        help=f"Arquivo SQLite de origem (padrão: {DB_PATH}).",
    )
    parser.add_argument(
        "--lote",
        type=int,
        default=500,
        help="Registros por INSERT (padrão: 500).",
    )
    parser.add_argument(
        "--simular",
        action="store_true",
        help="Só compara a origem com o destino; não cria nem grava nada.",
    )
    parser.add_argument(
        "--limpar",
        action="store_true",
        help="TRUNCATE nas tabelas do destino antes de copiar (refaz a carga).",
    )
    return parser.parse_args(argv)


def _erro(mensagem):
    print("ERRO: " + mensagem, file=sys.stderr)
    return 1


def _url_sqlite(caminho):
    return "sqlite:///" + os.path.abspath(caminho).replace("\\", "/")


def _normalizar_url_postgres(url):
    # Provedores antigos ainda entregam "postgres://", que o SQLAlchemy não reconhece.
    if url.startswith("postgres://"):
        return "postgresql://" + url[len("postgres://"):]
    return url


def _engine_destino(url):
    """Engine do PostgreSQL já testado (falha cedo se a nuvem estiver inacessível)."""
    try:
        engine_destino = criar_engine(url)
        with engine_destino.connect() as conexao:
            conexao.execute(text("SELECT 1"))
    except ModuleNotFoundError as erro:
        raise RuntimeError(
            f"Driver do PostgreSQL ausente ({erro}). Rode: pip install -r requirements.txt"
        ) from erro
    except OperationalError as erro:
        raise RuntimeError(
            "Não foi possível conectar ao PostgreSQL: " + str(erro.orig)
        ) from erro
    return engine_destino


def _contar(engine, tabela):
    with engine.connect() as conexao:
        return int(conexao.execute(text(f'SELECT COUNT(*) FROM "{tabela}"')).scalar() or 0)


# ---------------------------------------------------------------------------
# Esquema: ordem das FKs, tabelas legadas e sequências
# ---------------------------------------------------------------------------
def _ordem_de_carga(engine_destino):
    """Tabelas do destino em ordem de dependência (pai antes do filho).

    Sem isso o INSERT de `itens_nota` falharia por FK: a nota ainda não existiria.
    """
    inspetor = inspect(engine_destino)
    tabelas = [nome for nome in inspetor.get_table_names() if nome not in TABELAS_IGNORADAS]
    dependencias = {
        nome: {
            fk["referred_table"]
            for fk in inspetor.get_foreign_keys(nome)
            if fk["referred_table"] in tabelas and fk["referred_table"] != nome
        }
        for nome in tabelas
    }
    ordem = []
    while dependencias:
        prontas = sorted(nome for nome, deps in dependencias.items() if deps <= set(ordem))
        if not prontas:
            # Ciclo entre tabelas (não existe hoje): cai para a ordem alfabética.
            prontas = sorted(dependencias)
        for nome in prontas:
            ordem.append(nome)
            dependencias.pop(nome)
    return ordem


def _criar_tabelas_ausentes(origem_meta, engine_destino):
    """Cria no destino as tabelas que só existem na origem (hoje `itens_notas`).

    `itens_notas` é uma tabela histórica (5 mil linhas) sem classe no models.py, criada
    pelo importador antigo. O `create_all` não a conhece, então ela é recriada a partir
    da estrutura refletida do SQLite para nenhum dado ficar para trás.
    """
    existentes = set(inspect(engine_destino).get_table_names())
    criadas = []
    for tabela in origem_meta.sorted_tables:
        if tabela.name in existentes or tabela.name in TABELAS_IGNORADAS:
            continue
        tabela.create(bind=engine_destino)
        _garantir_auto_incremento(engine_destino, tabela)
        criadas.append(tabela.name)
    return criadas


def _garantir_auto_incremento(engine_destino, tabela: Table):
    """Dá uma sequência ao PK INTEGER de uma tabela criada por reflexão.

    Uma tabela refletida nasce como `INTEGER PRIMARY KEY` (sem SERIAL), então um INSERT
    futuro sem id explodiria. A sequência criada aqui é a mesma que o SERIAL usaria
    (`<tabela>_<coluna>_seq`) e fica "owned by" a coluna, o que faz o
    `pg_get_serial_sequence`/`setval` da etapa final enxergá-la.
    """
    if engine_destino.dialect.name != "postgresql":
        return
    pks = inspect(engine_destino).get_pk_constraint(tabela.name).get("constrained_columns") or []
    if len(pks) != 1:
        return
    coluna = pks[0]
    if not isinstance(tabela.c[coluna].type, Integer):
        return
    sequencia = f"{tabela.name}_{coluna}_seq"
    with engine_destino.begin() as conexao:
        conexao.execute(text(f'CREATE SEQUENCE IF NOT EXISTS "{sequencia}"'))
        conexao.execute(
            text(
                f'ALTER TABLE "{tabela.name}" ALTER COLUMN "{coluna}" '
                f"SET DEFAULT nextval('\"{sequencia}\"')"
            )
        )
        conexao.execute(
            text(f'ALTER SEQUENCE "{sequencia}" OWNED BY "{tabela.name}"."{coluna}"')
        )


def _limpar_destino(engine_destino, tabelas):
    """Apaga o conteúdo do destino (só com --limpar) e reinicia as identidades."""
    if not tabelas:
        return
    lista = ", ".join(f'"{nome}"' for nome in tabelas)
    with engine_destino.begin() as conexao:
        conexao.execute(text(f"TRUNCATE TABLE {lista} RESTART IDENTITY CASCADE"))


def _reiniciar_sequences(engine_destino):
    """Reposiciona cada sequência para MAX(id) + 1 depois da cópia explícita dos IDs.

    Sem isso o primeiro INSERT da API tentaria id=1 e bateria na PK já copiada.
    Devolve a quantidade de sequências ajustadas.
    """
    inspetor = inspect(engine_destino)
    ajustadas = 0
    with engine_destino.begin() as conexao:
        for nome in inspetor.get_table_names():
            pks = inspetor.get_pk_constraint(nome).get("constrained_columns") or []
            if len(pks) != 1:
                continue
            coluna = pks[0]
            tipos = {coluna_info["name"]: coluna_info["type"] for coluna_info in inspetor.get_columns(nome)}
            if not isinstance(tipos.get(coluna), Integer):
                continue
            sequencia = conexao.execute(
                text("SELECT pg_get_serial_sequence(:tabela, :coluna)"),
                {"tabela": nome, "coluna": coluna},
            ).scalar()
            if not sequencia:
                continue
            conexao.execute(
                text(
                    f'SELECT setval(:seq, COALESCE((SELECT MAX("{coluna}") '
                    f'FROM "{nome}"), 0) + 1, false)'
                ),
                {"seq": sequencia},
            )
            ajustadas += 1
    return ajustadas


# ---------------------------------------------------------------------------
# Conversão dos tipos gravados pelo SQLite
# ---------------------------------------------------------------------------
def _ler_data(texto, com_hora=False):
    """Texto -> date/datetime. Aceita ISO (o formato atual) e dd/mm/aaaa (legado)."""
    texto = texto.strip()
    if not texto:
        return None
    for formato in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            base = dt.datetime.strptime(texto[:10], formato)
        except ValueError:
            continue
        return base if com_hora else base.date()
    return None


def _converter(valor, coluna):
    """Ajusta o valor do SQLite ao tipo da coluna do PostgreSQL.

    - BOOLEAN: o SQLite gravou 0/1 (INTEGER) por causa dos ALTERs antigos;
    - DATE: `notas_fiscais.data_emissao` é DATE no ORM e texto no SQLite. Texto vazio
      vira NULL (o PostgreSQL recusaria '' em DATE);
    - o resto passa como está (TEXT/VARCHAR, INTEGER, FLOAT/REAL).
    """
    if valor is None:
        return None
    if isinstance(coluna.type, Boolean):
        if isinstance(valor, str):
            return valor.strip().lower() in ("1", "true", "t", "sim", "yes", "s")
        return bool(valor)
    if isinstance(coluna.type, DateTime):
        return _ler_data(valor, com_hora=True) if isinstance(valor, str) else valor
    if isinstance(coluna.type, Date):
        return _ler_data(valor) if isinstance(valor, str) else valor
    return valor


# ---------------------------------------------------------------------------
# Cópia
# ---------------------------------------------------------------------------
def _copiar_tabela(origem_engine, origem_tabela, destino_engine, destino_tabela, lote):
    """Copia todas as linhas preservando os IDs; devolve quantas foram gravadas.

    Uma transação por tabela: se algo falhar (FK, UNIQUE, tipo), a tabela fica intacta e
    o script para — nada de destino pela metade.
    """
    colunas = [coluna.name for coluna in destino_tabela.columns if coluna.name in origem_tabela.c]
    if not colunas:
        return 0
    consulta = select(*[origem_tabela.c[nome] for nome in colunas])
    total = 0
    with origem_engine.connect() as origem, destino_engine.begin() as destino:
        resultado = origem.execution_options(stream_results=True).execute(consulta)
        buffer = []
        for linha in resultado:
            buffer.append(
                {
                    nome: _converter(valor, destino_tabela.c[nome])
                    for nome, valor in zip(colunas, linha)
                }
            )
            if len(buffer) >= lote:
                destino.execute(destino_tabela.insert(), buffer)
                total += len(buffer)
                buffer = []
        if buffer:
            destino.execute(destino_tabela.insert(), buffer)
            total += len(buffer)
    return total


# ---------------------------------------------------------------------------
# Simulação
# ---------------------------------------------------------------------------
def _simular(origem_engine, destino_engine, origem_meta, ordem):
    """Mostra o que será copiado sem criar nem gravar nada no destino."""
    nomes = list(ordem)
    nomes += [
        nome
        for nome in sorted(origem_meta.tables)
        if nome not in nomes and nome not in TABELAS_IGNORADAS
    ]
    print("\nSIMULAÇÃO — nada será gravado no destino.")
    print(f"{'tabela':<30}{'origem':>8}{'destino':>10}")
    print("-" * 48)
    ocupado = False
    na_origem = 0
    for nome in nomes:
        origem_n = _contar(origem_engine, nome) if nome in origem_meta.tables else 0
        na_origem += origem_n
        try:
            destino_n = str(_contar(destino_engine, nome))
        except SQLAlchemyError:
            destino_n = "ausente"
        print(f"{nome:<30}{origem_n:>8}{destino_n:>10}")
        if destino_n not in ("ausente", "0"):
            ocupado = True
    print("-" * 48)
    print(f"Total de linhas na origem: {na_origem}")
    if ocupado:
        print("\nO destino já tem dados. Rode sem --simular e com --limpar para refazer a carga.")
        return 1
    print("\nDestino vazio: pode rodar sem --simular para copiar.")
    return 0


# ---------------------------------------------------------------------------
# Execução
# ---------------------------------------------------------------------------
def main(argv=None):
    args = _argumentos(argv)
    url_destino = _normalizar_url_postgres(
        (args.destino or os.getenv("DATABASE_URL") or "").strip()
    )
    if not url_destino:
        return _erro(
            "Destino não informado. Use --destino postgresql://usuario:senha@host:5432/banco "
            "ou defina DATABASE_URL no .env."
        )
    if url_destino.startswith("sqlite"):
        return _erro(
            "O destino precisa ser PostgreSQL e o DATABASE_URL ainda aponta para o SQLite.\n"
            "  Defina no .env:  DATABASE_URL=postgresql://usuario:senha@host:5432/sistema_nfe\n"
            f"  (a origem é lida automaticamente de {args.origem})"
        )
    if not os.path.exists(args.origem):
        return _erro(f"Banco de origem não encontrado: {args.origem}")
    if args.lote < 1:
        return _erro("--lote precisa ser maior que zero.")

    print(f"Origem : {args.origem}")
    print(f"Destino: {mascarar_url(url_destino)}")

    origem_engine = create_engine(_url_sqlite(args.origem))
    origem_meta = MetaData()
    origem_meta.reflect(bind=origem_engine)

    try:
        destino_engine = _engine_destino(url_destino)
    except RuntimeError as erro:
        return _erro(str(erro))

    if args.simular:
        return _simular(
            origem_engine, destino_engine, origem_meta, _ordem_de_carga(destino_engine)
        )

    print("\nCriando/atualizando o esquema no destino...")
    init_db(destino_engine)
    criadas = _criar_tabelas_ausentes(origem_meta, destino_engine)
    if criadas:
        print("  Tabelas criadas a partir da origem: " + ", ".join(criadas))

    ordem = _ordem_de_carga(destino_engine)
    ocupadas = {
        nome: total
        for nome, total in ((nome, _contar(destino_engine, nome)) for nome in ordem)
        if total
    }
    if ocupadas:
        if not args.limpar:
            detalhe = ", ".join(f"{nome}={total}" for nome, total in sorted(ocupadas.items()))
            return _erro(
                f"O destino já tem dados ({detalhe}). Rode de novo com --limpar para apagar "
                "o destino e copiar do zero."
            )
        print("  Limpando o destino (TRUNCATE ... RESTART IDENTITY CASCADE)...")
        _limpar_destino(destino_engine, ordem)

    destino_meta = MetaData()
    destino_meta.reflect(bind=destino_engine, only=list(ordem))

    print("\nCopiando os dados...")
    copiados = {}
    for nome in ordem:
        tabela_origem = origem_meta.tables.get(nome)
        if tabela_origem is None:
            print(f"  - {nome}: não existe na origem (0 registro(s))")
            copiados[nome] = 0
            continue
        try:
            copiados[nome] = _copiar_tabela(
                origem_engine,
                tabela_origem,
                destino_engine,
                destino_meta.tables[nome],
                args.lote,
            )
        except SQLAlchemyError as erro:
            return _erro(
                f"falha ao copiar {nome}: {erro.orig}\n"
                "  A tabela ficou intacta; corrija o problema e rode de novo com --limpar."
            )
        print(f"  ✓ {nome}: {copiados[nome]} registro(s)")

    print("\nReposicionando as sequências do PostgreSQL...")
    print(f"  {_reiniciar_sequences(destino_engine)} sequência(s) ajustada(s)")

    print("\nConferindo as contagens...")
    tudo_certo = True
    for nome in ordem:
        origem_n = _contar(origem_engine, nome) if nome in origem_meta.tables else 0
        destino_n = _contar(destino_engine, nome)
        if origem_n != destino_n:
            tudo_certo = False
        marca = "OK " if origem_n == destino_n else "DIF"
        print(f"  {marca} {nome:<30} origem={origem_n:<6} destino={destino_n}")

    if not tudo_certo:
        return _erro("As contagens não batem — confira as mensagens acima.")

    total = sum(copiados.values())
    print(f"\n✅ Migração concluída: {total} registro(s) em {mascarar_url(url_destino)}")
    print("   Defina DATABASE_URL com essa URL para a API passar a usar o PostgreSQL.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
