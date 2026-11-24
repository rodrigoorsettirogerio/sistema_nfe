"""Teste manual da integração com a API do Omie (fase 1).

Uso, a partir da raiz do projeto:

    python testar_omie.py
    python testar_omie.py --dias 15
    python testar_omie.py --data-inicio 01/11/2026 --data-fim 23/11/2026

O script não grava nada no banco: apenas lista os recebimentos de NF-e de
entrada do período, baixa os XMLs completos para ``xmls/omie_teste/`` e mostra
as contagens. As credenciais vêm do ``.env`` (``OMIE_APP_KEY`` e
``OMIE_APP_SECRET``) e nunca são exibidas.
"""

import argparse
import html
import json
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent
# O carregamento é explícito no script (além do feito pelo cliente) para que a
# mensagem de "credenciais ausentes" apareça antes de qualquer chamada de rede
# e para deixar claro de onde vêm OMIE_APP_KEY/OMIE_APP_SECRET.
load_dotenv(RAIZ / ".env", override=False)

from backend.omie_client import (  # noqa: E402  (após o load_dotenv, de propósito)
    ErroOmie,
    OmieClient,
    OmieNaoConfigurado,
    chave_do_xml,
)

PASTA_PADRAO = RAIZ / "xmls" / "omie_teste"


def _periodo_padrao(dias: int) -> tuple:
    """Últimos ``dias`` dias, incluindo hoje.

    A janela inclui o dia atual porque o Omie filtra por data de emissão: uma
    nota emitida hoje cedo já deve aparecer no teste. ``max(1, dias)`` impede
    que ``--dias 0`` gere período invertido.
    """
    hoje = date.today()
    return hoje - timedelta(days=max(1, dias) - 1), hoje


def _data_argumento(texto: str) -> date:
    """Converte ``--data-*`` aceitando ``dd/mm/aaaa`` e ``aaaa-mm-dd``."""
    for formato in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(texto.strip(), formato).date()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"data inválida: {texto!r} (use dd/mm/aaaa)"
    )


def _resolver_periodo(args) -> tuple:
    """Combina ``--dias`` e as datas explícitas em um período válido.

    Quando só um dos limites é informado, o outro é completado a partir de
    hoje/``--dias`` para o teste não falhar por esquecimento de argumento.
    """
    hoje = date.today()
    if args.data_inicio or args.data_fim:
        fim = args.data_fim or hoje
        inicio = args.data_inicio or (
            fim - timedelta(days=max(1, args.dias) - 1)
        )
    else:
        inicio, fim = _periodo_padrao(args.dias)
    if inicio > fim:
        raise ValueError(
            "--data-inicio não pode ser posterior a --data-fim."
        )
    return inicio, fim


def _primeiro(valores: dict, *chaves) -> str:
    """Primeiro campo preenchido entre os nomes candidatos.

    Os nomes dos campos variam entre ``ListarRecebimentos`` e
    ``ListarDocumentos`` (e entre versões da API); testar alternativas evita
    que o resumo fique vazio só por causa de um nome diferente.
    """
    for chave in chaves:
        valor = valores.get(chave)
        if valor not in (None, ""):
            return str(valor).strip()
    return ""


def _formatar_valor(valor) -> str:
    """Formata o valor monetário no padrão brasileiro, sem quebrar se textual."""
    texto = str(valor).strip()
    if not texto:
        return ""
    try:
        numero = float(texto)
    except ValueError:
        try:
            numero = float(texto.replace(".", "").replace(",", "."))
        except ValueError:
            return texto
    formatado = f"{numero:,.2f}".replace(",", "X")
    return "R$ " + formatado.replace(".", ",").replace("X", ".")


def _resumo_nota(nota: dict) -> str:
    """Linha curta com número, emissão, chave, fornecedor e valor.

    Os nomes usados primeiro são os do tipo ``recebimentos`` documentado
    (``cNumeroNFe``, ``cChaveNfe``, ``nValorNFe``); as alternativas cobrem
    respostas antigas/parciais sem deixar a linha vazia.
    """
    cabec = nota.get("cabec")
    if not isinstance(cabec, dict):
        cabec = nota
    partes = []
    numero = _primeiro(cabec, "cNumeroNFe", "nNumeroNF", "cNumeroNF", "nNumero")
    if numero:
        partes.append(f"nº {numero}")
    emissao = _primeiro(cabec, "dEmissaoNFe", "dEmissao")
    if emissao:
        partes.append(f"emissão {emissao}")
    chave = _primeiro(cabec, "cChaveNfe", "cChaveNFe", "cChave")
    if chave:
        partes.append(f"chave {chave}")
    fornecedor = _primeiro(
        cabec, "cNome", "cRazaoSocial", "cFantasia", "cNomeFornecedor"
    )
    if fornecedor:
        partes.append(fornecedor)
    valor = _formatar_valor(
        _primeiro(cabec, "nValorNFe", "nValorNF", "nValorTotal", "nValor")
    )
    if valor:
        partes.append(valor)
    return " | ".join(partes) or "recebimento sem identificação"


def _nome_arquivo(documento: dict, xml: str) -> str:
    """Nome do arquivo: ``<chave>.xml`` ou um identificador estável.

    A chave de acesso é o identificador natural da NF-e e é o padrão já usado
    em ``xmls/`` pelo projeto. Quando o XML não expõe a chave (documento
    cancelado/resumido), usa-se o id/número do Omie para não sobrescrever
    arquivos diferentes com o mesmo nome.
    """
    chave = chave_do_xml(xml)
    if not chave:
        chave = re.sub(r"\D", "", str(documento.get("nChave") or ""))
    if len(chave) == 44:
        return f"{chave}.xml"
    identificador = _primeiro(
        documento, "nIdNF", "nIdDocumento", "nNumeroNF", "cNumeroNF", "nNumero"
    )
    base = re.sub(r"[^0-9A-Za-z_-]+", "_", f"omie-{identificador}").strip("_")
    return f"{base or 'documento_omie'}.xml"


def _salvar_xmls(documentos, pasta: Path) -> tuple:
    """Grava cada ``cXml`` e devolve ``(salvos, sem_xml, repetidos)``.

    A pasta é criada sob demanda para o teste funcionar em instalação limpa.
    Arquivos repetidos são ignorados porque a Omie pode devolver o mesmo
    documento em páginas diferentes no meio de uma varredura. O arquivo é
    aberto com ``newline=""`` para preservar o XML exatamente como veio (sem
    CRLF extra do Windows), que é o que a fase 2 vai importar. A gravação usa
    UTF-8 porque a NF-e é obrigatoriamente UTF-8 (MOC): o ``cXml`` chega como
    texto dentro do JSON (não há bytes originais a preservar) e regravar em
    UTF-8 mantém a declaração ``encoding`` do documento válida.

    O ``cXml`` da Omie vem **HTML-escapado** (``"&lt;?xml..."``), então o
    ``html.unescape`` desfaz a camada de escape antes de gravar: sem isso o
    arquivo salvo não seria um XML válido para ``import_xmls.py``.
    """
    pasta.mkdir(parents=True, exist_ok=True)
    salvos = 0
    sem_xml = 0
    repetidos = 0
    vistos = set()
    for documento in documentos:
        xml = html.unescape(str(documento.get("cXml") or "")).strip()
        if not xml:
            sem_xml += 1
            continue
        nome = _nome_arquivo(documento, xml)
        if nome in vistos:
            repetidos += 1
            continue
        vistos.add(nome)
        with open(pasta / nome, "w", encoding="utf-8", newline="") as arquivo:
            arquivo.write(xml)
        salvos += 1
    return salvos, sem_xml, repetidos


def _imprimir_erro(exc: ErroOmie) -> None:
    """Mostra o erro do Omie com o código e o corpo (sem credenciais)."""
    print(f"❌ FALHOU: {exc}")
    if exc.codigo:
        print(f"   Código Omie: {exc.codigo}")
    if exc.status is not None:
        print(f"   HTTP {exc.status}")
    if exc.corpo is not None:
        print("   Corpo da resposta:")
        try:
            print(json.dumps(exc.corpo, ensure_ascii=False, indent=2))
        except (TypeError, ValueError):
            print(exc.corpo)
    mensagem = str(exc).lower()
    if any(
        termo in mensagem
        for termo in ("app_key", "app_secret", "credencia", "aplicativo")
    ):
        print(
            "   Confira OMIE_APP_KEY/OMIE_APP_SECRET no .env: as chaves são "
            "geradas em app.omie.com.br (API > Aplicativos)."
        )


def main(argv=None) -> int:
    """Executa o teste de integração e devolve o código de saída do processo."""
    parser = argparse.ArgumentParser(
        description="Testa a API do Omie (recebimentos e XMLs de entrada)."
    )
    parser.add_argument(
        "--dias",
        type=int,
        default=7,
        help="Janela em dias, incluindo hoje (padrão: 7).",
    )
    parser.add_argument(
        "--data-inicio",
        type=_data_argumento,
        default=None,
        help="Início do período (dd/mm/aaaa). Padrão: hoje - --dias + 1.",
    )
    parser.add_argument(
        "--data-fim",
        type=_data_argumento,
        default=None,
        help="Fim do período (dd/mm/aaaa). Padrão: hoje.",
    )
    parser.add_argument(
        "--pasta",
        default=str(PASTA_PADRAO),
        help=f"Pasta de destino dos XMLs (padrão: {PASTA_PADRAO}).",
    )
    args = parser.parse_args(argv)

    try:
        inicio, fim = _resolver_periodo(args)
    except ValueError as exc:
        print(f"❌ FALHOU: {exc}")
        return 1

    cliente = OmieClient()
    if not cliente.configurado:
        print("❌ FALHOU: OMIE_APP_KEY/OMIE_APP_SECRET não configurados no .env.")
        print(
            "   Gere as chaves em app.omie.com.br (API > Aplicativos) e "
            "acrescente ao .env:"
        )
        print("   OMIE_APP_KEY=sua_chave")
        print("   OMIE_APP_SECRET=seu_segredo")
        return 1

    pasta = Path(args.pasta)
    print(
        f"🔎 Omie — NF-e de entrada de {inicio:%d/%m/%Y} a {fim:%d/%m/%Y} "
        f"(janela de {(fim - inicio).days + 1} dia(s))"
    )

    try:
        recebimentos = cliente.listar_todos_recebimentos(inicio, fim)
        print(f"📋 Recebimentos listados: {len(recebimentos)}")
        for nota in recebimentos[:5]:
            print(f"   • {_resumo_nota(nota)}")
        if len(recebimentos) > 5:
            print(f"   ... e mais {len(recebimentos) - 5}")

        documentos = cliente.listar_todos_documentos_entrada(inicio, fim)
        print(f"📄 Documentos de entrada listados: {len(documentos)}")

        salvos, sem_xml, repetidos = _salvar_xmls(documentos, pasta)
    except OmieNaoConfigurado as exc:
        print(f"❌ FALHOU: {exc}")
        return 1
    except ErroOmie as exc:
        _imprimir_erro(exc)
        return 1
    except ValueError as exc:
        print(f"❌ FALHOU: {exc}")
        return 1
    except OSError as exc:
        print(f"❌ FALHOU: não foi possível gravar os XMLs em {pasta}: {exc}")
        return 1

    print(f"💾 XMLs gravados em {pasta}: {salvos}")
    if sem_xml:
        print(f"   {sem_xml} documento(s) sem cXml na resposta (não gravados).")
    if repetidos:
        print(f"   {repetidos} arquivo(s) repetido(s) ignorado(s).")
    print(
        f"✅ OK — recebimentos: {len(recebimentos)} | "
        f"documentos: {len(documentos)} | XMLs salvos: {salvos}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
