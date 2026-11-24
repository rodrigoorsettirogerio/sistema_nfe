"""Sincronização manual dos recebimentos do Omie com o banco (fase 2).

Uso, a partir da raiz do projeto:

    python testar_omie_sync.py --simular
    python testar_omie_sync.py --cnpj 11.222.333/0001-81 --dias 7
    python testar_omie_sync.py --cnpj 11222333000181 --data-inicio 01/11/2026 --data-fim 23/11/2026
    python testar_omie_sync.py --cnpj 11222333000181 --etapas 60,80 --simular
    python testar_omie_sync.py --cnpj 11222333000181 --gravar-credenciais

Diferente do ``testar_omie.py`` (que só lista a API e baixa XML), este script
GRAVA no banco: fornecedores, ``notas_fiscais``, ``itens_nota`` e a memória
de-para, com dedupe pela chave de acesso — rodar duas vezes não duplica nada.
Sem ``--cnpj`` ele usa a loja quando existe só uma no banco; com mais de uma,
lista as opções e pede o argumento.

``--simular`` executa a importação inteira e desfaz no fim (rollback): serve
para conferir o volume (novas/duplicadas/itens) antes de gravar de verdade.

``--sem-marcas`` só é aceito junto de ``--simular``: a importação real SEMPRE
recalcula ``itens_nota.marca`` no fim. A marca entra na chave do grupo dos
relatórios (Produto + Marca), então um lote gravado sem marca abre linha
duplicada ("BATATA ... (2)") no preço do mesmo produto. Usado sem ``--simular``
o script encerra com erro (código de saída 2) e não grava nada.

``--gravar-credenciais`` cifra o par App Key/App Secret da loja nas colunas
``empresas.omie_app_key``/``omie_app_secret`` (a chave de criptografia vem de
``OMIE_ENC_KEY``/``NFE_SECRET_KEY``/``.secret_key`` e nunca vai para o banco) e
encerra sem sincronizar. O par é pedido em ``--app-key``/``--app-secret`` ou,
na falta deles, lido do ``.env``. Sem esse comando a sincronização usa a
credencial da loja e, quando ela ainda não tem uma, o par do ``.env``.
"""

import argparse
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent
# Carregamento explícito (o mesmo feito pelo cliente e pelo módulo de cripto) para
# que a mensagem de "credenciais ausentes" apareça antes de qualquer chamada de rede.
load_dotenv(RAIZ / ".env", override=False)

from backend import cripto_credenciais as bcripto  # noqa: E402  (após o load_dotenv)
from backend import omie_sync as bsync  # noqa: E402
from backend.aprendizado_produtos import normalizar_cnpj  # noqa: E402
from backend.database import SessionLocal, init_db  # noqa: E402
from backend.models import Empresa  # noqa: E402
from backend.omie_client import ErroOmie, OmieNaoConfigurado  # noqa: E402


def _data_argumento(texto: str) -> date:
    """Converte ``--data-*`` aceitando ``dd/mm/aaaa`` e ``aaaa-mm-dd``."""
    for formato in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(texto.strip(), formato).date()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(f"data inválida: {texto!r} (use dd/mm/aaaa)")


def _resolver_periodo(args) -> tuple:
    """Combina ``--dias`` e as datas explícitas em um período válido.

    Só um dos limites informado é completado a partir de hoje/``--dias``, para o
    teste não falhar por esquecimento de argumento (mesma regra do testar_omie.py).
    """
    hoje = date.today()
    if args.data_inicio or args.data_fim:
        fim = args.data_fim or hoje
        inicio = args.data_inicio or (fim - timedelta(days=max(1, args.dias) - 1))
    else:
        inicio = hoje - timedelta(days=max(1, args.dias) - 1)
        fim = hoje
    if inicio > fim:
        raise ValueError("--data-inicio não pode ser posterior a --data-fim.")
    return inicio, fim


def _etapas_argumento(texto: str) -> tuple:
    """``--etapas 40,60,80`` -> ``("40", "60", "80")``, sem repetição."""
    etapas = []
    for parte in str(texto or "").replace(";", ",").split(","):
        valor = parte.strip()
        if valor and valor not in etapas:
            etapas.append(valor)
    if not etapas:
        raise argparse.ArgumentTypeError("informe ao menos uma etapa (40, 60, 80)")
    return tuple(etapas)


def _erro_sem_marcas(args) -> str:
    """Mensagem de erro quando ``--sem-marcas`` é usado fora da simulação.

    ``--sem-marcas`` desliga ``recalcular_marcas_itens`` e deixa os itens novos
    com ``itens_nota.marca = NULL``. Como a marca entra na chave do grupo dos
    relatórios, o mesmo produto passa a aparecer em duas linhas ("... (2)").
    Na simulação nada é gravado (rollback no fim), então lá o atalho é inofensivo.
    Devolve ``""`` quando a combinação é válida.
    """
    if getattr(args, "sem_marcas", False) and not getattr(args, "simular", False):
        return (
            "--sem-marcas só é permitido junto de --simular: a importação real "
            "sempre recalcula itens_nota.marca (a marca entra na chave do grupo "
            "nos relatórios e o item sem marca abre linha duplicada). Rode de "
            "novo sem --sem-marcas para importar."
        )
    return ""


def _nome_da_empresa(empresa) -> str:
    """Rótulo curto da loja para as mensagens (nome fantasia > razão > CNPJ)."""
    return (
        (empresa.nome_loja or "").strip()
        or (empresa.razao_social or "").strip()
        or empresa.cnpj
        or f"empresa {empresa.id}"
    )


def _escolher_empresa(db, cnpj: str = ""):
    """Loja a sincronizar: pelo CNPJ informado ou a única cadastrada.

    Com mais de uma loja e sem ``--cnpj`` a função devolve ``None`` e imprime a
    lista — escolher "a primeira" silenciosamente gravaria notas na loja errada.
    """
    digitos = normalizar_cnpj(cnpj)
    if digitos:
        empresa = db.query(Empresa).filter(Empresa.cnpj == digitos).first()
        if empresa is None:
            print(f"❌ FALHOU: nenhuma loja com o CNPJ {digitos} no banco.")
        return empresa

    empresas = db.query(Empresa).order_by(Empresa.id).all()
    if len(empresas) == 1:
        return empresas[0]
    if not empresas:
        print("❌ FALHOU: nenhuma loja cadastrada no banco (cadastre a loja antes).")
        return None
    print("❌ FALHOU: mais de uma loja cadastrada — informe --cnpj. Opções:")
    for empresa in empresas:
        print(f"   • {empresa.cnpj} — {_nome_da_empresa(empresa)}")
    return None


def _imprimir_resultado(resultado: dict) -> None:
    """Contadores da sincronização, etapa por etapa e amostra das notas novas."""
    for etapa, dados in (resultado.get("etapas") or {}).items():
        erro = dados.get("erro")
        if erro:
            print(f"   Etapa {etapa} ({dados.get('rotulo')}): ERRO — {erro}")
        else:
            print(
                f"   Etapa {etapa} ({dados.get('rotulo')}): "
                f"{dados.get('recebimentos', 0)} recebimento(s)"
            )

    print(
        f"📥 Recebimentos lidos: {resultado['recebimentos']} | "
        f"novas: {resultado['novas']} | duplicadas: {resultado['duplicadas']} | "
        f"canceladas: {resultado['canceladas']} | ignoradas: {resultado['ignoradas']}"
    )
    print(
        f"🧾 Itens gravados: {resultado['itens']} | "
        f"bonificados/doação ignorados: {resultado['bonificados']} | "
        f"notas sem itens: {resultado['notas_sem_itens']}"
    )
    print(
        f"🏭 Fornecedores: {resultado['fornecedores_novos']} novo(s), "
        f"{resultado['fornecedores_atualizados']} atualizado(s) | "
        f"de-para: {resultado['mapeamentos_criados']} criado(s), "
        f"{resultado['mapeamentos_usados']} usado(s)"
    )

    for nota in resultado.get("notas_novas") or []:
        valor = nota.get("valor") or 0
        valor_texto = (
            f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        )
        print(
            f"   + NF {nota.get('numero') or '?'}/{nota.get('serie') or '?'} "
            f"de {nota.get('data') or '?'} — {nota.get('fornecedor') or '?'} — "
            f"{valor_texto} — {nota.get('itens')} item(ns) — {nota.get('chave')}"
        )

    marcas = resultado.get("marcas")
    if isinstance(marcas, dict):
        print(
            "🏷️  Marcas recalculadas (cache local): "
            f"{marcas.get('com_marca', 0)} item(ns) com marca"
        )

    avisos = resultado.get("avisos") or []
    if avisos:
        print(f"⚠️  {len(avisos)} aviso(s):")
        for aviso in avisos:
            print(f"   - {aviso}")

    erros = resultado.get("erros") or []
    if erros:
        print(f"⚠️  {len(erros)} erro(s)/aviso(s):")
        for erro in erros[:20]:
            print(f"   - {erro}")
        if len(erros) > 20:
            print(f"   ... e mais {len(erros) - 20}")



def _gravar_credenciais(db, empresa, args) -> int:
    """Grava (cifrado) o par App Key/App Secret da loja e encerra.

    O par vem de ``--app-key``/``--app-secret``; o que faltar é lido do ``.env``
    (``OMIE_APP_KEY``/``OMIE_APP_SECRET``), que é onde o operador já costuma
    tê-lo. Nada é exibido em texto: a confirmação usa só o final do segredo.
    """
    app_key = (args.app_key or os.getenv("OMIE_APP_KEY") or "").strip()
    app_secret = (args.app_secret or os.getenv("OMIE_APP_SECRET") or "").strip()
    if not app_key or not app_secret:
        print(
            "❌ FALHOU: informe --app-key e --app-secret (ou defina "
            "OMIE_APP_KEY/OMIE_APP_SECRET no .env)."
        )
        return 1

    bsync.definir_credenciais(db, empresa, app_key, app_secret)
    print(
        f"🔐 Credenciais Omie gravadas CIFRADAS na loja {_nome_da_empresa(empresa)} "
        f"(CNPJ {empresa.cnpj}):"
    )
    print(f"   App Key:    {bcripto.mascarar_segredo(app_key)}")
    print(f"   App Secret: {bcripto.mascarar_segredo(app_secret)}")
    print(
        "   A chave de criptografia (OMIE_ENC_KEY/NFE_SECRET_KEY/.secret_key) "
        "não fica no banco — guarde-a junto com o backup."
    )
    print(
        "Agora rode \"python " + Path(__file__).name + " --simular\" para "
        "conferir o volume antes de importar."
    )
    return 0



def main(argv=None) -> int:
    """Executa a sincronização e devolve o código de saída do processo."""
    parser = argparse.ArgumentParser(
        description="Sincroniza os recebimentos de NF-e do Omie com o banco (fase 2)."
    )
    parser.add_argument(
        "--cnpj",
        default="",
        help="CNPJ da loja (com ou sem máscara). Padrão: a única loja do banco.",
    )
    parser.add_argument(
        "--dias",
        type=int,
        default=bsync.DIAS_PADRAO,
        help=f"Janela em dias, incluindo hoje (padrão: {bsync.DIAS_PADRAO}).",
    )
    parser.add_argument(
        "--data-inicio",
        type=_data_argumento,
        default=None,
        help="Início do período por emissão (dd/mm/aaaa). Padrão: hoje - --dias + 1.",
    )
    parser.add_argument(
        "--data-fim",
        type=_data_argumento,
        default=None,
        help="Fim do período por emissão (dd/mm/aaaa). Padrão: hoje.",
    )
    parser.add_argument(
        "--tudo",
        action="store_true",
        help="Importa todo o histórico das etapas (ignora --dias/datas).",
    )
    parser.add_argument(
        "--etapas",
        type=_etapas_argumento,
        default=bsync.ETAPAS_SYNC,
        help="Etapas do kanban a importar (padrão: 40,60,80).",
    )
    parser.add_argument(
        "--simular",
        action="store_true",
        help="Executa tudo e desfaz no fim (não grava nada no banco).",
    )
    parser.add_argument(
        "--gravar-credenciais",
        action="store_true",
        help="Grava as credenciais da loja (cifradas) e encerra, sem sincronizar.",
    )
    parser.add_argument(
        "--app-key",
        default="",
        help="App Key do Omie para --gravar-credenciais (padrão: OMIE_APP_KEY do .env).",
    )
    parser.add_argument(
        "--app-secret",
        default="",
        help="App Secret do Omie para --gravar-credenciais (padrão: OMIE_APP_SECRET).",
    )
    parser.add_argument(
        "--sem-marcas",
        action="store_true",
        help=(
            "Só junto de --simular: não recalcula itens_nota.marca na simulação. "
            "A importação real sempre recalcula."
        ),
    )
    args = parser.parse_args(argv)

    erro_sem_marcas = _erro_sem_marcas(args)
    if erro_sem_marcas:
        print(f"❌ FALHOU: {erro_sem_marcas}")
        return 2

    try:
        if args.tudo:
            inicio, fim = None, None
        else:
            inicio, fim = _resolver_periodo(args)
    except ValueError as exc:
        print(f"❌ FALHOU: {exc}")
        return 1

    init_db()
    db = SessionLocal()
    try:
        empresa = _escolher_empresa(db, args.cnpj)
        if empresa is None:
            return 1

        if args.gravar_credenciais:
            return _gravar_credenciais(db, empresa, args)

        janela = (
            "histórico completo"
            if inicio is None
            else f"{inicio:%d/%m/%Y} a {fim:%d/%m/%Y} "
            f"({(fim - inicio).days + 1} dia(s))"
        )
        print(
            f"🔄 Omie — loja {_nome_da_empresa(empresa)} (CNPJ {empresa.cnpj}) | "
            f"{janela} | etapas {','.join(args.etapas)}"
            + (" | SIMULAÇÃO (rollback no fim)" if args.simular else "")
        )

        resultado = bsync.sincronizar_recebimentos(
            db,
            empresa,
            data_inicio=inicio,
            data_fim=fim,
            dias=None if args.tudo else args.dias,
            etapas=args.etapas,
            confirmar=not args.simular,
            recalcular_marcas_itens=not args.sem_marcas,
        )
    except (
        bsync.ErroSincronizacao,
        bcripto.ErroCredenciais,
        OmieNaoConfigurado,
        ErroOmie,
        ValueError,
    ) as exc:
        db.rollback()
        print(f"❌ FALHOU: {exc}")
        if isinstance(exc, OmieNaoConfigurado):
            print(
                "   Grave as credenciais da loja com "
                "\"python testar_omie_sync.py --gravar-credenciais\" ou defina "
                "OMIE_APP_KEY/OMIE_APP_SECRET no .env."
            )
        return 1
    finally:
        db.close()


    print(
        f"🔑 Credenciais usadas: {resultado['origem_credenciais']}"
        + (
            f" | sincronizado em {resultado['sincronizado_em']}"
            if resultado.get("sincronizado_em")
            else ""
        )
    )
    _imprimir_resultado(resultado)

    if args.simular:
        if resultado["erros"]:
            print("⚠️  SIMULAÇÃO concluída COM ERROS — veja a lista acima.")
            return 2
        print(
            "✅ SIMULAÇÃO OK — nada foi gravado. Rode sem --simular para importar "
            f"{resultado['novas']} nota(s) e {resultado['itens']} item(ns)."
        )
        return 0

    if resultado["erros"]:
        print(
            f"⚠️  Sincronização concluída com {len(resultado['erros'])} erro(s)/"
            f"aviso(s) — notas novas: {resultado['novas']}, "
            f"duplicadas: {resultado['duplicadas']}, itens: {resultado['itens']}."
        )
        return 2

    print(
        f"✅ OK — notas novas: {resultado['novas']} | "
        f"duplicadas: {resultado['duplicadas']} | "
        f"canceladas: {resultado['canceladas']} | itens: {resultado['itens']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

