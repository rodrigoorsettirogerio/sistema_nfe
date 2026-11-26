"""Limpa o cache de marcas envenenadas (`marcas_ia`) e reaplica `itens_nota.marca`.

A memória `marcas_ia` é consultada ANTES da IA e do catálogo: uma resposta antiga
gravada por engano ("PLAST PET", "VERDE CAMPO", "REI DO ALHO") volta em toda rodada
e chega aos relatórios como marca do item. O bloqueio de aliases genéricos impediu
gravações NOVAS, mas não apagou o que já estava memorizado — e o cache, por ser
consultado primeiro, anula a trava nova.

Critério de linha inválida (o mesmo da cascata em `backend/ia_consenso.py`):
  1. `_marca_invalida`: termo genérico isolado (OSSO, TRANSP...) ou marca COMPOSTA
     em que TODAS as palavras são genéricas/de produto ("PLAST PET", "VERDE CAMPO");
  2. resposta NÃO verificável: não existe em `marcas_master`/`MARCAS_CONHECIDAS` e
     também não aparece na própria descrição ("REI DO ALHO", "VINHA GRANDE").
Nomes passados em `--manter` (repetível) nunca são removidos.

Fluxo seguro, nesta ordem:
    python limpar_cache_marcas.py                     # 1. relatório (não grava nada)
    python limpar_cache_marcas.py --backup            # 2. backup no Postgres + CSV
    python limpar_cache_marcas.py --backup --aplicar  # 3. backup e apaga os inválidos
    python limpar_cache_marcas.py --recalcular        # 4. reaplica itens_nota.marca

`--backup` copia `marcas_ia` para `marcas_ia_backup_AAAAMMDD` no MESMO schema
(`CREATE TABLE AS SELECT`: retrato sem PK/índices) e exporta `backups/*.csv`, com
`itens_nota(id, marca)` junto para poder desfazer um recálculo. `--aplicar` aborta se
não existir backup do dia com a MESMA contagem de linhas da tabela — DELETE nunca
acontece sem rede de segurança.
"""

import argparse
import csv
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, inspect, text

from backend import ia_consenso
from backend.database import SessionLocal
from backend.models import ItemNota, MarcaIA, MarcaMaster

RAIZ = Path(__file__).resolve().parent
PASTA_BACKUP = RAIZ / "backups"
PREFIXO_TABELA_BACKUP = "marcas_ia_backup_"


def _catalogo(sessao) -> frozenset:
    """Chaves canônicas das marcas do catálogo mestre e de `MARCAS_CONHECIDAS`."""
    chaves = {
        ia_consenso._chave_marca(nome)
        for nome in ia_consenso.MARCAS_CONHECIDAS.values()
    }
    chaves.update(
        ia_consenso._chave_marca(nome)
        for (nome,) in sessao.query(MarcaMaster.nome_marca).all()
    )
    return frozenset(chaves)


def _motivo_invalida(registro, catalogo) -> str:
    """Motivo pelo qual a linha é lixo (``""`` quando ela deve ser mantida)."""
    marca = " ".join(str(registro.marca_extraida or "").split())
    if not marca or ia_consenso.eh_sem_marca(marca):
        return ""
    if ia_consenso._marca_invalida(marca):
        return "termo generico"
    if ia_consenso._chave_marca(marca) in catalogo:
        return ""
    if ia_consenso._marca_na_descricao(marca, registro.descricao_original):
        return ""
    return "nao verificavel"


def _selecionar(sessao, manter=()) -> list:
    """Pares ``(registro, motivo)`` das linhas inválidas, fora as de `manter`."""
    catalogo = _catalogo(sessao)
    preservar = {ia_consenso._chave_marca(nome) for nome in manter or ()}
    selecao = []
    for registro in sessao.query(MarcaIA).all():
        if ia_consenso._chave_marca(registro.marca_extraida) in preservar:
            continue
        motivo = _motivo_invalida(registro, catalogo)
        if motivo:
            selecao.append((registro, motivo))
    return selecao


def _relatorio(sessao, selecao) -> None:
    """Resumo do cache e das linhas que seriam removidas (só leitura)."""
    linhas = sessao.query(MarcaIA).all()
    sem_marca = sum(
        1 for registro in linhas if ia_consenso.eh_sem_marca(registro.marca_extraida)
    )
    por_motivo = Counter(motivo for _, motivo in selecao)
    contagem = Counter(
        (registro.marca_extraida or "").strip() for registro, _ in selecao
    )
    print(
        f"marcas_ia: {len(linhas)} linha(s) | {sem_marca} 'SEM MARCA' | "
        f"{len(linhas) - sem_marca} com marca"
    )
    print(
        f"inválidas a remover: {len(selecao)} "
        f"({por_motivo.get('termo generico', 0)} termo genérico, "
        f"{por_motivo.get('nao verificavel', 0)} não verificável)"
    )
    for marca, quantidade in sorted(contagem.items(), key=lambda par: (-par[1], par[0])):
        exemplo = next(
            (
                registro.descricao_original or ""
                for registro, _ in selecao
                if (registro.marca_extraida or "").strip() == marca
            ),
            "",
        )
        motivo = next(
            motivo
            for registro, motivo in selecao
            if (registro.marca_extraida or "").strip() == marca
        )
        print(f"  {marca!r:<22} {quantidade:>3} | {motivo:<16} | ex.: {exemplo[:60]}")


def _nome_tabela_backup(motor) -> str:
    """Nome livre no schema: ``marcas_ia_backup_AAAAMMDD`` (``_2``, ``_3`` se preciso)."""
    base = f"{PREFIXO_TABELA_BACKUP}{datetime.now():%Y%m%d}"
    inspetor = inspect(motor)
    nome, sufixo = base, 1
    while inspetor.has_table(nome):
        sufixo += 1
        nome = f"{base}_{sufixo}"
    return nome


def _exportar_csv(caminho: Path, cabecalho, linhas) -> int:
    """Grava o CSV (UTF-8) e devolve quantas linhas foram exportadas."""
    PASTA_BACKUP.mkdir(parents=True, exist_ok=True)
    with caminho.open("w", encoding="utf-8", newline="") as arquivo:
        escritor = csv.writer(arquivo)
        escritor.writerow(cabecalho)
        escritor.writerows(linhas)
    return len(linhas)


def _backup(sessao) -> str:
    """Copia `marcas_ia` para uma tabela no mesmo schema e exporta os CSVs."""
    motor = sessao.get_bind()
    nome = _nome_tabela_backup(motor)
    sessao.execute(text(f'CREATE TABLE "{nome}" AS SELECT * FROM marcas_ia'))
    sessao.commit()
    origem = sessao.query(func.count(MarcaIA.id)).scalar() or 0
    copia = sessao.execute(text(f'SELECT count(*) FROM "{nome}"')).scalar() or 0
    if copia != origem:
        raise SystemExit(
            f"❌ Backup inconsistente: marcas_ia tem {origem} linha(s) e {nome} "
            f"ficou com {copia}. Nenhum DELETE será feito."
        )
    momento = f"{datetime.now():%Y%m%d_%H%M%S}"
    marcas_csv = PASTA_BACKUP / f"marcas_ia_{momento}.csv"
    exportadas = _exportar_csv(
        marcas_csv,
        ["id", "descricao_original", "marca_extraida", "data_consulta"],
        sessao.query(
            MarcaIA.id,
            MarcaIA.descricao_original,
            MarcaIA.marca_extraida,
            MarcaIA.data_consulta,
        ).all(),
    )
    itens_csv = PASTA_BACKUP / f"itens_nota_marca_{momento}.csv"
    itens = _exportar_csv(
        itens_csv,
        ["id", "marca"],
        sessao.query(ItemNota.id, ItemNota.marca).all(),
    )
    print(
        f"✅ Backup: tabela {nome} ({copia} linha(s), igual a marcas_ia) | "
        f"{exportadas} linha(s) em {marcas_csv.name} | "
        f"{itens} item(ns) em {itens_csv.name}"
    )
    return nome


def _garantir_backup(sessao) -> str:
    """Devolve o backup de hoje com a contagem de `marcas_ia`; aborta se não houver."""
    atual = sessao.query(func.count(MarcaIA.id)).scalar() or 0
    hoje = datetime.now().strftime("%Y%m%d")
    inspetor = inspect(sessao.get_bind())
    candidatos = sorted(
        (
            nome
            for nome in inspetor.get_table_names()
            if nome.startswith(f"{PREFIXO_TABELA_BACKUP}{hoje}")
        ),
        reverse=True,
    )
    for nome in candidatos:
        copia = sessao.execute(text(f'SELECT count(*) FROM "{nome}"')).scalar() or 0
        if copia == atual:
            return nome
    raise SystemExit(
        f"❌ Nenhum backup de hoje ({PREFIXO_TABELA_BACKUP}{hoje}*) tem {atual} "
        "linha(s), igual a marcas_ia. Rode `python limpar_cache_marcas.py --backup` "
        "antes de qualquer DELETE."
    )


def _aplicar(sessao, selecao) -> int:
    """Apaga as linhas selecionadas (exige backup do dia com a contagem atual)."""
    backup = _garantir_backup(sessao)
    ids = [registro.id for registro, _ in selecao]
    antes = sessao.query(func.count(MarcaIA.id)).scalar() or 0
    apagadas = 0
    for inicio in range(0, len(ids), 500):
        apagadas += (
            sessao.query(MarcaIA)
            .filter(MarcaIA.id.in_(ids[inicio : inicio + 500]))
            .delete(synchronize_session=False)
        )
    sessao.commit()
    depois = sessao.query(func.count(MarcaIA.id)).scalar() or 0
    print(
        f"🗑️  {apagadas} linha(s) removida(s) (backup preservado em {backup}) | "
        f"marcas_ia: {antes} → {depois}"
    )
    return apagadas


def _recalcular(sessao, usar_ia: bool) -> None:
    """Reaplica `itens_nota.marca` com a memória já limpa (`recalcular_marcas`)."""
    from backend.marcas import recalcular_marcas  # import tardio: evita ciclo

    com_marca = lambda: (  # noqa: E731 - leitura curta usada duas vezes
        sessao.query(func.count(ItemNota.id))
        .filter(ItemNota.marca.isnot(None), ItemNota.marca != "")
        .scalar()
        or 0
    )
    antes = com_marca()
    resumo = recalcular_marcas(sessao, usar_ia=usar_ia)
    print(
        f"Itens: {resumo['itens']} | com marca: {antes} → {com_marca()} | "
        f"atualizados: {resumo['atualizados']} | cache: {resumo['cache_hits']} | "
        f"IA: {resumo['consultas_ia']} | sem marca: {resumo['sem_marca']}"
    )
    if resumo.get("erro_ia"):
        print(f"AVISO da IA: {resumo['erro_ia']}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Remove de marcas_ia as marcas genéricas/não verificáveis que envenenam "
            "o cache e, opcionalmente, reaplica itens_nota.marca."
        )
    )
    parser.add_argument(
        "--backup",
        action="store_true",
        help="copia marcas_ia para uma tabela no Postgres + CSV (antes de qualquer DELETE)",
    )
    parser.add_argument(
        "--aplicar",
        action="store_true",
        help="APAGA as linhas inválidas (exige backup do dia com a contagem atual)",
    )
    parser.add_argument(
        "--manter",
        action="append",
        default=[],
        metavar="MARCA",
        help="marca que nunca deve ser removida (pode repetir a opção)",
    )
    parser.add_argument(
        "--recalcular",
        action="store_true",
        help="reexecuta recalcular_marcas (itens_nota.marca) depois da limpeza",
    )
    parser.add_argument(
        "--usar-ia",
        action="store_true",
        help="no recálculo, consulta a DeepSeek para as descrições inéditas",
    )
    args = parser.parse_args(argv)

    sessao = SessionLocal()
    try:
        selecao = _selecionar(sessao, args.manter)
        _relatorio(sessao, selecao)
        if not (args.backup or args.aplicar or args.recalcular):
            print("\n(dry-run: nada foi gravado; use --backup e depois --aplicar)")
            return 0
        if args.backup:
            _backup(sessao)
        if args.aplicar:
            if not selecao:
                print("\nNada a remover.")
            else:
                _aplicar(sessao, selecao)
                restantes = _selecionar(sessao, args.manter)
                print(f"Conferência: {len(restantes)} linha(s) inválida(s) restante(s).")
        if args.recalcular:
            _recalcular(sessao, args.usar_ia)
        return 0
    finally:
        sessao.close()


if __name__ == "__main__":
    sys.exit(main())
