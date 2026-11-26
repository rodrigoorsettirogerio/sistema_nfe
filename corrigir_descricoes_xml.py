"""Corrige descrições já gravadas usando o XML local da NF-e (backfill único).

POR QUE ESTE SCRIPT EXISTE
--------------------------
O `import_xmls.py` grava a descrição do XML. Depois, o de-para
(`mapeamento_fornecedor_produtos`) passou a reaplicar a descrição canônica em
todo o histórico: itens de um mesmo fornecedor+cProd que no XML têm textos
diferentes (por exemplo, o número de controle "- 2" x "- 4") ficaram com uma
descrição só. A sincronização Omie corrige as notas que ela mesma importa, mas
não volta nas notas antigas já gravadas.

Este backfill relê o XML local (inclusive em subpastas e notas sem
`caminho_xml`, localizadas pela chave de acesso) e troca
`itens_nota.descricao_produto` pelo `xProd` do fornecedor quando o texto é
diferente. É o mesmo pareamento da sincronização (`backend/omie_sync.py`):
EAN, cProd do fornecedor e, por último, posição. Item bonificado é ignorado
(não existe em `itens_nota`) e item já corrigido por XML (`descricao_origem`
começando com "XML") não é reescrito.

GRUPO A x GRUPO B
-----------------
O plano aprovado separa as divergências pelo impacto na descrição:

- **Grupo A**: a nota tem pelo menos um item cuja similaridade (texto
  normalizado + ``SequenceMatcher``) entre a descrição atual e o `xProd` fica
  abaixo de ``--limiar`` (padrão 0,85). Todos os itens divergentes da nota
  entram — inclusive os que isoladamente parecem variações pequenas.
- **Grupo B**: todas as divergências da nota ficaram em ``>= --limiar``
  (sufixo, preço embutido, número de controle, plural...). Por padrão o grupo B
  é **intocado**. ``--incluir-b`` libera a correção para ele também.

No grupo A o script também:

- alinha ``itens_nota.c_prod`` ao cProd do XML quando o código do banco é
  diferente (10 itens no plano aprovado). As linhas antigas do de-para viram
  órfãs e NÃO são apagadas nem movidas;
- atualiza ``descricao_padrao`` em UM par de de-para por fornecedor+cProd de A
  (15 pares no plano aprovado), usando o `xProd` da nota mais recente. Pares
  cujo cProd foi alinhado ficam de fora; ``origem="manual"`` nunca é
  sobrescrita.

REGRAS DE SEGURANÇA
-------------------
- Sem `--aplicar` nada é gravado: o padrão é relatório (dry-run).
- `--aplicar` exige backup do DIA com a MESMA contagem de linhas de
  `itens_nota`, `itens_auditados` e `mapeamento_fornecedor_produtos`; sem ele o
  script aborta. `--backup` cria as três tabelas `*_backup_AAAAMMDD` no mesmo
  schema e CSVs em `backups/` com o plano (descrições, c_prod, de-para e
  protegidas).
- Descrição ATUAL aprovada na Auditoria não é trocada se o texto do XML ainda
  não tiver aprovação: isso apagaria uma validação humana. Use
  `--incluir-aprovadas` para corrigir mesmo assim.
- Cada troca vira uma linha `XML_<chave>_<item>` em `itens_auditados`
  (`auditado=True`, motivo `descricao_xml`), fora do prefixo `DESC_`: é
  histórico, não pendência, e os relatórios continuam contando o item.
- A memória de-para acompanha somente os pares de A (exceto
  `origem="manual"`); `--sem-memoria` desliga essa atualização.

DEPOIS DE APLICAR
-----------------
A marca (`itens_nota.marca`) é inferida da descrição; rode
`python backfill_marcas.py` para recalculá-la. O script NÃO mexe em valores de
compra, EAN, CFOP ou vínculo da nota.

USO (na raiz do projeto)
------------------------
    python corrigir_descricoes_xml.py                      # relatório, não grava
    python corrigir_descricoes_xml.py --limite 50          # só as 50 primeiras linhas
    python corrigir_descricoes_xml.py --incluir-b          # inclui o grupo B
    python corrigir_descricoes_xml.py --backup             # tabelas + CSVs, não grava
    python corrigir_descricoes_xml.py --backup --aplicar   # backup e aplica
    python corrigir_descricoes_xml.py --aplicar            # usa backup do dia
"""

import argparse
import csv
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from sqlalchemy import func, inspect, text

from backend.auditoria_relatorio import limpar_texto, similaridade_textos
from backend.aprendizado_produtos import normalizar_cnpj, normalizar_codigo_produto
from backend.database import DATABASE_URL, SessionLocal, init_db, mascarar_url
from backend.models import (
    Fornecedor,
    ItemAuditado,
    ItemNota,
    MapeamentoFornecedorProduto,
    NotaFiscal,
)
from backend.omie_client import chave_do_xml
from backend.omie_sync import (
    PREFIXO_ORIGEM_XML,
    _atualizar_memoria_descricao,
    _item_bonificado,
    _mesmo_texto,
    _parear_itens_gravados,
    _registrar_correcao_descricao,
    _texto,
    itens_do_xml,
)

RAIZ = Path(__file__).resolve().parent
PASTA_BACKUP = RAIZ / "backups"
PASTA_XML = RAIZ / "xmls"
LIMIAR_GRUPO_A = 0.85
# Prefixo gravado em `itens_nota.descricao_origem` e `itens_auditados.fonte`.
# Começa com "XML": a sincronização Omie não sobrescreve o que já veio do XML.
FONTE_DESCRICAO = "XML arquivo local"
PREFIXO_BACKUP_ITENS = "descricoes_xml_itens_nota_backup_"
PREFIXO_BACKUP_AUDITADOS = "descricoes_xml_itens_auditados_backup_"
PREFIXO_BACKUP_MAPEAMENTOS = "descricoes_xml_mapeamentos_backup_"


@dataclass
class Correcao:
    """Uma descrição que será trocada pelo `xProd` do XML."""

    nota: NotaFiscal
    item: ItemNota
    indice: int
    antes: str
    depois: str
    cnpj: str = ""
    c_prod_xml: str = ""
    ean_xml: str = ""
    similaridade: float = 1.0
    grupo: str = ""
    origem_xml: bool = False


@dataclass
class Protegida:
    """Divergência que NÃO será trocada porque a descrição atual foi aprovada."""

    nota: NotaFiscal
    item: ItemNota
    indice: int
    antes: str
    depois: str
    grupo: str = ""


@dataclass
class CorrecaoCProd:
    """`itens_nota.c_prod` que será alinhado ao cProd do XML (grupo A)."""

    nota: NotaFiscal
    item: ItemNota
    indice: int
    antes: str
    depois: str
    cnpj: str = ""


@dataclass
class AtualizacaoDePara:
    """Uma linha de de-para que passará a guardar o xProd canônico."""

    cnpj: str
    c_prod: str
    antes: str
    depois: str
    mapeamento: MapeamentoFornecedorProduto
    nota_id: int
    item_id: int


@dataclass
class Plano:
    """Resultado do dry-run: o que seria corrigido e os contadores da varredura."""

    correcoes: List[Correcao] = field(default_factory=list)
    grupo_b: List[Correcao] = field(default_factory=list)
    protegidas: List[Protegida] = field(default_factory=list)
    correcoes_c_prod: List[CorrecaoCProd] = field(default_factory=list)
    atualizacoes_de_para: List[AtualizacaoDePara] = field(default_factory=list)
    notas_grupo_a: Set[int] = field(default_factory=set)
    notas_grupo_b: Set[int] = field(default_factory=set)
    resumo: Dict[str, int] = field(default_factory=dict)


def _resumo_zerado() -> Dict[str, int]:
    return {
        "notas": 0,
        "sem_arquivo": 0,
        "erro_leitura": 0,
        "xml_invalido": 0,
        "nota_sem_itens": 0,
        "xml_itens": 0,
        "bonificados": 0,
        "pares": 0,
        "db_sem_par": 0,
        "xml_sem_par": 0,
        "conferidas": 0,
        "divergentes": 0,
        "ja_origem_xml": 0,
        "xml_sem_descricao": 0,
        "protegidas": 0,
        "grupo_a_itens": 0,
        "grupo_a_notas": 0,
        "grupo_b_itens": 0,
        "grupo_b_notas": 0,
        "a_corrigir": 0,
        "notas_com_correcao": 0,
        "c_prod": 0,
        "de_para": 0,
        "memoria": 0,
    }


def _resolver_caminho(caminho: str, raiz: Path = RAIZ) -> Path:
    """Caminho do XML: relativo à raiz do projeto (como o importador grava)."""
    arquivo = Path(str(caminho or "").strip())
    if not arquivo.is_absolute():
        arquivo = Path(raiz) / arquivo
    return arquivo


def _ler_xml(caminho: Path) -> str:
    """Lê o XML tolerando UTF-8 (com/sem BOM) e arquivos antigos em Latin-1."""
    dados = caminho.read_bytes()
    for codificacao in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return dados.decode(codificacao)
        except UnicodeDecodeError:
            continue
    return dados.decode("utf-8", errors="ignore")


def _indexar_xmls(pasta: Optional[Path]) -> Dict[str, Tuple[Path, List[dict]]]:
    """Indexa (recursivamente) os XMLs válidos pela chave de acesso.

    A pasta de XML pode ter subpastas (importação por período, ACBr etc.) e
    algumas notas têm itens no banco mas `caminho_xml` vazio. O índice permite
    encontrá-las pela chave sem exigir que a coluna tenha sido preenchida.
    Arquivos sem `nfeProc`/itens ou sem chave ficam de fora.
    """
    indice: Dict[str, Tuple[Path, List[dict]]] = {}
    if pasta is None:
        return indice
    pasta = Path(pasta)
    if not pasta.is_dir():
        return indice
    for arquivo in sorted(pasta.rglob("*.xml")):
        try:
            texto = _ler_xml(arquivo)
            chave = chave_do_xml(texto)
            itens = itens_do_xml(texto)
        except (OSError, ValueError, TypeError):
            continue
        if not chave or not itens:
            continue
        anterior = indice.get(chave)
        if anterior is None:
            indice[chave] = (arquivo, itens)
            continue
        # Duplicidade: fica o arquivo modificado mais recentemente.
        try:
            if arquivo.stat().st_mtime > anterior[0].stat().st_mtime:
                indice[chave] = (arquivo, itens)
        except OSError:
            continue
    return indice


def _chaves_aprovadas(db) -> List[str]:
    """Chaves `DESC_...` já aprovadas na Auditoria (auditado=True)."""
    registros = (
        db.query(ItemAuditado.chave_identificacao)
        .filter(
            ItemAuditado.chave_identificacao.like("DESC_%"),
            ItemAuditado.auditado == True,  # noqa: E712 - coluna booleana
        )
        .all()
    )
    return [str(linha[0] or "") for linha in registros]


def _descricao_aprovada(chaves: List[str], descricao: str) -> bool:
    """True quando a descrição aparece no sufixo de alguma chave `DESC_` aprovada.

    A chave da Auditoria é `DESC_<grupo>_<texto normalizado>` e o grupo pode ter
    underscores; por isso a comparação é pelo sufixo `_<texto>`, a mesma regra de
    `main._aprovar_equivalentes_por_descricao`.
    """
    alvo = limpar_texto(descricao)
    if not alvo:
        return False
    sufixo = "_" + alvo
    return any(chave.endswith(sufixo) for chave in chaves)


def _mapa_aprendizado(
    db,
) -> Dict[Tuple[str, str], MapeamentoFornecedorProduto]:
    """Mesma chave usada por `backend.aprendizado_produtos`."""
    return {
        (
            normalizar_cnpj(mapeamento.cnpj_fornecedor),
            normalizar_codigo_produto(mapeamento.c_prod),
        ): mapeamento
        for mapeamento in db.query(MapeamentoFornecedorProduto).all()
    }


def _memoria_pode_atualizar(mapeamento, descricao: str) -> bool:
    """A mesma regra de `omie_sync._atualizar_memoria_descricao`.

    Mapeamento manual é decisão humana e nunca é sobrescrito; mapeamento cuja
    descrição já é a nova não precisa de UPDATE.
    """
    if mapeamento is None or not _texto(descricao):
        return False
    if str(getattr(mapeamento, "origem", "") or "").strip().lower() == "manual":
        return False
    return not _mesmo_texto(
        getattr(mapeamento, "descricao_padrao", ""), descricao
    )


def planejar(
    db,
    *,
    raiz: Path = RAIZ,
    pasta: Optional[Path] = None,
    limiar: float = LIMIAR_GRUPO_A,
    incluir_b: bool = False,
    incluir_aprovadas: bool = False,
    atualizar_memoria: bool = True,
    alinhar_cprod: bool = True,
) -> Plano:
    """Varre as notas com XML local e monta o plano, sem gravar nada."""
    plano = Plano(resumo=_resumo_zerado())
    resumo = plano.resumo
    aprovadas = _chaves_aprovadas(db)
    mapa_aprendizado = _mapa_aprendizado(db) if atualizar_memoria else {}
    fornecedores: Dict[int, Optional[Fornecedor]] = {}
    indice_xml = _indexar_xmls(pasta)

    # Notas com caminho preenchido OU cuja chave existe na pasta de XML. Isso
    # inclui as notas recentes em que `caminho_xml` ainda está vazio.
    candidatas: List[NotaFiscal] = []
    for nota in db.query(NotaFiscal).order_by(NotaFiscal.id).all():
        caminho = _texto(nota.caminho_xml)
        chave = _texto(nota.chave_acesso)
        if caminho or (chave and chave in indice_xml):
            candidatas.append(nota)

    # Uma consulta para todos os itens (o banco de produção fica na nuvem: uma
    # consulta por nota levava minutos). A ordem por id é a mesma da leitura
    # individual que existia antes.
    itens_por_nota: Dict[int, List[ItemNota]] = {}
    if candidatas:
        for item in (
            db.query(ItemNota)
            .filter(ItemNota.nota_id.in_([nota.id for nota in candidatas]))
            .order_by(ItemNota.nota_id, ItemNota.id)
            .all()
        ):
            itens_por_nota.setdefault(item.nota_id, []).append(item)

    # Primeiro coleta TODAS as divergências (inclusive as que a proteção vai
    # bloquear): o grupo A/B é uma decisão da NOTA, não do item isolado.
    divergencias: List[Correcao] = []
    for nota in candidatas:
        resumo["notas"] += 1
        itens_xml: Optional[List[dict]] = None
        arquivo: Optional[Path] = None
        arquivo_existe = False
        falha_leitura = False
        caminho = _texto(nota.caminho_xml)
        if caminho:
            arquivo = _resolver_caminho(caminho, raiz)
            arquivo_existe = arquivo.is_file()
            if arquivo_existe:
                try:
                    itens_xml = itens_do_xml(_ler_xml(arquivo))
                except OSError:
                    falha_leitura = True

        # Fallback pela chave: cobre `caminho_xml` vazio, arquivo movido e
        # pasta com subdiretórios. O índice guarda o XML já parseado.
        cache = indice_xml.get(_texto(nota.chave_acesso))
        if cache is not None:
            arquivo, itens_xml = cache

        if not itens_xml:
            if falha_leitura:
                resumo["erro_leitura"] += 1
            elif arquivo_existe:
                resumo["xml_invalido"] += 1
            else:
                resumo["sem_arquivo"] += 1
            continue

        # `_parear_itens_gravados` já ignora bonificados, mas mantém o índice
        # original do XML (o mesmo que a sincronização usa na chave do marcador).
        nao_bonificados = [
            bruto for bruto in itens_xml if not _item_bonificado(bruto)
        ]
        resumo["bonificados"] += len(itens_xml) - len(nao_bonificados)
        resumo["xml_itens"] += len(nao_bonificados)

        itens_db = itens_por_nota.get(nota.id, [])
        if not itens_db:
            resumo["nota_sem_itens"] += 1
            continue

        pares = _parear_itens_gravados(itens_xml, itens_db)
        resumo["pares"] += len(pares)
        resumo["db_sem_par"] += max(0, len(itens_db) - len(pares))
        resumo["xml_sem_par"] += max(0, len(nao_bonificados) - len(pares))

        for indice, bruto, item in pares:
            nova = _texto(bruto.get("descricao"))
            atual = _texto(item.descricao_produto)
            if not nova:
                resumo["xml_sem_descricao"] += 1
                continue
            if _mesmo_texto(nova, atual):
                resumo["conferidas"] += 1
                continue

            resumo["divergentes"] += 1
            fornecedor = None
            if item.fornecedor_id is not None:
                if item.fornecedor_id not in fornecedores:
                    fornecedores[item.fornecedor_id] = db.get(
                        Fornecedor, item.fornecedor_id
                    )
                fornecedor = fornecedores[item.fornecedor_id]
            cnpj = _texto(getattr(fornecedor, "cnpj", ""))
            divergencias.append(
                Correcao(
                    nota=nota,
                    item=item,
                    indice=indice,
                    antes=atual,
                    depois=nova,
                    cnpj=cnpj,
                    c_prod_xml=_texto(bruto.get("c_prod")),
                    ean_xml=_texto(bruto.get("ean")),
                    similaridade=similaridade_textos(
                        limpar_texto(atual), limpar_texto(nova)
                    ),
                    origem_xml=_texto(
                        getattr(item, "descricao_origem", "")
                    ).startswith(PREFIXO_ORIGEM_XML),
                )
            )

    por_nota: Dict[int, List[Correcao]] = defaultdict(list)
    for correcao in divergencias:
        por_nota[correcao.nota.id].append(correcao)
    plano.notas_grupo_a = {
        nota_id
        for nota_id, itens in por_nota.items()
        if min(item.similaridade for item in itens) < limiar
    }
    plano.notas_grupo_b = set(por_nota) - plano.notas_grupo_a
    resumo["grupo_a_itens"] = sum(
        len(por_nota[nota_id]) for nota_id in plano.notas_grupo_a
    )
    resumo["grupo_b_itens"] = sum(
        len(por_nota[nota_id]) for nota_id in plano.notas_grupo_b
    )
    resumo["grupo_a_notas"] = len(plano.notas_grupo_a)
    resumo["grupo_b_notas"] = len(plano.notas_grupo_b)

    for correcao in divergencias:
        correcao.grupo = (
            "A" if correcao.nota.id in plano.notas_grupo_a else "B"
        )
        if correcao.grupo == "B":
            plano.grupo_b.append(correcao)

        if correcao.origem_xml:
            resumo["ja_origem_xml"] += 1
            continue
        if (
            not incluir_aprovadas
            and _descricao_aprovada(aprovadas, correcao.antes)
            and not _descricao_aprovada(aprovadas, correcao.depois)
        ):
            resumo["protegidas"] += 1
            plano.protegidas.append(
                Protegida(
                    correcao.nota,
                    correcao.item,
                    correcao.indice,
                    correcao.antes,
                    correcao.depois,
                    correcao.grupo,
                )
            )
            continue
        if correcao.grupo == "B" and not incluir_b:
            continue
        plano.correcoes.append(correcao)

    resumo["a_corrigir"] = len(plano.correcoes)
    resumo["notas_com_correcao"] = len(
        {correcao.nota.id for correcao in plano.correcoes}
    )

    # 10 itens do grupo A trocam o cProd interno pelo cProd do fornecedor.
    # A chave do de-para NÃO é movida: a linha antiga fica órfã e a nova (se já
    # existe) é preservada.
    cprod_por_grupo: Set[Tuple[str, str]] = set()
    if alinhar_cprod:
        vistos: Set[int] = set()
        for correcao in plano.correcoes:
            antes = normalizar_codigo_produto(correcao.item.c_prod)
            depois = normalizar_codigo_produto(correcao.c_prod_xml)
            if not antes or not depois or antes == depois:
                continue
            if correcao.item.id in vistos:
                continue
            vistos.add(correcao.item.id)
            cprod_por_grupo.add(
                (normalizar_cnpj(correcao.cnpj), depois)
            )
            plano.correcoes_c_prod.append(
                CorrecaoCProd(
                    nota=correcao.nota,
                    item=correcao.item,
                    indice=correcao.indice,
                    antes=antes,
                    depois=depois,
                    cnpj=correcao.cnpj,
                )
            )
    resumo["c_prod"] = len(plano.correcoes_c_prod)

    # 15 pares de de-para do grupo A (fora os que tiveram cProd alinhado).
    # O texto escolhido é o da nota mais recente do par.
    if atualizar_memoria:
        grupos: Dict[Tuple[str, str], List[Correcao]] = defaultdict(list)
        for correcao in plano.correcoes:
            chave = (
                normalizar_cnpj(correcao.cnpj),
                normalizar_codigo_produto(correcao.c_prod_xml),
            )
            if chave in cprod_por_grupo:
                continue
            grupos[chave].append(correcao)
        for (_, c_prod), itens in grupos.items():
            mapeamento = mapa_aprendizado.get(
                (
                    normalizar_cnpj(itens[0].cnpj),
                    normalizar_codigo_produto(c_prod),
                )
            )
            mais_recente = max(
                itens,
                key=lambda c: (
                    c.nota.data_emissao or date.min,
                    c.nota.id,
                    c.item.id,
                ),
            )
            if not _memoria_pode_atualizar(mapeamento, mais_recente.depois):
                continue
            plano.atualizacoes_de_para.append(
                AtualizacaoDePara(
                    cnpj=mais_recente.cnpj,
                    c_prod=c_prod,
                    antes=_texto(
                        getattr(mapeamento, "descricao_padrao", "")
                    ),
                    depois=mais_recente.depois,
                    mapeamento=mapeamento,
                    nota_id=mais_recente.nota.id,
                    item_id=mais_recente.item.id,
                )
            )
    resumo["de_para"] = len(plano.atualizacoes_de_para)
    resumo["memoria"] = len(plano.atualizacoes_de_para)
    return plano


def _definicoes_backup():
    """Tabelas que precisam de cópia antes de qualquer UPDATE."""
    return [
        ("itens", PREFIXO_BACKUP_ITENS, "itens_nota", ItemNota),
        (
            "auditados",
            PREFIXO_BACKUP_AUDITADOS,
            "itens_auditados",
            ItemAuditado,
        ),
        (
            "mapeamentos",
            PREFIXO_BACKUP_MAPEAMENTOS,
            "mapeamento_fornecedor_produtos",
            MapeamentoFornecedorProduto,
        ),
    ]


def _nome_tabela_backup(motor, prefixo: str, data: str) -> str:
    """`<prefixo><AAAAMMDD>`, com sufixo numérico se já existir."""
    base = f"{prefixo}{data}"
    inspetor = inspect(motor)
    nome = base
    sufixo = 1
    while inspetor.has_table(nome):
        sufixo += 1
        nome = f"{base}_{sufixo}"
    return nome


def _tabelas_do_dia(motor, prefixo: str, data: str) -> List[str]:
    base = f"{prefixo}{data}"
    nomes = [
        nome
        for nome in inspect(motor).get_table_names()
        if nome == base or nome.startswith(base + "_")
    ]

    def ordem(nome: str) -> int:
        final = nome.rsplit("_", 1)[-1]
        return int(final) if final.isdigit() else 1

    return sorted(nomes, key=ordem)


def _contar_tabela(db, nome: str) -> int:
    consulta = text(f'SELECT count(*) FROM "{nome}"')
    return int(db.execute(consulta).scalar() or 0)


def _nome_csv(prefixo: str, pasta: Path) -> Path:
    marca = datetime.now().strftime("%Y%m%d_%H%M%S")
    caminho = pasta / f"{prefixo}_{marca}.csv"
    sufixo = 1
    while caminho.exists():
        sufixo += 1
        caminho = pasta / f"{prefixo}_{marca}_{sufixo}.csv"
    return caminho


def _exportar_csv(caminho: Path, cabecalho: List[str], linhas: List[list]) -> int:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    with caminho.open("w", encoding="utf-8", newline="") as arquivo:
        escritor = csv.writer(arquivo)
        escritor.writerow(cabecalho)
        escritor.writerows(linhas)
    return len(linhas)


def _backup(db, plano: Plano, pasta: Path = PASTA_BACKUP) -> Dict[str, object]:
    """Copia as três tabelas para `*_backup_AAAAMMDD` e exporta o plano em CSV."""
    motor = db.get_bind()
    data = datetime.now().strftime("%Y%m%d")
    tabelas: Dict[str, Dict[str, object]] = {}
    try:
        for chave, prefixo, origem, modelo in _definicoes_backup():
            nome = _nome_tabela_backup(motor, prefixo, data)
            db.execute(
                text(f'CREATE TABLE "{nome}" AS SELECT * FROM "{origem}"')
            )
            linhas = int(db.query(func.count(modelo.id)).scalar() or 0)
            copiadas = _contar_tabela(db, nome)
            if copiadas != linhas:
                db.rollback()
                raise SystemExit(
                    f"ERRO: backup {nome} tem {copiadas} linha(s), "
                    f"esperado {linhas}. Nada foi alterado."
                )
            tabelas[chave] = {"tabela": nome, "linhas": copiadas}
        db.commit()
    except Exception:
        db.rollback()
        raise

    pasta.mkdir(parents=True, exist_ok=True)
    cabecalho = [
        "nota_id",
        "chave_acesso",
        "item_id",
        "indice",
        "c_prod_banco",
        "c_prod_xml",
        "ean_banco",
        "ean_xml",
        "descricao_antes",
        "descricao_depois",
        "grupo",
        "similaridade",
        "fornecedor_id",
        "fornecedor_cnpj",
    ]
    linhas_correcao = [
        [
            correcao.nota.id,
            correcao.nota.chave_acesso or "",
            correcao.item.id,
            correcao.indice,
            correcao.item.c_prod or "",
            correcao.c_prod_xml,
            correcao.item.ean or "",
            correcao.ean_xml,
            correcao.antes,
            correcao.depois,
            correcao.grupo,
            f"{correcao.similaridade:.4f}",
            correcao.item.fornecedor_id or "",
            correcao.cnpj,
        ]
        for correcao in plano.correcoes
    ]
    caminho_csv = _nome_csv("descricoes_xml", pasta)
    _exportar_csv(caminho_csv, cabecalho, linhas_correcao)

    caminho_cprod = _nome_csv("descricoes_xml_cprod", pasta)
    _exportar_csv(
        caminho_cprod,
        [
            "nota_id",
            "chave_acesso",
            "item_id",
            "indice",
            "c_prod_antes",
            "c_prod_depois",
            "fornecedor_cnpj",
        ],
        [
            [
                correcao.nota.id,
                correcao.nota.chave_acesso or "",
                correcao.item.id,
                correcao.indice,
                correcao.antes,
                correcao.depois,
                correcao.cnpj,
            ]
            for correcao in plano.correcoes_c_prod
        ],
    )

    caminho_de_para = _nome_csv("descricoes_xml_de_para", pasta)
    _exportar_csv(
        caminho_de_para,
        [
            "mapeamento_id",
            "fornecedor_cnpj",
            "c_prod",
            "descricao_antes",
            "descricao_depois",
            "nota_id",
            "item_id",
        ],
        [
            [
                atualizacao.mapeamento.id,
                atualizacao.cnpj,
                atualizacao.c_prod,
                atualizacao.antes,
                atualizacao.depois,
                atualizacao.nota_id,
                atualizacao.item_id,
            ]
            for atualizacao in plano.atualizacoes_de_para
        ],
    )

    caminho_protegidas = ""
    if plano.protegidas:
        caminho_protegidas = str(
            _nome_csv("descricoes_xml_protegidas", pasta)
        )
        linhas_protegidas = [
            [
                protegida.nota.id,
                protegida.nota.chave_acesso or "",
                protegida.item.id,
                protegida.indice,
                protegida.item.c_prod or "",
                protegida.item.ean or "",
                protegida.antes,
                protegida.depois,
                "descricao atual aprovada na Auditoria",
            ]
            for protegida in plano.protegidas
        ]
        _exportar_csv(
            Path(caminho_protegidas),
            [
                "nota_id",
                "chave_acesso",
                "item_id",
                "indice",
                "c_prod",
                "ean",
                "descricao_antes",
                "descricao_xml",
                "motivo",
            ],
            linhas_protegidas,
        )

    return {
        "tabelas": tabelas,
        "csv": str(caminho_csv),
        "csv_cprod": str(caminho_cprod),
        "csv_de_para": str(caminho_de_para),
        "csv_protegidas": caminho_protegidas,
    }


def _garantir_backup(db) -> Dict[str, str]:
    """Aborta se não houver backup de HOJE com a contagem atual das tabelas."""
    motor = db.get_bind()
    data = datetime.now().strftime("%Y%m%d")
    encontrados: Dict[str, str] = {}
    faltando: List[str] = []
    for chave, prefixo, origem, modelo in _definicoes_backup():
        atual = int(db.query(func.count(modelo.id)).scalar() or 0)
        candidatos = _tabelas_do_dia(motor, prefixo, data)
        escolhida = None
        for nome in reversed(candidatos):
            if _contar_tabela(db, nome) == atual:
                escolhida = nome
                break
        if escolhida is None:
            faltando.append(f"{origem} ({atual} linha(s))")
        else:
            encontrados[chave] = escolhida
    if faltando:
        raise SystemExit(
            "ERRO: --aplicar exige backup de hoje com a mesma contagem de "
            "linhas.\n"
            "  Faltando: " + ", ".join(faltando) + "\n"
            "  Rode: python corrigir_descricoes_xml.py --backup\n"
            "  Ou:   python corrigir_descricoes_xml.py --backup --aplicar"
        )
    return encontrados


def _limitar(itens: List, limite: int) -> List:
    return itens if limite <= 0 else itens[:limite]


def _imprimir_relatorio(
    plano: Plano, limite: int = 0, incluir_b: bool = False
) -> None:
    r = plano.resumo
    print("Correção das descrições pelo XML local da NF-e")
    print(f"Banco: {mascarar_url(DATABASE_URL)}")
    print(
        f"Notas com XML local: {r['notas']} | sem arquivo: "
        f"{r['sem_arquivo']} | XML inválido: {r['xml_invalido']} | "
        f"erro de leitura: {r['erro_leitura']} | sem itens: "
        f"{r['nota_sem_itens']}"
    )
    print(
        f"Itens do XML (sem bonificados): {r['xml_itens']} | bonificados: "
        f"{r['bonificados']} | pareados: {r['pares']} | sem par no banco: "
        f"{r['db_sem_par']} | sem par no XML: {r['xml_sem_par']}"
    )
    print(
        f"Conferidas: {r['conferidas']} | divergentes: {r['divergentes']} | "
        f"XML sem descrição: {r['xml_sem_descricao']} | já com origem XML: "
        f"{r['ja_origem_xml']}"
    )
    rotulo_b = "incluído" if incluir_b else "intocado"
    print(
        f"Grupo A (limiar): {r['grupo_a_itens']} item(ns) em "
        f"{r['grupo_a_notas']} nota(s) | Grupo B ({rotulo_b}): "
        f"{r['grupo_b_itens']} item(ns) em {r['grupo_b_notas']} nota(s)"
    )
    print(
        f"Protegidas por aprovação: {r['protegidas']} | a corrigir: "
        f"{r['a_corrigir']} em {r['notas_com_correcao']} nota(s) | c_prod: "
        f"{r['c_prod']} | de-para: {r['de_para']} par(es)"
    )

    if plano.protegidas:
        print(
            "\nNÃO SERÃO ALTERADAS (a descrição atual foi aprovada na "
            "Auditoria):"
        )
        for protegida in _limitar(plano.protegidas, limite):
            print(
                f"  nota {protegida.nota.id} item {protegida.indice} | "
                f"cProd {protegida.item.c_prod or '-'} | EAN "
                f"{protegida.item.ean or '-'} | {protegida.antes!r} -> "
                f"{protegida.depois!r}"
            )
        if limite > 0 and len(plano.protegidas) > limite:
            print(
                f"  ... (+{len(plano.protegidas) - limite} não exibida(s); "
                "use --limite 0 para ver todas)"
            )
        print("  Use --incluir-aprovadas para corrigir estas também.")

    if plano.correcoes_c_prod:
        print("\nc_prod A ALINHAR (o de-para antigo fica órfão):")
        for correcao in _limitar(plano.correcoes_c_prod, limite):
            print(
                f"  nota {correcao.nota.id} item {correcao.indice} | "
                f"{correcao.antes} -> {correcao.depois}"
            )
        if limite > 0 and len(plano.correcoes_c_prod) > limite:
            print(
                f"  ... (+{len(plano.correcoes_c_prod) - limite} não "
                "exibido(s); use --limite 0 para ver todos)"
            )

    if plano.atualizacoes_de_para:
        print("\nDE-PARA A ATUALIZAR (xProd da nota mais recente):")
        for atualizacao in _limitar(plano.atualizacoes_de_para, limite):
            print(
                f"  de-para {atualizacao.mapeamento.id} | cProd "
                f"{atualizacao.c_prod} | {atualizacao.antes!r} -> "
                f"{atualizacao.depois!r}"
            )
        if limite > 0 and len(plano.atualizacoes_de_para) > limite:
            print(
                f"  ... (+{len(plano.atualizacoes_de_para) - limite} não "
                "exibido(s); use --limite 0 para ver todos)"
            )

    if plano.correcoes:
        print("\nCORREÇÕES PLANEJADAS (grupo A):")
        for correcao in _limitar(plano.correcoes, limite):
            print(
                f"  nota {correcao.nota.id} item {correcao.indice} | cProd "
                f"{correcao.item.c_prod or '-'} | EAN "
                f"{correcao.item.ean or '-'} | {correcao.antes!r} -> "
                f"{correcao.depois!r}"
            )
        if limite > 0 and len(plano.correcoes) > limite:
            print(
                f"  ... (+{len(plano.correcoes) - limite} correção(ões) não "
                "exibida(s); use --limite 0 para ver todas)"
            )


def aplicar(db, plano: Plano, *, exigir_backup: bool = True) -> Dict[str, int]:
    """Grava o plano em uma única transação.

    `exigir_backup=True` (padrão) confere a existência de um backup do dia com a
    contagem atual antes de qualquer UPDATE. A descrição é comparada com a do
    planejamento: se outro processo mudou o item no meio do caminho, ele é
    ignorado em vez de sobrescrito às cegas.
    """
    if exigir_backup:
        _garantir_backup(db)
    resultado = {"descricoes": {"marcadores": 0}}
    aplicadas = 0
    ignoradas = 0
    c_prod_atualizados = 0
    c_prod_ignorados = 0
    memoria = 0
    try:
        for correcao in plano.correcoes:
            item = correcao.item
            atual = _texto(item.descricao_produto)
            if atual != correcao.antes:
                ignoradas += 1
                continue

            item.descricao_produto = correcao.depois
            if not _texto(getattr(item, "descricao_produto_anterior", "")):
                item.descricao_produto_anterior = correcao.antes or None
            item.descricao_origem = FONTE_DESCRICAO

            marcadores_antes = resultado["descricoes"]["marcadores"]
            _registrar_correcao_descricao(
                db,
                correcao.nota.chave_acesso or "",
                correcao.indice,
                correcao.antes,
                correcao.depois,
                FONTE_DESCRICAO,
                resultado,
            )
            if resultado["descricoes"]["marcadores"] != marcadores_antes:
                # Deixa o INSERT visível para a próxima consulta do mesmo item.
                db.flush()
            aplicadas += 1

        for correcao in plano.correcoes_c_prod:
            item = correcao.item
            if normalizar_codigo_produto(item.c_prod) != correcao.antes:
                c_prod_ignorados += 1
                continue
            item.c_prod = correcao.depois
            c_prod_atualizados += 1

        for atualizacao in plano.atualizacoes_de_para:
            mapeamento = atualizacao.mapeamento
            if not _mesmo_texto(
                getattr(mapeamento, "descricao_padrao", ""),
                atualizacao.antes,
            ):
                # Outro processo mudou o de-para entre o relatório e a gravação.
                continue
            mapa = {
                (
                    normalizar_cnpj(atualizacao.cnpj),
                    normalizar_codigo_produto(atualizacao.c_prod),
                ): mapeamento
            }
            if _atualizar_memoria_descricao(
                mapa,
                atualizacao.cnpj,
                atualizacao.c_prod,
                atualizacao.depois,
            ):
                memoria += 1

        db.commit()
    except BaseException:
        db.rollback()
        raise

    return {
        "corrigidas": aplicadas,
        "ignoradas": ignoradas,
        "marcadores_novos": resultado["descricoes"]["marcadores"],
        "marcadores_atualizados": aplicadas
        - resultado["descricoes"]["marcadores"],
        "c_prod_atualizados": c_prod_atualizados,
        "c_prod_ignorados": c_prod_ignorados,
        "memoria_atualizada": memoria,
    }


def _argumentos(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help=(
            "copia itens_nota, itens_auditados e mapeamento_fornecedor_produtos "
            "para tabelas *_backup_AAAAMMDD e exporta o plano em CSV; não grava "
            "descrições"
        ),
    )
    parser.add_argument(
        "--aplicar",
        action="store_true",
        help="aplica as correções (exige backup do dia; use com --backup)",
    )
    parser.add_argument(
        "--incluir-aprovadas",
        action="store_true",
        help="corrige também quando a descrição atual já foi aprovada na Auditoria",
    )
    parser.add_argument(
        "--incluir-b",
        action="store_true",
        help=(
            "inclui o grupo B (sufixo/preço/variação pequena); por padrão só o "
            "grupo A é corrigido"
        ),
    )
    parser.add_argument(
        "--limiar",
        type=float,
        default=LIMIAR_GRUPO_A,
        help=(
            "similaridade mínima da nota para o grupo A (padrão "
            f"{LIMIAR_GRUPO_A:g})"
        ),
    )
    parser.add_argument(
        "--pasta",
        default=str(PASTA_XML),
        help="pasta dos XMLs (varre subpastas); padrão: xmls/",
    )
    parser.add_argument(
        "--sem-memoria",
        action="store_true",
        help="não atualiza a memória de-para (mapeamento_fornecedor_produtos)",
    )
    parser.add_argument(
        "--sem-cprod",
        action="store_true",
        help="não alinha itens_nota.c_prod ao cProd do XML no grupo A",
    )
    parser.add_argument(
        "--limite",
        type=int,
        default=0,
        help="máximo de linhas por seção no relatório (0 = todas)",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _argumentos(argv)
    init_db()
    db = SessionLocal()
    try:
        plano = planejar(
            db,
            pasta=Path(args.pasta),
            limiar=args.limiar,
            incluir_b=args.incluir_b,
            incluir_aprovadas=args.incluir_aprovadas,
            atualizar_memoria=not args.sem_memoria,
            alinhar_cprod=not args.sem_cprod,
        )
        _imprimir_relatorio(
            plano,
            limite=max(0, args.limite),
            incluir_b=args.incluir_b,
        )

        if (
            not plano.correcoes
            and not plano.correcoes_c_prod
            and not plano.atualizacoes_de_para
        ):
            print("\nNada a corrigir: nenhuma gravação foi feita.")
            return 0

        info_backup = None
        if args.backup:
            info_backup = _backup(db, plano)
            print("\nBackup criado:")
            for dados in info_backup["tabelas"].values():
                print(f"  {dados['tabela']} ({dados['linhas']} linha(s))")
            print(f"  CSV do plano: {info_backup['csv']}")
            print(f"  CSV de c_prod: {info_backup['csv_cprod']}")
            print(f"  CSV de de-para: {info_backup['csv_de_para']}")
            if info_backup.get("csv_protegidas"):
                print(f"  CSV das protegidas: {info_backup['csv_protegidas']}")

        if args.aplicar:
            resumo = aplicar(db, plano)
            print(
                f"\nAplicado: {resumo['corrigidas']} item(ns) corrigido(s) | "
                f"{resumo['marcadores_novos']} trilha(s) nova(s) | "
                f"{resumo['marcadores_atualizados']} atualizada(s) | "
                f"c_prod: {resumo['c_prod_atualizados']} | "
                f"de-para: {resumo['memoria_atualizada']}"
            )
            if resumo["ignoradas"]:
                print(
                    f"AVISO: {resumo['ignoradas']} item(ns) mudaram entre o "
                    "relatório e a gravação e foram ignorados; rode o relatório "
                    "de novo."
                )
            print(
                "AVISO: a marca é inferida da descrição; rode "
                "`python backfill_marcas.py` para recalculá-la."
            )
            return 0

        if args.backup:
            print(
                "\nNada foi alterado. Para aplicar: python "
                "corrigir_descricoes_xml.py --backup --aplicar"
            )
        else:
            print("\nModo relatório: nada foi gravado.")
            print(
                "Para criar o backup: python corrigir_descricoes_xml.py --backup"
            )
            print(
                "Para aplicar:       python corrigir_descricoes_xml.py "
                "--backup --aplicar"
            )
        if plano.protegidas and not args.incluir_aprovadas:
            print(
                f"{len(plano.protegidas)} item(ns) ficaram protegidos por "
                "aprovação; use --incluir-aprovadas para incluí-los."
            )
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

