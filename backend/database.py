"""Conexão com o banco de dados do sistema (PostgreSQL em nuvem ou SQLite local).

A URL vem da variável de ambiente ``DATABASE_URL`` — por exemplo:

    postgresql://usuario:senha@host:5432/sistema_nfe

Sem a variável, o sistema continua usando o SQLite da pasta PAI do projeto
(``C:\\sistema_nfe\\sistema_nfe_pro.db``), que é o modo de desenvolvimento local e o
fallback que impede a API de quebrar quando a nuvem ainda não foi configurada.

Quem fala com o banco é o SQLAlchemy: models.py, rotas e scripts não mudam. Só esta
camada conhece o dialeto — as migrações aditivas usam o inspector do SQLAlchemy e DDL
válido nos dois bancos (nada de PRAGMA/sqlite_master fora do SQLite).

Para levar os dados que já existem no SQLite para o PostgreSQL:
    python migrar_para_postgres.py --destino postgresql://usuario:senha@host:5432/banco
"""

import os

from dotenv import load_dotenv
from sqlalchemy import (
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.engine import make_url
from sqlalchemy.orm import declarative_base, sessionmaker

# ---------------------------------------------------------------------------
# Caminho do banco local (SQLite) e URL efetiva
# ---------------------------------------------------------------------------
# O banco local fica na pasta PAI do projeto: C:\sistema_nfe\sistema_nfe_pro.db,
# ou seja, "../sistema_nfe_pro.db" a partir da raiz deste projeto.
# O caminho é resolvido de forma ABSOLUTA a partir deste arquivo para que a API
# funcione mesmo que o processo seja iniciado de outra pasta de trabalho. Ele
# também é o banco de ORIGEM do script de migração (migrar_para_postgres.py).
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.abspath(os.path.join(PROJECT_ROOT, "..", "sistema_nfe_pro.db"))
SQLITE_URL = "sqlite:///" + DB_PATH.replace("\\", "/")

# O .env da raiz é lido ANTES de qualquer os.getenv. `override=False` preserva a
# variável injetada pelo serviço de nuvem (Render/Railway/Fly.io...), que tem
# precedência sobre o arquivo local.
load_dotenv(os.path.join(PROJECT_ROOT, ".env"), override=False)


def _url_do_ambiente():
    """URL efetiva: DATABASE_URL do ambiente ou o SQLite local (fallback)."""
    url = (os.getenv("DATABASE_URL") or "").strip()
    if not url:
        return SQLITE_URL
    # Heroku e clones ainda entregam o esquema antigo "postgres://", que o SQLAlchemy
    # não reconhece como PostgreSQL; o driver (psycopg2) é fixado em criar_engine().
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    # SQLite com caminho relativo (ex.: sqlite:///./sistema_nfe_pro.db) é resolvido a
    # partir da raiz do projeto, mantendo o banco local no mesmo lugar de sempre.
    prefixo = "sqlite:///"
    if url.startswith(prefixo):
        caminho = url[len(prefixo):]
        if (
            caminho
            and not caminho.startswith((":memory:", "file:"))
            and not os.path.isabs(caminho)
        ):
            absoluto = os.path.abspath(os.path.join(PROJECT_ROOT, caminho))
            url = prefixo + absoluto.replace("\\", "/")
    return url


DATABASE_URL = _url_do_ambiente()


def _url_com_driver(url):
    """Fixa o driver psycopg2 (o instalado pelo requirements.txt) na URL do PostgreSQL.

    A partir do SQLAlchemy 2.1, `postgresql://` sem driver usa psycopg (v3), que não está
    no projeto — o import falharia com "No module named 'psycopg'". Todo mundo (API,
    scripts e migrar_para_postgres.py) cria a engine por aqui, então o ajuste vale para
    DATABASE_URL, para o .env e para o --destino do script de migração.
    """
    if url.startswith("postgres://"):  # esquema antigo de provedores (Heroku e clones)
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url  # sqlite (local) ou URL que já traz o driver


def criar_engine(url):
    """Engine do SQLAlchemy para a URL informada (SQLite ou PostgreSQL).

    - SQLite: `check_same_thread=False` porque a API (FastAPI) e o agendador de
      alertas usam o banco em threads diferentes;
    - PostgreSQL: `pool_pre_ping` descarta conexões derrubadas pela rede/nuvem e
      `pool_recycle` renova a conexão antes do timeout ocioso do provedor.
    """
    url = _url_com_driver(url)
    if url.startswith("sqlite"):
        return create_engine(url, connect_args={"check_same_thread": False})
    return create_engine(url, pool_pre_ping=True, pool_recycle=1800)


def mascarar_url(url):
    """URL sem a senha — para logs, terminal e mensagens de apoio."""
    try:
        convertida = make_url(url)
        if convertida.password is None:
            return url  # SQLite/caminho local: nada a esconder, e o texto fica legível
        return convertida.render_as_string(hide_password=True)
    except Exception:  # URL digitada errada: não vale derrubar o chamador
        return "(URL do banco inválida)"


engine = criar_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# ---------------------------------------------------------------------------
# Tabelas legadas (não têm classe em models.py)
# ---------------------------------------------------------------------------
# `produtos` (catálogo por EAN) e `compras` (itens crus das notas) vêm do importador
# antigo (xml_parser.py). São criadas aqui com tipos do SQLAlchemy para o PostgreSQL
# gerar SERIAL/INTEGER corretamente — o "INTEGER PRIMARY KEY AUTOINCREMENT" da DDL
# do SQLite não existe lá.
legacy_metadata = MetaData()
produtos = Table(
    "produtos",
    legacy_metadata,
    Column("ean", String, primary_key=True),
    Column("descricao", String),
    Column("ncm", String),
)
compras = Table(
    "compras",
    legacy_metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ean", String),
    Column("cnpj_fornecedor", String),
    Column("data_emissao", String),
    Column("quantidade", Float),
    Column("valor_unitario", Float),
)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db(engine_alvo=None):
    """Cria as tabelas do ORM (models.py) e as tabelas legadas que faltarem.

    `engine_alvo` permite preparar outro banco sem mexer no engine global — é o que
    o script `migrar_para_postgres.py` usa para criar o esquema no destino.
    """
    try:
        from backend import models as orm_models
    except ImportError:  # Permite executar direto: python backend/database.py
        import models as orm_models

    destino = engine if engine_alvo is None else engine_alvo

    # Cria empresas, fornecedores, notas_fiscais, itens_nota, itens_auditados,
    # regras_agrupamento, sugestoes_correcao_ia (sugestões da IA para a Auditoria),
    # configuracoes_alerta (preferências de alerta do cliente), configuracoes_sistema
    # (chave/valor global, ex.: token da API do WhatsApp), marcas_ia (memória/cache
    # das respostas da IA de extração de marca) e marcas_master (catálogo mestre de
    # marcas usado pela cascata de extração antes de consultar a DeepSeek).
    # O create_all é idempotente: cria apenas as tabelas que ainda não existem.
    orm_models.Base.metadata.create_all(bind=destino)

    # Tabelas legadas (produtos/compras) — a DDL vem do MetaData, então o PostgreSQL
    # recebe SERIAL no id de `compras` e o SQLite recebe o AUTOINCREMENT equivalente.
    legacy_metadata.create_all(bind=destino)

    # Uma única transação para as migrações aditivas (idempotentes, sem apagar dados).
    with destino.begin() as conexao:
        _garantir_colunas_novas(conexao)


def _tabelas(conexao):
    """Nomes das tabelas existentes no banco conectado (inspector do SQLAlchemy)."""
    return set(inspect(conexao).get_table_names())


def _colunas(conexao, tabela):
    """Nomes das colunas de uma tabela — usado nas migrações aditivas."""
    inspetor = inspect(conexao)
    if tabela not in inspetor.get_table_names():
        return set()
    return {coluna["name"] for coluna in inspetor.get_columns(tabela)}


def _booleano(conexao, padrao=False):
    """DDL de uma coluna booleana NOT NULL no dialeto conectado.

    No PostgreSQL o ORM mapeia essas colunas como BOOLEAN nativo (INTEGER seria
    recusado); no SQLite continua INTEGER 0/1, que é como a base antiga gravou.
    """
    if conexao.dialect.name == "postgresql":
        return "BOOLEAN NOT NULL DEFAULT " + ("TRUE" if padrao else "FALSE")
    return "INTEGER NOT NULL DEFAULT " + ("1" if padrao else "0")


def _garantir_colunas_novas(conexao):
    """Migrações aditivas: colunas criadas depois da primeira versão do banco.

    O `create_all` só cria tabelas que não existem, então colunas novas precisam de
    ALTER TABLE. Tudo aqui é idempotente (checa antes de alterar) e não apaga dados.

    - itens_nota.cfop: CFOP do item na NFe, usado para excluir bonificação/doação
      (5910/6910) dos relatórios de preço médio;
    - itens_nota.marca: marca inferida da variação das descrições do mesmo NCM
      (backend/marcas.py). Entra na chave inteligente Produto + Marca (médias
      separadas entre marcas) e no tooltip de marcas substitutas dos relatórios;
      a base antiga é preenchida por `python backfill_marcas.py`;
    - itens_auditados.motivo: distingue pendência manual de aberração de variação;
    - empresas.usuario_id: dono da loja (autenticação/multilojas). A tabela `usuarios` é
      criada pelo `create_all`; o ALTER só liga a coluna nas empresas que já existiam;
    - usuarios.is_admin: conta administradora (Auditoria). Contas antigas nascem como
      usuárias comuns (DEFAULT 0) — promover é um ato explícito (ver criar_admin.py);
    - usuarios.totp_secret / totp_ativo: autenticação em dois fatores (TOTP) da Auditoria.
      O segredo é gerado no primeiro acesso ao QR Code e `totp_ativo` só vira 1 quando o
      administrador confirma um código de 6 dígitos;
    - empresas.certificado_*: dados públicos do certificado digital A1 informado no
      vínculo da loja (titular, validade e data do envio);
    - empresas.ultimo_nsu_acbr: último NSU consumido na Distribuição NF-e da ACBr.
      "0" (ou NULL) marca a primeira consulta (DIAS_PRIMEIRO_DOWNLOAD, padrão 7);
    - empresas.omie_app_key / omie_app_secret / omie_sync_em: integração com a API
      do Omie (fase 2, backend/omie_sync.py). As duas credenciais guardam o texto
      CRIPTOGRAFADO ("fernet:v1:...", backend/cripto_credenciais.py) e nascem NULL
      — loja sem credencial usa OMIE_APP_KEY/OMIE_APP_SECRET do .env. `omie_sync_em`
      é o ISO 8601 UTC da última sincronização concluída;
    - empresas.logradouro/numero/complemento/bairro/codigo_municipio/uf/cep: endereço
      fiscal completo, gravado no vínculo da loja e usado como fallback quando a
      BrasilAPI estiver indisponível no cadastro da ACBr;
    - itens_nota.c_prod / unidade_compra / qtd_por_embalagem / unidade_base /
      quantidade_base / valor_unitario_base: memória de aprendizado e tratamento de
      embalagens (backend/aprendizado_produtos.py). `quantidade` e `valor_unitario`
      continuam com o valor CRU do XML; os campos *_base guardam a conversão;
    - mapeamento_fornecedor_produtos.qtd_por_embalagem / unidade_base / origem /
      atualizado_em: completam a tabela de de-para que já existia;
    - configuracoes_alerta.email / canal: preferência de destino dos alertas
      (WhatsApp, e-mail ou ambos). Registros antigos ficam no canal WhatsApp.
    - usuarios.email_verificado / email_verificado_em: confirmação do e-mail no cadastro
      (código de 6 dígitos). O ALTER usa DEFAULT 1 para as contas que já existiam
      continuarem entrando; contas NOVAS são gravadas com 0 pelo ORM (default=False).
    - configuracoes_alerta.alerta_diario/semanal/mensal + ultima_nota_*_id +
      ultimo_envio_*: relatórios automáticos por e-mail (backend/relatorios_alerta.py e
      backend/agendador.py). Os tipos nascem ligados (DEFAULT 1) e os cursores em 0
      (primeira execução não despeja o histórico inteiro).

    O DDL abaixo vale para SQLite e PostgreSQL: os tipos são os mesmos, o dialeto só
    muda os booleanos (via `_booleano`) e o inspector substitui PRAGMA/sqlite_master.
    """

    def executar(sql):
        """Comando cru no dialeto conectado (SQLAlchemy 2.x exige text())."""
        conexao.execute(text(sql))

    tabelas = _tabelas(conexao)

    if "cfop" not in _colunas(conexao, "itens_nota"):
        executar("ALTER TABLE itens_nota ADD COLUMN cfop TEXT")
    executar("CREATE INDEX IF NOT EXISTS ix_itens_nota_cfop ON itens_nota (cfop)")

    if "marca" not in _colunas(conexao, "itens_nota"):
        # Marca inferida (chave Produto + Marca e tooltip). NULL na base antiga até
        # rodar backfill_marcas.py.
        executar("ALTER TABLE itens_nota ADD COLUMN marca TEXT")
    executar("CREATE INDEX IF NOT EXISTS ix_itens_nota_marca ON itens_nota (marca)")

    # Consultas dos alertas: recorte por loja + janela de datas. O índice composto
    # evita varrer todo o histórico de notas para descobrir a última/penúltima
    # compra das lojas selecionadas. `nota_id` acelera o join item -> nota.
    executar(
        "CREATE INDEX IF NOT EXISTS ix_notas_fiscais_empresa_id "
        "ON notas_fiscais (empresa_id)"
    )
    executar(
        "CREATE INDEX IF NOT EXISTS ix_notas_fiscais_empresa_data "
        "ON notas_fiscais (empresa_id, data_emissao)"
    )
    executar(
        "CREATE INDEX IF NOT EXISTS ix_itens_nota_nota_id ON itens_nota (nota_id)"
    )

    if "motivo" not in _colunas(conexao, "itens_auditados"):
        executar("ALTER TABLE itens_auditados ADD COLUMN motivo TEXT")
        # Pendências antigas foram todas enviadas à mão pela aba Impacto Financeiro.
        executar("UPDATE itens_auditados SET motivo = 'manual' WHERE motivo IS NULL")

    if "usuario_id" not in _colunas(conexao, "empresas"):
        executar(
            "ALTER TABLE empresas ADD COLUMN usuario_id INTEGER REFERENCES usuarios(id)"
        )
    executar(
        "CREATE INDEX IF NOT EXISTS ix_empresas_usuario_id ON empresas (usuario_id)"
    )

    if "is_admin" not in _colunas(conexao, "usuarios"):
        # NOT NULL DEFAULT 0 (FALSE no PostgreSQL): quem já tinha conta continua usuário
        # comum. A promoção a administrador é manual (python criar_admin.py email@dominio.com).
        executar("ALTER TABLE usuarios ADD COLUMN is_admin " + _booleano(conexao))

    if "totp_secret" not in _colunas(conexao, "usuarios"):
        # Segredo TOTP (base32): NULL até o administrador abrir o QR Code do 2FA.
        executar("ALTER TABLE usuarios ADD COLUMN totp_secret TEXT")
    if "totp_ativo" not in _colunas(conexao, "usuarios"):
        # DEFAULT 0: conta existente continua entrando só com senha até confirmar o 1º código.
        executar("ALTER TABLE usuarios ADD COLUMN totp_ativo " + _booleano(conexao))

    for coluna in ("certificado_titular", "certificado_validade", "certificado_enviado_em"):
        if coluna not in _colunas(conexao, "empresas"):
            executar(f"ALTER TABLE empresas ADD COLUMN {coluna} TEXT")

    if "ultimo_nsu_acbr" not in _colunas(conexao, "empresas"):
        # DEFAULT '0': lojas antigas passam a ser tratadas como "primeiro download".
        executar(
            "ALTER TABLE empresas ADD COLUMN ultimo_nsu_acbr TEXT DEFAULT '0'"
        )
    executar(
        "UPDATE empresas SET ultimo_nsu_acbr = '0' "
        "WHERE ultimo_nsu_acbr IS NULL OR TRIM(ultimo_nsu_acbr) = ''"
    )

    # ── Credenciais da integração Omie (fase 2) ────────────────────────────────
    # As colunas guardam o texto CIFRADO ("fernet:v1:...") — o segredo em texto
    # nunca é gravado. Nascem NULL: a loja que não configurou a integração usa as
    # credenciais OMIE_APP_KEY/OMIE_APP_SECRET do .env (backend/omie_sync.py).
    for coluna in ("omie_app_key", "omie_app_secret", "omie_sync_em"):
        if coluna not in _colunas(conexao, "empresas"):
            executar(f"ALTER TABLE empresas ADD COLUMN {coluna} TEXT")

    # Endereço fiscal da loja (cadastro na ACBr). Colunas criadas depois da
    # primeira versão do banco; as lojas antigas são preenchidas no próximo
    # vínculo/consulta bem-sucedida da BrasilAPI.
    for coluna in (
        "logradouro",
        "numero",
        "complemento",
        "bairro",
        "codigo_municipio",
        "uf",
        "cep",
    ):
        if coluna not in _colunas(conexao, "empresas"):
            executar(f"ALTER TABLE empresas ADD COLUMN {coluna} TEXT")

    # ── Memória de aprendizado / embalagens ────────────────────────────────────
    colunas_itens = {
        "c_prod": "TEXT",
        "unidade_compra": "TEXT",
        "qtd_por_embalagem": "REAL",
        "unidade_base": "TEXT",
        "quantidade_base": "REAL",
        "valor_unitario_base": "REAL",
    }
    for coluna, tipo in colunas_itens.items():
        if coluna not in _colunas(conexao, "itens_nota"):
            executar(f"ALTER TABLE itens_nota ADD COLUMN {coluna} {tipo}")
    executar(
        "CREATE INDEX IF NOT EXISTS ix_itens_nota_c_prod ON itens_nota (c_prod)"
    )

    colunas_mapeamento = {
        "qtd_por_embalagem": "REAL",
        "unidade_base": "TEXT",
        "origem": "TEXT",
        "atualizado_em": "TEXT",
    }
    if "mapeamento_fornecedor_produtos" in tabelas:
        for coluna, tipo in colunas_mapeamento.items():
            if coluna not in _colunas(conexao, "mapeamento_fornecedor_produtos"):
                executar(
                    f"ALTER TABLE mapeamento_fornecedor_produtos ADD COLUMN {coluna} {tipo}"
                )
        executar(
            "UPDATE mapeamento_fornecedor_produtos SET origem = 'automatico' "
            "WHERE origem IS NULL"
        )

    # ── Preferência de canal dos alertas (WhatsApp/e-mail/ambos) ───────────────
    if "configuracoes_alerta" in tabelas:
        if "email" not in _colunas(conexao, "configuracoes_alerta"):
            executar("ALTER TABLE configuracoes_alerta ADD COLUMN email TEXT")
        if "email_secundario" not in _colunas(conexao, "configuracoes_alerta"):
            # Cópia opcional (Cc) do e-mail de alerta; NULL nos registros antigos.
            executar(
                "ALTER TABLE configuracoes_alerta ADD COLUMN email_secundario TEXT"
            )
        if "canal" not in _colunas(conexao, "configuracoes_alerta"):
            executar(
                "ALTER TABLE configuracoes_alerta ADD COLUMN canal TEXT DEFAULT 'whatsapp'"
            )
        # Registros criados antes da coluna continuam recebendo por WhatsApp.
        executar(
            "UPDATE configuracoes_alerta SET canal = 'whatsapp' WHERE canal IS NULL"
        )
        # Relatórios por e-mail: tipos ligados por padrão (cliente decidiu receber
        # diário, semanal e mensal) e cursores de idempotência em 0 = nunca enviado.
        for coluna in ("alerta_diario", "alerta_semanal", "alerta_mensal"):
            if coluna not in _colunas(conexao, "configuracoes_alerta"):
                executar(
                    f"ALTER TABLE configuracoes_alerta ADD COLUMN {coluna} "
                    + _booleano(conexao, padrao=True)
                )
        for coluna in (
            "ultima_nota_alerta_id",
            "ultima_nota_semanal_id",
            "ultima_nota_mensal_id",
        ):
            if coluna not in _colunas(conexao, "configuracoes_alerta"):
                executar(
                    f"ALTER TABLE configuracoes_alerta ADD COLUMN {coluna} "
                    "INTEGER NOT NULL DEFAULT 0"
                )
        for coluna in (
            "ultimo_envio_diario",
            "ultimo_envio_semanal",
            "ultimo_envio_mensal",
        ):
            if coluna not in _colunas(conexao, "configuracoes_alerta"):
                executar(
                    f"ALTER TABLE configuracoes_alerta ADD COLUMN {coluna} TEXT"
                )

    # ── Confirmação do e-mail no cadastro (código de 6 dígitos) ────────────────
    if "email_verificado" not in _colunas(conexao, "usuarios"):
        # DEFAULT 1: quem já tinha conta (o administrador, inclusive) segue entrando
        # sem confirmar nada. Contas novas nascem com 0 (default=False no ORM) e só
        # recebem sessão depois de POST /api/auth/validar-email.
        executar(
            "ALTER TABLE usuarios ADD COLUMN email_verificado "
            + _booleano(conexao, padrao=True)
        )
    if "email_verificado_em" not in _colunas(conexao, "usuarios"):
        executar("ALTER TABLE usuarios ADD COLUMN email_verificado_em TEXT")


if __name__ == "__main__":
    init_db()
    print(f"✅ Banco de dados inicializado em: {mascarar_url(DATABASE_URL)}")