from sqlalchemy import (
    Boolean,
    Column,
    Date,
    Float,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()

class Usuario(Base):
    """Conta do cliente do SaaS (login por e-mail e senha).

    A senha NUNCA é gravada em texto: `senha_hash` guarda o hash do passlib (bcrypt).
    Cada usuário é dono de um conjunto de empresas (CNPJs) — ver `Empresa.usuario_id`.
    """
    __tablename__ = "usuarios"
    id = Column(Integer, primary_key=True, index=True)
    nome = Column(String, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    senha_hash = Column(String, nullable=False)
    # Administrador do sistema: única conta que enxerga/executa a Auditoria. A flag é
    # conferida no backend (`auth.get_admin_user`), não apenas escondida no menu.
    is_admin = Column(Boolean, nullable=False, default=False)
    # Autenticação em dois fatores (TOTP). O segredo nasce quando o admin abre o QR Code
    # em /api/auth/2fa/setup e `totp_ativo` só vira True depois que ele confirma um código
    # de 6 dígitos válido — assim um QR gerado e não escaneado não tranca a conta.
    totp_secret = Column(String)
    totp_ativo = Column(Boolean, nullable=False, default=False)
    # Confirmação do e-mail no cadastro: a conta só recebe sessão depois do código de
    # 6 dígitos (POST /api/auth/validar-email). Contas criadas ANTES desta coluna são
    # migradas como verificadas em `database._garantir_colunas_novas` (DEFAULT 1); as
    # contas novas são gravadas com `False` pelo próprio ORM.
    email_verificado = Column(Boolean, nullable=False, default=False)
    email_verificado_em = Column(String)  # ISO 8601 UTC da confirmação
    empresas = relationship("Empresa", back_populates="usuario")


class CodigoEmail(Base):
    """Código de 6 dígitos enviado ao e-mail da conta (confirmação de cadastro).

    O código NUNCA é gravado em texto: `codigo_hash` é o SHA-256 dos dígitos + segredo
    do processo (`auth.hash_codigo_email`). Cada linha serve uma única vez
    (`usado_em`), expira em `expira_em` e aceita no máximo `auth.TENTATIVAS_CODIGO_EMAIL`
    erros — depois disso o cliente precisa pedir outro código
    (/api/auth/reenviar-codigo).

    A tabela é criada pelo `create_all`; é histórica (não é apagada ao confirmar) para
    permitir auditoria de envios e o limite de reenvios por hora.
    """

    __tablename__ = "codigos_email"
    id = Column(Integer, primary_key=True, index=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), index=True, nullable=False)
    email = Column(String, nullable=False)
    finalidade = Column(String, nullable=False, default="validacao_email")
    codigo_hash = Column(String, nullable=False)
    criado_em = Column(String, nullable=False)  # ISO 8601 UTC
    expira_em = Column(String, nullable=False)  # ISO 8601 UTC
    usado_em = Column(String)  # ISO 8601 UTC (NULL = ainda vale)
    tentativas = Column(Integer, nullable=False, default=0)
    usuario = relationship("Usuario")

class Empresa(Base):
    __tablename__ = "empresas"
    id = Column(Integer, primary_key=True, index=True)
    cnpj = Column(String, unique=True, index=True)
    razao_social = Column(String)
    nome_loja = Column(String)
    municipio = Column(String)
    # Endereço fiscal completo. É gravado no vínculo da loja a partir da BrasilAPI e
    # usado como fallback quando a consulta do CNPJ estiver indisponível (ex.: 429)
    # no momento de cadastrar a empresa na ACBr.
    logradouro = Column(String)
    numero = Column(String)
    complemento = Column(String)
    bairro = Column(String)
    codigo_municipio = Column(String)  # código IBGE com 7 dígitos
    uf = Column(String)
    cep = Column(String)  # somente dígitos
    # Dono da loja. Fica NULL nas empresas importadas antes do login existir; elas são
    # adotadas pela primeira conta que cadastrar o CNPJ em "Minhas Lojas".
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), index=True)
    usuario = relationship("Usuario", back_populates="empresas")
    # Certificado digital A1 (.pfx/.p12) conferido no vínculo da loja. Guardamos só os
    # dados PÚBLICOS do certificado: o arquivo e a senha nunca são gravados no banco.
    certificado_titular = Column(String)
    certificado_validade = Column(String)  # "DD/MM/AAAA"
    certificado_enviado_em = Column(String)  # ISO 8601 do último envio
    # Fonte das notas da loja: "omie" (API de recebimentos) ou "certificado"
    # (A1 + Distribuição NF-e da ACBr/SEFAZ). NULL nas lojas antigas; a leitura
    # (`main._fonte_notas_da_loja`) deriva do que a loja tem configurado para a
    # tela nunca ficar sem valor.
    fonte_notas = Column(String)
    # Concordância explícita do cliente com o download direto na SEFAZ. Fica
    # registrada com data/hora (ISO 8601 UTC) e o e-mail de quem confirmou: é a
    # prova de que as notas não são baixadas em outro sistema (evita conflito na
    # distribuição). Apagada junto com o certificado.
    cert_concordancia_em = Column(String)
    cert_concordancia_por = Column(String)
    # Último NSU já consumido na Distribuição NF-e da ACBr. "0" (ou NULL) significa
    # que a loja nunca baixou nada; o primeiro download usa 7 dias por padrão.
    ultimo_nsu_acbr = Column(String, nullable=True, default="0")
    # ── Integração com a API do Omie (fase 2: importação dos recebimentos) ─────
    # `app_key`/`app_secret` são do aplicativo criado em app.omie.com.br e ficam
    # CRIPTOGRAFADOS no banco (backend/cripto_credenciais.py): o valor gravado é
    # "fernet:v1:<token>", nunca o segredo em texto. A chave de criptografia vem de
    # OMIE_ENC_KEY (ou NFE_SECRET_KEY/.secret_key) e NÃO é gravada aqui — um dump do
    # banco sozinho não permite chamar a API em nome da loja. NULL = integração
    # desligada para esta empresa (o sincronizador aceita as credenciais do .env).
    omie_app_key = Column(String)
    omie_app_secret = Column(String)
    # ISO 8601 UTC da última sincronização concluída (relatório/agendador da fase 3).
    omie_sync_em = Column(String)
    notas = relationship("NotaFiscal", back_populates="empresa")

class Fornecedor(Base):
    __tablename__ = "fornecedores"
    id = Column(Integer, primary_key=True, index=True)
    cnpj = Column(String, unique=True, index=True)
    nome = Column(String)
    itens = relationship("ItemNota", back_populates="fornecedor")

class NotaFiscal(Base):
    __tablename__ = "notas_fiscais"
    id = Column(Integer, primary_key=True, index=True)
    chave_acesso = Column(String, unique=True, index=True)
    numero_nota = Column(String)
    serie = Column(String)
    data_emissao = Column(Date, index=True)
    valor_total = Column(Float)
    caminho_xml = Column(String)
    empresa_id = Column(Integer, ForeignKey("empresas.id"), index=True)
    empresa = relationship("Empresa", back_populates="notas")
    itens = relationship("ItemNota", back_populates="nota", cascade="all, delete-orphan")

class ItemNota(Base):
    __tablename__ = "itens_nota"
    id = Column(Integer, primary_key=True, index=True)
    nota_id = Column(Integer, ForeignKey("notas_fiscais.id"), index=True)
    fornecedor_id = Column(Integer, ForeignKey("fornecedores.id"))
    ean = Column(String, index=True)
    descricao_produto = Column(String)
    # ── Correção da descrição pelo XML da nota (backend/omie_sync.py) ─────────────
    # A listagem de recebimentos do Omie pode trazer a descrição do CADASTRO interno
    # (ex.: "Skirt Steak" em vez de "FRALDA") por causa de vínculo manual errado. Na
    # etapa 40 (Faturado) o xProd do XML da NF-e é a descrição do fornecedor e passa a
    # ser gravado em `descricao_produto`; o texto que estava antes fica preservado em
    # `descricao_produto_anterior` (a convenção de guardar o valor cru para auditoria).
    # `descricao_origem` diz de onde veio a descrição ATUAL ("XML etapa 40") e é o que
    # impede uma segunda sobrescrita quando a nota chega em 60/80 (só conferência).
    descricao_produto_anterior = Column(String)
    descricao_origem = Column(String)
    quantidade = Column(Float)
    valor_unitario = Column(Float)
    valor_total = Column(Float)
    ncm = Column(String, default="00000000")
    # Marca inferida pelo importador (backend/marcas.py): a palavra que VARIA entre as
    # descrições do mesmo NCM ("... MANDIOCA FINA YOKI" x "... MATHEUS"). Usada só no
    # tooltip dos relatórios (marcas substitutas) — NENHUM preço médio usa esta coluna.
    marca = Column(String, index=True)
    # CFOP do item na NFe (ex.: 5102 venda, 5910/6910 bonificação/doação/brinde).
    # Os relatórios de preço excluem 5910/6910: o item não tem preço real de compra.
    cfop = Column(String, index=True)
    # ── Memória de aprendizado / embalagens (backend/aprendizado_produtos.py) ──────
    # Código do produto NO FORNECEDOR (tag cProd do XML). É a chave do de-para: o mesmo
    # fornecedor pode mudar o EAN/descrição, mas o cProd costuma permanecer estável.
    c_prod = Column(String, index=True)
    # Unidade comercial do XML (uCom): "UN", "KG", "CX", "FD", "PCT"...
    unidade_compra = Column(String)
    # Quantas unidades vendáveis (ou quanto conteúdo, quando a uCom é embalagem de peso)
    # existem em 1 embalagem de compra. 1.0 = sem embalagem identificada.
    qtd_por_embalagem = Column(Float)
    # Unidade do preço normalizado: "UN", "KG", "L"...
    unidade_base = Column(String)
    # Quantidade e preço unitário convertidos para a unidade base. `quantidade` e
    # `valor_unitario` continuam guardando o valor CRU do XML (auditoria/reprocessamento).
    quantidade_base = Column(Float)
    valor_unitario_base = Column(Float)
    nota = relationship("NotaFiscal", back_populates="itens")
    fornecedor = relationship("Fornecedor", back_populates="itens")

class MapeamentoFornecedorProduto(Base):
    """Memória de aprendizado (de-para) do produto do fornecedor para o produto interno.

    A chave é (CNPJ do fornecedor + cProd do XML). O primeiro item aprendido grava o
    EAN/descrição/NCM/embalagem canônicos; as próximas notas do mesmo fornecedor reutilizam
    esses dados mesmo que o XML venha com EAN "SEM GTIN" ou descrição abreviada.

    `origem`:
    - "automatico": aprendido na importação (o admin pode corrigir pela Auditoria);
    - "manual": corrigido pelo administrador — a importação NÃO sobrescreve.
    """

    __tablename__ = "mapeamento_fornecedor_produtos"
    id = Column(Integer, primary_key=True, index=True)
    cnpj_fornecedor = Column(String, index=True)
    c_prod = Column(String, index=True)
    ean_padrao = Column(String)
    descricao_padrao = Column(String)
    ncm_gravado = Column(String)
    u_com_gravada = Column(String)
    qtd_por_embalagem = Column(Float)
    unidade_base = Column(String)
    origem = Column(String, default="automatico")
    atualizado_em = Column(String)  # ISO 8601 da última atualização

class ItemAuditado(Base):
    __tablename__ = "itens_auditados"
    id = Column(Integer, primary_key=True, index=True)
    chave_identificacao = Column(String, unique=True, index=True)
    auditado = Column(Boolean, default=True)
    # Por que a chave está pendente (auditado=False):
    # - "manual": enviada pelo usuário na aba Impacto Financeiro;
    # - "aberrante": variação de preço acima do limite (enviada automaticamente).
    # Pendências "manual" saem dos relatórios; as "aberrante" continuam visíveis nas
    # tabelas marcadas com o alerta, para o auditor validar sem perder o dado de vista.
    motivo = Column(String, default="manual")
    # ── Trilha das correções automáticas de descrição (backend/omie_sync.py) ──────
    # Cada correção pelo xProd do XML da NF-e grava uma linha com ``auditado=True``
    # (é registro histórico, não pendência) e chave ``XML_<chave_da_nota>_<item>`` —
    # fora do prefixo ``DESC_``, então os relatórios não mudam por causa dela.
    # `fonte` guarda a origem ("XML etapa 40") e `registrado_em` o ISO 8601 UTC.
    descricao_antes = Column(String)
    descricao_depois = Column(String)
    fonte = Column(String)
    registrado_em = Column(String)

class RegraAgrupamento(Base):
    __tablename__ = "regras_agrupamento"
    id = Column(Integer, primary_key=True, index=True)
    chave_origem = Column(String, unique=True, index=True)
    chave_destino = Column(String, index=True)
    tipo = Column(String, default="juntar")  # "juntar" ou "mover"


class SugestaoCorrecaoIA(Base):
    """Sugestão da IA para corrigir um agrupamento — NUNCA aplicada sozinha.

    A rota de auditoria em massa apenas grava a sugestão. A base (ItensNota,
    ItemAuditado e RegraAgrupamento) só muda quando o administrador clica em
    "Aplicar" na aba de Auditoria. `status_aprovacao` guarda o ciclo:

    - ``pendente``: aguardando decisão do auditor;
    - ``aprovada``: aplicada como uma regra de-para do tipo "mover";
    - ``ignorada``: descartada pelo auditor (não é recriada na próxima rodada).
    """

    __tablename__ = "sugestoes_correcao_ia"
    id = Column(Integer, primary_key=True, index=True)
    grupo_origem = Column(String, nullable=False, index=True)
    item_sugerido_sair = Column(String, nullable=False)
    motivo_ia = Column(String)
    status_aprovacao = Column(String, nullable=False, default="pendente", index=True)
    # Preenchidos só depois do "Aplicar": grupo NOVO_ criado e data da aplicação.
    grupo_destino = Column(String)
    criado_em = Column(String)
    atualizado_em = Column(String)
    aplicado_em = Column(String)
    __table_args__ = (
        UniqueConstraint(
            "grupo_origem",
            "item_sugerido_sair",
            name="uq_sugestao_ia_grupo_item",
        ),
    )


class ConfiguracaoAlerta(Base):
    """Preferências do cliente para os alertas de preço (uma linha por conta).

    O motor de alertas (`backend/alertas.py`) monta a mensagem a partir destas regras e do
    mesmo histórico usado pelos relatórios — nenhum preço médio é recalculado aqui. O envio
    real depende do token da API do WhatsApp, que é global e fica em
    `configuracoes_sistema` (só o administrador vê/edita).

    `email` é o destino dos alertas por e-mail, `email_secundario` é a cópia opcional
    (campo "Cc" da mensagem) e `canal` escolhe a preferência do cliente:
    ``whatsapp``, ``email`` ou ``ambos``. A validação exige apenas os dados do canal
    escolhido (telefone para WhatsApp, e-mail para e-mail).

    `lojas` guarda os CNPJs escolhidos separados por vírgula; vazio/NULL = todas as lojas
    da conta. `variacao_minima` é o corte (%) para um item entrar como aumento/queda e
    `janela_dias` é o período olhado para trás a partir da última nota importada.
    """
    __tablename__ = "configuracoes_alerta"
    id = Column(Integer, primary_key=True, index=True)
    usuario_id = Column(
        Integer, ForeignKey("usuarios.id"), unique=True, index=True, nullable=False
    )
    ativo = Column(Boolean, nullable=False, default=True)
    # Destino no formato internacional só com dígitos (ex.: 5511999999999). Nunca há
    # envio sem telefone: a rota valida antes de gravar (backend/alertas.py).
    telefone = Column(String)
    # Destino de e-mail (texto simples já validado pela rota). `canal` é o enum
    # whatsapp/email/ambos; NULL em registros antigos é lido como whatsapp.
    email = Column(String)
    # Cópia opcional (Cc) do e-mail de alerta. Também é apenas um endereço de texto
    # validado na rota: nenhuma credencial SMTP passa pelo banco.
    email_secundario = Column(String)
    canal = Column(String, default="whatsapp")
    variacao_minima = Column(Float, nullable=False, default=5.0)
    janela_dias = Column(Integer, nullable=False, default=30)
    incluir_aumentos = Column(Boolean, nullable=False, default=True)
    incluir_divergencias = Column(Boolean, nullable=False, default=True)
    lojas = Column(String)  # CNPJs separados por "," (vazio = todas as lojas da conta)
    atualizado_em = Column(String)  # ISO 8601 da última alteração
    # ── Relatórios por e-mail (backend/relatorios_alerta.py) ───────────────────
    # Tipos que o agendador envia (06:00): diário (toda nota nova), semanal (segunda)
    # e mensal (dia 1º). As colunas abaixo guardam a idempotência do disparo:
    # `ultima_nota_*` é o cursor por ORDEM DE IMPORTAÇÃO (não por data de emissão) e
    # `ultimo_envio_*` impede dois envios do mesmo dia/semana/mês.
    alerta_diario = Column(Boolean, nullable=False, default=True)
    alerta_semanal = Column(Boolean, nullable=False, default=True)
    alerta_mensal = Column(Boolean, nullable=False, default=True)
    ultima_nota_alerta_id = Column(Integer, nullable=False, default=0)
    ultima_nota_semanal_id = Column(Integer, nullable=False, default=0)
    ultima_nota_mensal_id = Column(Integer, nullable=False, default=0)
    ultimo_envio_diario = Column(String)  # "AAAA-MM-DD"
    ultimo_envio_semanal = Column(String)  # "AAAA-MM-DD" da segunda-feira
    ultimo_envio_mensal = Column(String)  # "AAAA-MM" do mês fechado
    usuario = relationship("Usuario")


class AlertaNota(Base):
    """Nota fiscal já coberta pelo alerta de UMA conta (histórico/auditoria).

    O controle de "o que já foi enviado" é feito pelos cursores de
    `ConfiguracaoAlerta` (por ordem de importação). Esta tabela registra o que cada
    relatório de fato cobriu — serve para o suporte conferir por que uma nota não
    apareceu e impede que uma repetição de envio conte a mesma nota duas vezes.
    Uma nota entra uma única vez por conta e por tipo de relatório.
    """

    __tablename__ = "alertas_notas"
    id = Column(Integer, primary_key=True, index=True)
    usuario_id = Column(Integer, ForeignKey("usuarios.id"), index=True, nullable=False)
    nota_id = Column(Integer, ForeignKey("notas_fiscais.id"), index=True, nullable=False)
    tipo = Column(String, nullable=False)  # diario | semanal | mensal
    criado_em = Column(String)  # ISO 8601 UTC do envio confirmado
    __table_args__ = (
        UniqueConstraint(
            "usuario_id", "nota_id", "tipo", name="uq_alerta_nota_usuario_tipo"
        ),
    )


class ConfiguracaoSistema(Base):
    """Configuração GLOBAL do sistema no formato chave/valor (ex.: token do WhatsApp).

    Fica separada de `ConfiguracaoAlerta` porque vale para todas as contas: o token da API
    é um só e quem o cadastra é o administrador (rota /api/admin/configuracoes, que exige
    `is_admin` + 2FA). A leitura para o cliente devolve apenas se está configurado —
    nunca o valor (mascarado quando o próprio admin consulta).
    """
    __tablename__ = "configuracoes_sistema"
    id = Column(Integer, primary_key=True, index=True)
    chave = Column(String, unique=True, index=True, nullable=False)
    valor = Column(String)
    atualizado_em = Column(String)  # ISO 8601 da última alteração
    atualizado_por = Column(String)  # e-mail do administrador que gravou


class MarcaIA(Base):
    """Memória de cache das respostas da IA de extração de marca.

    Cada descrição é gravada NORMALIZADA (maiúsculas, sem acento e sem o número de
    controle "- 1"/"- 2") em `descricao_original`; é essa chave que o recálculo consulta
    antes de gastar uma chamada da DeepSeek. `marca_extraida` guarda a marca devolvida
    ou o sentinela ``SEM MARCA`` quando a IA concluiu que não há marca comercial.
    `data_consulta` é o ISO 8601 (UTC) da última consulta que gerou a linha.
    """

    __tablename__ = "marcas_ia"
    id = Column(Integer, primary_key=True, index=True)
    descricao_original = Column(String, unique=True, index=True, nullable=False)
    marca_extraida = Column(String)
    data_consulta = Column(String)


class MarcaMaster(Base):
    """Catálogo mestre de marcas comerciais usado pela cascata de extração.

    A tabela é populada por `popular_marcas_master.py` com as marcas conhecidas do
    varejo (alimentos, bebidas, embalagens e limpeza). A cascata de
    `ia_consenso.obter_marca_inteligente` consulta esta tabela por token antes de
    gastar uma chamada da DeepSeek: se a descrição cita uma marca do catálogo, ela é
    gravada em `marcas_ia` e reaproveitada nas próximas rodadas.

    `nome_marca` é único (a mesma marca não se repete entre categorias) e guarda o
    nome como exibido no catálogo; a comparação com a descrição é feita sem acento,
    sem pontuação e sem diferenciar maiúsculas.
    """

    __tablename__ = "marcas_master"
    id = Column(Integer, primary_key=True, index=True)
    nome_marca = Column(String, unique=True, index=True, nullable=False)
    categoria = Column(String, index=True)

