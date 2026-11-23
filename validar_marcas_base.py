"""Valida em massa as marcas JÁ gravadas em `itens_nota.marca`.

O importador e a heurística local por NCM podem gerar falsos positivos (OSSO, LIXO,
TRANSP, BLOCO, COMUM...). Este script reaproveita a memória `marcas_ia`: cada descrição
distinta que hoje tem marca passa por `obter_marca_inteligente`, que responde pelo cache
quando já foi analisada e só chama a DeepSeek para descrições inéditas. O resultado
validado substitui a marca antiga — `SEM MARCA` vira NULL em `itens_nota.marca`.

Uso (na raiz do projeto):
    python validar_marcas_base.py
    python validar_marcas_base.py --limite 10       # amostra (as demais ficam para depois)
    python validar_marcas_base.py --dry-run         # analisa e mostra o resumo, sem gravar
    python validar_marcas_base.py --revalidar       # ignora o cache e usa a marca atual como candidata
    python validar_marcas_base.py --revalidar --contem "TOALHA INTER"
    # revalida só as descrições que contenham o texto informado

O script mostra no terminal, para cada descrição, o acumulado de consultas à IA e de
respostas vindas do cache. Antes de alterar a base ele cria um backup SQLite ao lado do
banco (`sistema_nfe_pro.db.bak-AAAAMMDD-HHMMSS`); use `--sem-backup` para desativar.
"""

import argparse
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path

from backend import ia_consenso
from backend.database import DB_PATH, SessionLocal, init_db
from backend.marcas import normalizar, remover_numero_controle
from backend.models import ItemNota, MarcaIA


def _chave_descricao(descricao) -> str:
    """Mesma chave de cache da IA: normalizada e sem o número de controle final."""
    return normalizar(remover_numero_controle(descricao)).strip()


def _mesma_marca(antiga, nova) -> bool:
    """Compara marcas sem diferenciar caixa/espaços (TOMMY x Tommy são a mesma)."""
    return str(antiga or "").strip().casefold() == str(nova or "").strip().casefold()


def _carregar_alvos(db) -> dict:
    """Agrupa por descrição normalizada os itens que HOJE têm marca.

    A chave normalizada junta variações como "... - 1" e "... - 2". Só entram itens com
    marca preenchida: a validação corrige/remove o que já existe, não amplia o catálogo.
    """
    linhas = (
        db.query(ItemNota.id, ItemNota.descricao_produto, ItemNota.marca)
        .filter(ItemNota.marca.isnot(None))
        .all()
    )
    grupos = {}
    for item_id, descricao, marca in linhas:
        antiga = str(marca or "").strip()
        if not antiga:
            continue
        chave = _chave_descricao(descricao)
        if not chave:
            continue
        grupo = grupos.get(chave)
        if grupo is None:
            grupo = grupos[chave] = {
                "descricao": str(descricao or "").strip(),
                "ids": [],
                "marcas": Counter(),
            }
        grupo["ids"].append(item_id)
        grupo["marcas"][antiga] += 1
    return grupos


def _filtrar_por_contem(grupos: dict, contem) -> dict:
    """Limita os grupos às descrições que contêm o texto (sem caixa/acentos)."""
    if not contem:
        return grupos
    filtro = normalizar(contem)
    return {
        chave: grupo
        for chave, grupo in grupos.items()
        if filtro in normalizar(grupo["descricao"])
    }


def validar_marcas_base(
    db, limite=None, dry_run=False, progresso=None, revalidar=False, contem=None
) -> dict:
    """Valida as marcas existentes com `obter_marca_inteligente` e grava o resultado.

    `progresso(indice, total, estatisticas, descricao, nova_marca)` é opcional e permite
    ao terminal mostrar o andamento sem acoplar o script à impressão. Uma falha isolada
    da IA mantém a marca antiga daquela descrição; três falhas seguidas interrompem as
    consultas (para não insistir em erro de rede/cota), mas o que já foi respondido é
    aplicado no fim. O cache é persistido a cada resposta, então uma nova execução
    continua de onde parou sem repetir tokens.

    Com `revalidar=True`, a memória dessas descrições é apagada antes das chamadas e a
    marca atual entra como `marca_candidata` no prompt. Isso é usado quando o primeiro
    prompt foi conservador demais e removeu marcas reais; sem a flag, o cache manda e
    nenhum token é gasto. `contem` limita o trabalho às descrições que contenham esse
    texto (sem diferenciar caixa/acentos), útil para corrigir casos pontuais.
    """
    grupos = _filtrar_por_contem(_carregar_alvos(db), contem)
    if not grupos:
        return {
            "descricoes_alvo": 0,
            "descricoes_analisadas": 0,
            "descricoes_com_erro": 0,
            "itens_com_marca": 0,
            "consultas_ia": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "economias_cache": 0,
            "sem_marca_ia": 0,
            "marcas_ia": 0,
            "descricoes_alteradas_sem_marca": 0,
            "descricoes_mantidas": 0,
            "descricoes_alteradas_outra_marca": 0,
            "itens_alterados_sem_marca": 0,
            "itens_mantidos": 0,
            "itens_alterados_outra_marca": 0,
            "itens_alterados": 0,
            "itens_atualizados": 0,
            "alteracoes": [],
            "erros": [],
            "interrompido": False,
            "dry_run": bool(dry_run),
            "cache_total": 0,
        }

    if not ia_consenso.configurada():
        raise ia_consenso.IAnaoconfigurada()

    alvos = sorted(grupos.items(), key=lambda par: par[1]["descricao"].casefold())
    if limite:
        alvos = alvos[: int(limite)]
    total = len(alvos)
    if revalidar:
        # Apaga só a memória das descrições desta execução; as demais continuam valendo.
        chaves = [chave for chave, _ in alvos]
        for inicio in range(0, len(chaves), 500):
            (
                db.query(MarcaIA)
                .filter(
                    MarcaIA.descricao_original.in_(chaves[inicio : inicio + 500])
                )
                .delete(synchronize_session=False)
            )
        db.commit()

    estatisticas = {
        "cache_hits": 0,
        "cache_misses": 0,
        "consultas_ia": 0,
        "sem_marca": 0,
        "marcas_encontradas": 0,
    }
    resultados = {}
    erros = []
    falhas_seguidas = 0
    interrompido = False

    for indice, (chave, grupo) in enumerate(alvos, start=1):
        descricao = grupo["descricao"]
        try:
            resultado = ia_consenso.obter_marca_inteligente(
                descricao,
                db=db,
                usar_ia=True,
                estatisticas=estatisticas,
                marca_candidata=grupo["marcas"].most_common(1)[0][0],
            )
            falhas_seguidas = 0
        except ia_consenso.IAnaoconfigurada as erro:
            erros.append((descricao, str(erro)))
            interrompido = True
            break
        except ia_consenso.ErroIA as erro:
            erros.append((descricao, str(erro)))
            falhas_seguidas += 1
            print(f"ERRO na IA em {descricao!r}: {erro}", flush=True)
            if falhas_seguidas >= 3:
                print(
                    "Interrompendo as consultas após 3 falhas seguidas; "
                    "o que já foi respondido será aplicado.",
                    flush=True,
                )
                interrompido = True
                break
            continue

        if resultado is None:
            erros.append(
                (
                    descricao,
                    "A IA não devolveu resultado para uma descrição inédita.",
                )
            )
            continue
        nova = None if ia_consenso.eh_sem_marca(resultado) else str(resultado).strip()
        resultados[chave] = (grupo, nova or None)
        if callable(progresso):
            progresso(indice, total, dict(estatisticas), descricao, nova or None)



    resumo = {
        "descricoes_alvo": total,
        "descricoes_analisadas": len(resultados),
        "descricoes_com_erro": len(erros),
        "itens_com_marca": sum(sum(grupo["marcas"].values()) for _, grupo in alvos),
        "consultas_ia": estatisticas["consultas_ia"],
        "cache_hits": estatisticas["cache_hits"],
        "cache_misses": estatisticas["cache_misses"],
        "economias_cache": estatisticas["cache_hits"],
        "sem_marca_ia": estatisticas["sem_marca"],
        "marcas_ia": estatisticas["marcas_encontradas"],
        "descricoes_alteradas_sem_marca": 0,
        "descricoes_mantidas": 0,
        "descricoes_alteradas_outra_marca": 0,
        "itens_alterados_sem_marca": 0,
        "itens_mantidos": 0,
        "itens_alterados_outra_marca": 0,
        "itens_alterados": 0,
        "itens_atualizados": 0,
        "alteracoes": [],
        "erros": erros,
        "interrompido": interrompido,
        "dry_run": bool(dry_run),
        "cache_total": 0,
    }

    for chave, (grupo, nova) in resultados.items():
        antigas = list(grupo["marcas"])
        todas_iguais = all(_mesma_marca(antiga, nova) for antiga in antigas)
        if todas_iguais:
            resumo["descricoes_mantidas"] += 1
        elif nova is None:
            resumo["descricoes_alteradas_sem_marca"] += 1
        else:
            resumo["descricoes_alteradas_outra_marca"] += 1

        for antiga, quantidade in grupo["marcas"].items():
            if _mesma_marca(antiga, nova):
                resumo["itens_mantidos"] += quantidade
            elif nova is None:
                resumo["itens_alterados_sem_marca"] += quantidade
                resumo["itens_alterados"] += quantidade
            else:
                resumo["itens_alterados_outra_marca"] += quantidade
                resumo["itens_alterados"] += quantidade

        if not todas_iguais:
            resumo["alteracoes"].append(
                (grupo["descricao"], " / ".join(sorted(antigas)), nova)
            )
            if not dry_run:
                (
                    db.query(ItemNota)
                    .filter(ItemNota.id.in_(grupo["ids"]))
                    .update({ItemNota.marca: nova}, synchronize_session=False)
                )
                resumo["itens_atualizados"] += sum(grupo["marcas"].values())

    if not dry_run and resumo["itens_atualizados"]:
        db.commit()

    resumo["cache_total"] = db.query(MarcaIA).count()
    return resumo




def _imprimir_progresso(indice, total, estatisticas, descricao, nova):
    """Log de uma linha por descrição, com o acumulado de IA x cache."""
    destino = nova or "SEM MARCA"
    print(
        f"[{indice:>4}/{total}] IA: {estatisticas['consultas_ia']:>4} | "
        f"cache: {estatisticas['cache_hits']:>4} | {descricao[:78]} -> {destino}",
        flush=True,
    )


def _imprimir_resumo(resumo):
    """Resumo final: alterações para SEM MARCA, marcas mantidas e economia de cache."""
    print("\n" + "=" * 78)
    print("RESUMO DA VALIDAÇÃO DE MARCAS")
    print("=" * 78)
    print(
        f"Descrições com marca: {resumo['descricoes_alvo']} | "
        f"itens com marca: {resumo['itens_com_marca']}"
    )
    print(
        f"Analisadas agora: {resumo['descricoes_analisadas']} | "
        f"com erro: {resumo['descricoes_com_erro']}"
    )
    print(
        f"Consultas à DeepSeek: {resumo['consultas_ia']} | "
        f"economizadas pelo cache: {resumo['cache_hits']}"
    )
    print(
        f"Respostas da IA: {resumo['marcas_ia']} marca(s) | "
        f"{resumo['sem_marca_ia']} SEM MARCA | cache marcas_ia: {resumo['cache_total']}"
    )
    print("-" * 78)
    print(
        "Descrições alteradas para SEM MARCA: "
        f"{resumo['descricoes_alteradas_sem_marca']}"
    )
    print(f"Descrições mantidas: {resumo['descricoes_mantidas']}")
    if resumo["descricoes_alteradas_outra_marca"]:
        print(
            "Descrições alteradas para OUTRA marca: "
            f"{resumo['descricoes_alteradas_outra_marca']}"
        )
    sufixo = "seriam alterados" if resumo["dry_run"] else "alterados"
    print(
        f"Itens {sufixo} para SEM MARCA: {resumo['itens_alterados_sem_marca']} | "
        f"itens mantidos: {resumo['itens_mantidos']}"
    )
    if resumo["itens_alterados_outra_marca"]:
        print(
            "Itens alterados para OUTRA marca: "
            f"{resumo['itens_alterados_outra_marca']}"
        )
    print(
        f"Total de itens alterados: {resumo['itens_alterados']} | "
        f"gravados no banco: {resumo['itens_atualizados']}"
    )
    if resumo["dry_run"]:
        print("MODO DRY-RUN: nenhuma marca de itens_nota foi gravada.")

    if resumo["alteracoes"]:
        print("\nAlterações (amostra de até 30):")
        for descricao, antiga, nova in resumo["alteracoes"][:30]:
            print(f"  {descricao} | {antiga} -> {nova or 'SEM MARCA'}")
        restantes = len(resumo["alteracoes"]) - 30
        if restantes > 0:
            print(f"  ... e mais {restantes} alteração(ões).")

    if resumo["erros"]:
        print("\nAVISOS: descrições que não puderam ser validadas (marca antiga mantida):")
        for descricao, erro in resumo["erros"][:20]:
            print(f"  - {descricao}: {erro}")
        restantes = len(resumo["erros"]) - 20
        if restantes > 0:
            print(f"  ... e mais {restantes} erro(s).")
        if resumo["interrompido"]:
            print("A execução foi interrompida; rode novamente para aproveitar o cache.")


def _criar_backup():
    """Copia consistente do banco SQLite antes de alterar `itens_nota`."""
    origem = Path(DB_PATH)
    if not origem.exists():
        return None
    destino = origem.with_name(
        origem.name + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    )
    conexao_origem = sqlite3.connect(str(origem))
    conexao_destino = sqlite3.connect(str(destino))
    try:
        with conexao_destino:
            conexao_origem.backup(conexao_destino)
    finally:
        conexao_origem.close()
        conexao_destino.close()
    return destino


def _limite_positivo(valor):
    try:
        numero = int(valor)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("--limite precisa ser um número inteiro.")
    if numero <= 0:
        raise argparse.ArgumentTypeError("--limite precisa ser maior que zero.")
    return numero


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Valida com a IA (e com a memória marcas_ia) as marcas já gravadas "
            "em itens_nota, removendo falsos positivos como OSSO, LIXO e TRANSP."
        )
    )
    parser.add_argument(
        "--limite",
        type=_limite_positivo,
        help="processa apenas as N primeiras descrições (amostra; o cache fica gravado).",
    )
    parser.add_argument(
        "--revalidar",
        action="store_true",
        help=(
            "apaga a memória dessas descrições e consulta a IA de novo, usando a "
            "marca atual como candidata (gasta tokens; use após ajustar o prompt)."
        ),
    )
    parser.add_argument(
        "--contem",
        metavar="TEXTO",
        help=(
            "processa apenas descrições que contenham TEXTO (sem diferenciar "
            "caixa/acentos), para revalidar casos pontuais."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="analisa e mostra o resumo, mas não altera itens_nota.",
    )
    parser.add_argument(
        "--sem-backup",
        action="store_true",
        help="não cria a cópia de segurança do banco antes de gravar.",
    )
    args = parser.parse_args(argv)

    init_db()
    db = SessionLocal()
    try:
        grupos = _filtrar_por_contem(_carregar_alvos(db), args.contem)
        if not grupos:
            if args.contem:
                print(f"Nenhuma descrição com marca contendo {args.contem!r}.")
            else:
                print("Nenhuma descrição com marca cadastrada em itens_nota.")
            return 0
        total_itens = sum(sum(grupo["marcas"].values()) for grupo in grupos.values())
        print(f"Banco: {DB_PATH}")
        print(
            f"Descrições com marca: {len(grupos)} | itens com marca: {total_itens} | "
            f"modelo IA: {ia_consenso.modelo()}"
        )
        if args.limite:
            print(f"Limite desta execução: {args.limite} descrição(ões).")
        if args.contem:
            print(f"Filtro: somente descrições contendo {args.contem!r}.")
        if args.revalidar:
            print("Modo revalidar: a memória dessas descrições será consultada de novo.")
        if not ia_consenso.configurada():
            print(
                "ERRO: DEEPSEEK_API_KEY não configurada. Defina a variável no .env "
                "e rode novamente."
            )
            return 1

        if not args.dry_run and not args.sem_backup:
            backup = _criar_backup()
            if backup:
                print(f"Backup do banco: {backup}")

        print("Iniciando validação...\n")
        resumo = validar_marcas_base(
            db,
            limite=args.limite,
            dry_run=args.dry_run,
            progresso=_imprimir_progresso,
            revalidar=args.revalidar,
            contem=args.contem,
        )
    except ia_consenso.IAnaoconfigurada as erro:
        print(f"ERRO: {erro}")
        return 1
    except KeyboardInterrupt:
        print(
            "\nInterrompido pelo usuário. Nenhuma marca de itens_nota foi alterada "
            "nesta execução; as respostas já obtidas continuam no cache."
        )
        return 130
    finally:
        db.close()

    _imprimir_resumo(resumo)
    return 2 if resumo["erros"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

