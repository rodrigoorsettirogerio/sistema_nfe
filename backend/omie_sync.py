"""Importação dos recebimentos de NF-e do Omie para o banco (fase 2).

A fonte dos dados é a própria ``ListarRecebimentos`` com ``cExibirDetalhes="S"``:
ela devolve o cabeçalho da nota (fornecedor, número, série, emissão, valor,
chave) e ``itensRecebimento`` com produto, quantidade, preço unitário, total,
NCM, EAN, CFOP e unidade. Isso significa que **a nota entra no sistema sem
depender do XML** — o XML (``contador/xml``/``ObterNfe``) fica para uma etapa
posterior e para as notas que o módulo fiscal do Omie não devolve.

Mapeamento (campo do Omie -> coluna do banco):

+-------------------------------------+-------------------------------------------+
| ``cabec.cChaveNFe``                 | ``notas_fiscais.chave_acesso`` (dedupe)   |
| ``cabec.cNumeroNFe``                | ``notas_fiscais.numero_nota`` (sem zeros) |
| ``cabec.cSerieNFe``                 | ``notas_fiscais.serie``                   |
| ``cabec.dEmissaoNFe`` (dd/mm/aaaa)  | ``notas_fiscais.data_emissao`` (DATE)     |
| ``cabec.nValorNFe``/``totais.vTotalNFe`` | ``notas_fiscais.valor_total``        |
| (empresa que está sendo sincronizada)| ``notas_fiscais.empresa_id``             |
| ``cabec.cCNPJ_CPF``                 | ``fornecedores.cnpj`` (só dígitos)        |
| ``cabec.cRazaoSocial``/``cabec.cNome`` | ``fornecedores.nome``                  |
| ``itensCabec.cEAN``                 | ``itens_nota.ean``                        |
| ``itensCabec.cDescricaoProduto``    | ``itens_nota.descricao_produto``          |
| ``itensCabec.nQtdeNFe`` (cru)       | ``itens_nota.quantidade``                 |
| ``itensCabec.nPrecoUnit`` (cru)     | ``itens_nota.valor_unitario``             |
| ``itensCabec.vTotalItem``           | ``itens_nota.valor_total``                |
| ``itensCabec.cNCM`` (8 dígitos)     | ``itens_nota.ncm``                        |
| ``itensCabec.cCFOP`` (4 dígitos)    | ``itens_nota.cfop``                       |
| ``itensCabec.cCodigoProduto``       | ``itens_nota.c_prod``                     |
| ``itensCabec.cUnidadeNfe``          | ``itens_nota.unidade_compra``             |
| conversão de embalagem              | ``itens_nota.quantidade_base`` /          |
|                                     | ``valor_unitario_base`` /                 |
|                                     | ``qtd_por_embalagem`` / ``unidade_base``  |

Regras que vêm do importador de XML (``import_xmls.py``) e são repetidas aqui
para que as duas origens produzam as MESMAS linhas:

- ``quantidade``/``valor_unitario``/``valor_total`` guardam o valor CRU do Omie;
  as conversões vão só para os campos ``*_base`` (backend/aprendizado_produtos.py);
- bonificação/doação/brinde (CFOP 5910/6910, preço zero, desconto integral ou
  texto com BONIFIC/BRINDE/DOACAO) NÃO é gravado: o item não tem preço real de
  compra e distorceria as médias dos relatórios;
- ``ncm`` e ``cfop`` são normalizados para dígitos (o Omie devolve
  "0406.10.10"/"6.101", o XML traz "04061010"/"6101");
- a memória de-para (CNPJ do fornecedor + cProd) é consultada e atualizada como
  no XML, então o mesmo produto vindo das duas origens continua com o mesmo
  EAN/descrição/embalagem.

Sinalização de divergência (sem mudar a identidade): cada item tem uma âncora —
o EAN, quando existe, ou o par (fornecedor, cProd). Se a mesma âncora já aparece
no histórico com descrição/cProd diferente, ou se o ``cCodigoProduto`` começa com
``PRD`` (código interno do Omie, não o cProd do fornecedor), o item é marcado em
``itens_auditados`` com motivo ``divergencia_produto``/``revisar_vinculacao`` e o
resumo traz ``divergencias``. As chaves dessas pendências começam com
``DIV_``/``VINC_`` (nunca ``DESC_``), então os relatórios continuam usando os
itens normalmente até a revisão manual.

Correção da descrição pelo XML da NF-e (passo 3): a listagem de recebimentos pode
trazer a descrição do CADASTRO interno do Omie (``cDescricaoProduto``), que fica
errada quando o vínculo do produto foi feito à mão (ex.: "Skirt Steak" no lugar de
"FRALDA") e/ou quando ``cCodigoProduto`` é o código interno ``PRD...``. Na etapa
40 (Faturado) o XML da nota já está no serviço DocumentosFiscais, então a
sincronização baixa o XML com ``ListarDocumentos`` e usa o ``xProd`` do fornecedor:

- a nota é localizada pela chave de acesso (``chave_do_xml``) e cada item pela
  âncora de identidade — EAN quando existe, senão fornecedor + ``cProd``; o
  ``PRD...`` interno NUNCA é âncora (a posição na nota só entra como último
  recurso, quando as duas listas têm o mesmo tamanho);
- quando o ``xProd`` difere da descrição gravada, ``itens_nota.descricao_produto``
  passa a ser a do fornecedor, o texto anterior fica em
  ``descricao_produto_anterior`` e ``descricao_origem`` registra "XML etapa 40";
- cada mudança vira uma linha em ``itens_auditados`` (chave ``XML_<nota>_<item>``,
  ``auditado=True``, motivo ``descricao_xml``) com descrição antes/depois, fonte e
  data/hora — é registro histórico e fica fora do prefixo ``DESC_`` dos relatórios;
- em 60/80 o texto da listagem serve só de conferência: descrição já igual (ou já
  vinda do XML) não é reescrita. A memória de-para também passa a guardar a
  descrição do fornecedor (exceto quando o de-para é ``manual``).


Deduplicação: a chave de acesso (44 dígitos) é única em ``notas_fiscais``. Nota
já gravada é contada como **duplicada** e ignorada (sem tocar nos itens), o que
permite rodar a sincronização quantas vezes for preciso — inclusive quando a
nota muda de etapa (40 -> 60 -> 80) entre execuções.

Etapas sincronizadas (nomes configuráveis na tela do Omie; o que vale são as
flags do ``infoCadastro``): 40 = Faturado (``cFaturado=S``), 60 = Recebido
(``cRecebido=S`` + ``dRec``) e 80 = Conferido (``cRecebido=S`` + ``dAlt``).
Notas canceladas (``infoCadastro.cCancelada="S"``) não entram.
"""

from __future__ import annotations

import html
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from backend.aprendizado_produtos import (
    aplicar_embalagem_aprendida,
    aplicar_identidade,
    interpretar_embalagem,
    limpar_entidades_texto,
    normalizar_cnpj,
    normalizar_codigo_produto,
    normalizar_quantidades,
    normalizar_texto,
    registrar_aprendizado,
)
from backend.cripto_credenciais import criptografar, descriptografar
from backend.marcas import recalcular_marcas
from backend.models import (
    Fornecedor,
    ItemAuditado,
    ItemNota,
    MapeamentoFornecedorProduto,
    NotaFiscal,
)
from backend.omie_client import (
    ErroOmie,
    OmieClient,
    OmieNaoConfigurado,
    chave_do_xml,
)

# Etapas do kanban do Omie que a importação cobre (ver flags do infoCadastro).
ETAPAS_SYNC = ("40", "60", "80")
NOME_ETAPA = {"40": "Faturado", "60": "Recebido", "80": "Conferido"}
# Janela padrão da sincronização: dias para trás a partir de hoje (inclui hoje).
DIAS_PADRAO = 30
REGISTROS_POR_PAGINA = 50
MAXIMO_PAGINAS = 100
# A Omie responde HTTP 425 (consumo indevido) quando as chamadas vão em rajada;
# 1 s entre páginas mantém a importação completa dentro do limite observado nos
# testes reais (o diagnóstico usou 2 s e nunca foi bloqueado).
INTERVALO_ENTRE_PAGINAS = 1.0
# Mesmo critério do import_xmls.py: item sem preço real não entra na média.


class ErroSincronizacao(RuntimeError):
    """Falha que impede a sincronização de uma loja (credencial, período...)."""


# ──────────────────────────────────────────────────────────────────────────────
# Normalização dos campos do Omie
# ──────────────────────────────────────────────────────────────────────────────
def _texto(valor: Any) -> str:
    """Campo do Omie como texto aparado (``None`` vira ``""``)."""
    if valor is None:
        return ""
    return str(valor).strip()


def _numero(valor: Any) -> float:
    """Campo numérico do Omie como ``float`` (0.0 quando vazio/ilegível).

    O Omie devolve preço/quantidade como número JSON, mas versões antigas e
    respostas parciais já trouxeram texto — inclusive no padrão brasileiro,
    que o fallback abaixo aceita sem quebrar a importação da nota inteira.
    """
    if isinstance(valor, bool):
        return 0.0
    if isinstance(valor, (int, float)):
        return float(valor)
    texto = _texto(valor)
    if not texto:
        return 0.0
    if "," in texto:  # "1.234,56" -> "1234.56"
        texto = texto.replace(".", "").replace(",", ".")
    try:
        return float(texto)
    except ValueError:
        return 0.0


def _digitos(valor: Any, tamanho: int = 0) -> str:
    """Só os dígitos do campo, completando à esquerda com zeros até ``tamanho``.

    NCM ("0406.10.10" -> "04061010") e CFOP ("6.101" -> "6101") chegam com
    pontuação do Omie e são gravados no mesmo formato do XML (sem pontos); a
    chave de acesso chega limpa e só precisa ter as máscaras removidas.
    """
    limpo = re.sub(r"\D", "", _texto(valor))
    if tamanho and limpo:
        limpo = limpo.zfill(tamanho)
    return limpo


def _data(valor: Any, nome: str = "data") -> date:
    """Data do Omie (``dd/mm/aaaa``) ou ISO (``aaaa-mm-dd``) em ``date``."""
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    texto = _texto(valor)
    for formato in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(texto, formato).date()
        except ValueError:
            continue
    raise ErroSincronizacao(f"{nome.capitalize()} inválida: {texto!r}.")


def _ean(valor: Any) -> str:
    """EAN do item: dígitos, sem zeros à esquerda, ou ``"SEM_EAN"``.

    É a mesma regra do importador de XML: o código "SEM GTIN"/vazio vira
    ``SEM_EAN`` e o EAN válido perde os zeros à esquerda, para o mesmo produto
    não aparecer como dois no comparativo de preços.
    """
    texto = _texto(valor)
    if texto.upper() in ("", "SEM GTIN", "SEM_EAN"):
        return "SEM_EAN"
    digitos = _digitos(texto)
    if len(digitos) < 8:
        return "SEM_EAN"
    return digitos.lstrip("0") or "0"


def _item_bonificado(item: Dict[str, Any]) -> bool:
    """True para bonificação/doação/brinde (não entra na média de preço).

    Mesmos critérios do ``import_xmls.item_bonificado``: preço unitário zero,
    CFOP de remessa (5910/6910), desconto igual ao valor do item ou texto do
    produto citando bonificação/brinde/doação.
    """
    if item["valor_unitario"] <= 0:
        return True
    if item["cfop"] in CFOPS_BONIFICACAO:
        return True
    if item["valor_total"] > 0 and item["desconto"] >= item["valor_total"]:
        return True
    texto = item["descricao"].upper()
    return any(termo in texto for termo in TERMOS_BONIFICACAO)


def _bloqueado(erro: ErroOmie) -> bool:
    """True quando a Omie cortou a sequência por excesso de chamadas (HTTP 425)."""
    if erro.status == 425:
        return True
    mensagem = str(erro).lower()
    return "425" in mensagem or "consumo indevido" in mensagem

CFOPS_BONIFICACAO = {"5910", "6910"}
TERMOS_BONIFICACAO = ("BONIFIC", "BRINDE", "DOACAO", "DOAÇÃO", "DOAÃ")
# Quantas notas novas o retorno carrega para o script imprimir (o total é sempre exato).
LIMITE_AMOSTRA = 5
# ── Detecção de divergência de produto (só sinaliza; NÃO altera a identidade) ──
# Motivos gravados em ``itens_auditados.motivo``. As chaves desses registros
# começam com ``DIV_``/``VINC_`` (nunca ``DESC_``), então o mapa de auditoria dos
# relatórios as ignora: o item continua contando normalmente enquanto o admin
# revisa. É o mesmo mecanismo de pendências da Auditoria, sem criar tabela nova.
MOTIVO_DIVERGENCIA_PRODUTO = "divergencia_produto"
MOTIVO_REVISAR_VINCULACAO = "revisar_vinculacao"
# ``cCodigoProduto`` com este prefixo é o código INTERNO do Omie (PRD00001...),
# não o cProd do fornecedor que o de-para usa como chave.
PREFIXO_CODIGO_INTERNO_OMIE = "PRD"
# Quantas divergências o resumo carrega (os contadores são sempre exatos).
LIMITE_AMOSTRA_DIVERGENCIAS = 20
# ── Correção da descrição pelo XML da nota (passo 3) ──────────────────────────
# Motivo gravado em ``itens_auditados`` para cada descrição trocada pelo xProd do
# XML. A chave do registro começa com ``XML_`` (nunca ``DESC_``), então o mapa de
# auditoria dos relatórios não a considera: o item continua contando normalmente.
MOTIVO_DESCRICAO_XML = "descricao_xml"
PREFIXO_MARCADOR_DESCRICAO = "XML"
# Etapa em que a nota já tem o XML no serviço DocumentosFiscais (Faturado).
ETAPA_FATURADO = "40"
# Prefixo gravado em ``itens_nota.descricao_origem``: o item já foi corrigido por
# um XML anterior e não pode ser sobrescrito de novo (60/80 só conferem).
PREFIXO_ORIGEM_XML = "XML"
# Quantos eventos de correção o resumo carrega (os contadores são sempre exatos).
LIMITE_AMOSTRA_DESCRICOES = 20
# Namespace da NF-e (o parse aceita XML com e sem ele).
NS_NFE = "http://www.portalfiscal.inf.br/nfe"



# ──────────────────────────────────────────────────────────────────────────────
# Leitura de um recebimento do Omie
# ──────────────────────────────────────────────────────────────────────────────
def _itens_do_recebimento(recebimento: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Itens do recebimento em campos normalizados.

    Cada elemento de ``itensRecebimento`` é um envelope: ``itensCabec`` tem os
    campos comerciais (produto, quantidade, preço, NCM, EAN, CFOP, unidade) e
    ``itensAjustes`` tem a unidade usada no ajuste e a quantidade recebida. A
    função aceita também o formato "achatado" (os campos no próprio item) para
    não quebrar se a Omie mudar o envelope, e ignora itens sem descrição.
    """
    itens: List[Dict[str, Any]] = []
    for bruto in recebimento.get("itensRecebimento") or []:
        if not isinstance(bruto, dict):
            continue
        cabec = bruto.get("itensCabec")
        if not isinstance(cabec, dict):
            cabec = bruto
        ajustes = bruto.get("itensAjustes")
        if not isinstance(ajustes, dict):
            ajustes = {}

        # Entidades HTML/XML são decodificadas na ENTRADA: "&amp; SKIRT STEAK"
        # vira "& SKIRT STEAK" antes de comparar/gerar a chave do produto.
        descricao = limpar_entidades_texto(
            _texto(cabec.get("cDescricaoProduto") or cabec.get("xProd"))
        )
        if not descricao:
            continue  # sem descrição não há como comparar preço em relatório
        unidade = _texto(
            cabec.get("cUnidadeNfe")
            or cabec.get("uCom")
            or ajustes.get("cUnidade")
        )
        itens.append(
            {
                "c_prod": _texto(cabec.get("cCodigoProduto") or cabec.get("cProd")),
                "descricao": descricao,
                "ean": _ean(cabec.get("cEAN") or cabec.get("cEANCom")),
                "ncm": _digitos(cabec.get("cNCM"), 8) or "00000000",
                "cfop": _digitos(cabec.get("cCFOP"), 4) or "0000",
                "unidade": unidade,
                # Valores CRUS do Omie: vão direto para as colunas de auditoria.
                "quantidade": _numero(
                    cabec.get("nQtdeNFe", cabec.get("nQtde"))
                ),
                "valor_unitario": _numero(
                    cabec.get("nPrecoUnit", cabec.get("nValorUnit"))
                ),
                "valor_total": _numero(
                    cabec.get("vTotalItem", cabec.get("nValorTotal"))
                ),
                "desconto": _numero(cabec.get("vDesconto")),
            }
        )
    return itens


def _dados_do_recebimento(recebimento: Dict[str, Any]) -> Dict[str, Any]:
    """Cabeçalho do recebimento já normalizado (chave, número, fornecedor...).

    ``cChaveNFe`` é a identidade da nota no sistema: sem 44 dígitos não há como
    deduplicar nem casar com o XML, então o recebimento é descartado com aviso
    (importar sem chave criaria uma nota duplicada na próxima sincronização).
    O nome do fornecedor prioriza a razão social — ``cNome`` costuma ser o nome
    fantasia usado na tela do Omie, que não é o que a nota fiscal mostra.
    """
    cabec = recebimento.get("cabec")
    if not isinstance(cabec, dict):
        cabec = recebimento.get("cabecalho")
    if not isinstance(cabec, dict):
        cabec = {}
    totais = recebimento.get("totais")
    if not isinstance(totais, dict):
        totais = {}
    cadastro = recebimento.get("infoCadastro")
    if not isinstance(cadastro, dict):
        cadastro = {}

    chave = _digitos(cabec.get("cChaveNFe") or recebimento.get("cChaveNFe"))
    if len(chave) != 44:
        chave = ""
    numero = _digitos(cabec.get("cNumeroNFe") or cabec.get("nNumeroNFe"))
    valor = _numero(cabec.get("nValorNFe"))
    if not valor:
        valor = _numero(totais.get("vTotalNFe"))
    emissao_txt = cabec.get("dEmissaoNFe") or cadastro.get("dFat")
    emissao = None
    if _texto(emissao_txt):
        try:
            emissao = _data(emissao_txt, "data de emissão")
        except ErroSincronizacao:
            emissao = None
    return {
        "chave": chave,
        "numero": numero.lstrip("0") or "",
        "serie": _texto(cabec.get("cSerieNFe")).lstrip("0") or None,
        "emissao": emissao,
        "valor": valor,
        "cnpj": normalizar_cnpj(cabec.get("cCNPJ_CPF")),
        # Razão social do fornecedor também entra sem entidade ("&amp;" -> "&").
        "fornecedor": limpar_entidades_texto(
            _texto(cabec.get("cRazaoSocial") or cabec.get("cNome"))
        ),
        "cancelada": _texto(cadastro.get("cCancelada")).upper() == "S",
        "itens": _itens_do_recebimento(recebimento),
    }




# ──────────────────────────────────────────────────────────────────────────────
# Divergência de produto: âncora + histórico (SÓ sinaliza, não muda identidade)
# ──────────────────────────────────────────────────────────────────────────────
def _ean_valido(valor: Any) -> bool:
    """True para EAN real (não vazio, ``SEM_EAN``/``SEM GTIN``/``0``)."""
    texto = _texto(valor).upper()
    return bool(texto) and texto not in {"SEM_EAN", "SEM GTIN", "0"}


def _ancora_do_item(
    item: Dict[str, Any], fornecedor_id: Optional[int]
) -> Optional[Tuple[Any, ...]]:
    """Âncora de identidade do item.

    ``("EAN", ean)`` quando o item tem código de barras real (o EAN é global e
    identifica o produto mesmo entre fornecedores); senão
    ``("FORNECEDOR_PRODUTO", fornecedor_id, cProd)`` — a mesma chave da memória
    de-para. Sem EAN, sem fornecedor ou sem cProd não há como comparar: ``None``.
    """
    if _ean_valido(item.get("ean")):
        return ("EAN", _texto(item.get("ean")))
    c_prod = normalizar_codigo_produto(item.get("c_prod"))
    if c_prod and fornecedor_id is not None:
        return ("FORNECEDOR_PRODUTO", fornecedor_id, c_prod)
    return None


def _ancora_legivel(ancora: Optional[Tuple[Any, ...]]) -> str:
    """Texto da âncora para o resumo (não é gravado no banco)."""
    if not ancora:
        return ""
    if ancora[0] == "EAN":
        return f"EAN {ancora[1]}"
    return f"fornecedor {ancora[1]} + cProd {ancora[2]}"


class _HistoricoIdentidade:
    """Índices de identidade já gravados, carregados sob demanda.

    Comparar item a item direto no banco seria uma consulta por item. Aqui o
    índice por EAN e por (fornecedor, cProd) é montado uma vez e reaproveitado
    por todas as notas da execução. O mapa de-para (já carregado inteiro em
    ``sincronizar_recebimentos``) também é indexado por EAN, para pegar o caso
    em que o mesmo código de barras aparece com outro cProd/descrição.
    """

    LIMITE_HISTORICO = 100

    def __init__(self, db, mapa_aprendizado):
        self.db = db
        self.mapa = mapa_aprendizado
        self._mapa_por_ean: Dict[str, List[Any]] = {}
        self._itens_por_ean: Dict[str, List[Any]] = {}
        self._itens_por_fornecedor_prod: Dict[Tuple[int, str], List[Any]] = {}
        for mapeamento in mapa_aprendizado.values():
            ean = _texto(getattr(mapeamento, "ean_padrao", ""))
            if _ean_valido(ean):
                self._mapa_por_ean.setdefault(ean, []).append(mapeamento)

    def mapeamentos_do_ean(self, ean: str) -> List[Any]:
        return self._mapa_por_ean.get(ean, [])

    def mapeamento_do_fornecedor(self, cnpj: str, c_prod: str):
        if not cnpj or not c_prod:
            return None
        return self.mapa.get(
            (normalizar_cnpj(cnpj), normalizar_codigo_produto(c_prod))
        )

    def itens_do_ean(self, ean: str) -> List[Any]:
        if ean not in self._itens_por_ean:
            encontrados = (
                self.db.query(
                    ItemNota.descricao_produto,
                    ItemNota.c_prod,
                    ItemNota.fornecedor_id,
                )
                .filter(ItemNota.ean == ean)
                .order_by(ItemNota.id)
                .limit(self.LIMITE_HISTORICO)
                .all()
            )
            # Não guarda vazio: o próximo item da MESMA nota (ainda não commitado
            # quando a primeira consulta rodou) pode ser o primeiro histórico.
            if encontrados:
                self._itens_por_ean[ean] = encontrados
            return encontrados
        return self._itens_por_ean[ean]

    def itens_do_fornecedor_prod(self, fornecedor_id: int, c_prod: str) -> List[Any]:
        chave = (fornecedor_id, c_prod)
        if chave not in self._itens_por_fornecedor_prod:
            encontrados = (
                self.db.query(ItemNota.descricao_produto, ItemNota.ean)
                .filter(ItemNota.fornecedor_id == fornecedor_id)
                .filter(ItemNota.c_prod == c_prod)
                .order_by(ItemNota.id)
                .limit(self.LIMITE_HISTORICO)
                .all()
            )
            if encontrados:
                self._itens_por_fornecedor_prod[chave] = encontrados
            return encontrados
        return self._itens_por_fornecedor_prod[chave]


def _mesmo_texto(a: Any, b: Any) -> bool:
    """Compara descrições normalizadas; vazio nunca casa (não há o que comparar)."""
    normalizado_a = normalizar_texto(a)
    normalizado_b = normalizar_texto(b)
    return bool(normalizado_a) and normalizado_a == normalizado_b


def _conflito_de_identidade(item, fornecedor, cnpj, historico) -> str:
    """Descrição legível do primeiro conflito da âncora, ou ``""``.

    Regras:
    - âncora EAN: qualquer histórico (mapa de-para ou item já gravado) com o
      MESMO EAN e descrição diferente é divergência. O cProd só é comparado
      quando o histórico é do MESMO fornecedor — códigos diferentes entre
      fornecedores distintos para o mesmo EAN são normais.
    - âncora fornecedor + cProd: a descrição do histórico (mapa ou item) deve
      ser a mesma; o cProd é a própria chave, então não há o que comparar.
    """
    ean = _texto(item.get("ean")) if _ean_valido(item.get("ean")) else ""
    c_prod = normalizar_codigo_produto(item.get("c_prod"))
    descricao = _texto(item.get("descricao"))
    if not descricao:
        return ""

    if ean:
        for mapeamento in historico.mapeamentos_do_ean(ean):
            descricao_padrao = _texto(getattr(mapeamento, "descricao_padrao", ""))
            if descricao_padrao and not _mesmo_texto(descricao, descricao_padrao):
                return (
                    f"EAN {ean} já registrado como {descricao_padrao!r} "
                    f"(cProd {mapeamento.c_prod}) x descrição atual {descricao!r}"
                )
            if (
                c_prod
                and fornecedor is not None
                and normalizar_cnpj(getattr(mapeamento, "cnpj_fornecedor", "")) == cnpj
                and normalizar_codigo_produto(mapeamento.c_prod) != c_prod
            ):
                return (
                    f"EAN {ean} do fornecedor {cnpj} já vinculado ao cProd "
                    f"{mapeamento.c_prod} x cProd atual {c_prod}"
                )
        for descricao_item, c_prod_item, fornecedor_item in historico.itens_do_ean(ean):
            if _texto(descricao_item) and not _mesmo_texto(descricao, descricao_item):
                return (
                    f"EAN {ean} já aparece como {descricao_item!r} "
                    f"(cProd {c_prod_item}) x descrição atual {descricao!r}"
                )
            if (
                c_prod
                and fornecedor is not None
                and fornecedor_item == fornecedor.id
                and normalizar_codigo_produto(c_prod_item) != c_prod
            ):
                return (
                    f"EAN {ean} do fornecedor já vinculado ao cProd "
                    f"{c_prod_item} x cProd atual {c_prod}"
                )
        return ""

    if not c_prod or fornecedor is None:
        return ""
    mapeamento = historico.mapeamento_do_fornecedor(cnpj, c_prod)
    if mapeamento is not None:
        descricao_padrao = _texto(getattr(mapeamento, "descricao_padrao", ""))
        if descricao_padrao and not _mesmo_texto(descricao, descricao_padrao):
            return (
                f"cProd {c_prod} do fornecedor {cnpj} já registrado como "
                f"{descricao_padrao!r} x descrição atual {descricao!r}"
            )
    for descricao_item, _ean_item in historico.itens_do_fornecedor_prod(
        fornecedor.id, c_prod
    ):
        if _texto(descricao_item) and not _mesmo_texto(descricao, descricao_item):
            return (
                f"cProd {c_prod} do fornecedor {cnpj} já aparece como "
                f"{descricao_item!r} x descrição atual {descricao!r}"
            )
    return ""


def _marcar_revisao(db, chave_marcador: str, motivo: str) -> bool:
    """Grava (uma vez) a pendência em ``itens_auditados``; devolve se criou.

    A chave NÃO começa com ``DESC_``, então `_mapa_auditoria_relatorio` não a
    considera: o item continua nos relatórios — a pendência serve só para a
    Auditoria revisar a vinculação. Registro já aprovado pelo admin não é
    reaberto (a decisão humana vence a sinalização automática).
    """
    existente = (
        db.query(ItemAuditado)
        .filter(ItemAuditado.chave_identificacao == chave_marcador)
        .first()
    )
    if existente is not None:
        if not existente.auditado:
            existente.motivo = motivo
        return False
    db.add(
        ItemAuditado(
            chave_identificacao=chave_marcador,
            auditado=False,
            motivo=motivo,
        )
    )
    return True


def _sinalizar_divergencias(
    db,
    item: Dict[str, Any],
    fornecedor,
    cnpj: str,
    dados: Dict[str, Any],
    indice: int,
    historico: _HistoricoIdentidade,
    resultado: Dict[str, Any],
) -> bool:
    """Marca o item para revisão quando a âncora conflita ou o código é ``PRD``.

    Devolve ``True`` quando o item recebeu pelo menos uma sinalização. Não altera
    a identidade do item: quem chama continua gravando EAN/descrição/cProd como
    antes. ``resultado["divergencias"]`` acumula contadores e uma amostra.
    """
    conflito = _conflito_de_identidade(item, fornecedor, cnpj, historico)
    c_prod = normalizar_codigo_produto(item.get("c_prod"))
    motivos: List[Tuple[str, str]] = []
    if conflito:
        motivos.append((MOTIVO_DIVERGENCIA_PRODUTO, conflito))
    if c_prod.startswith(PREFIXO_CODIGO_INTERNO_OMIE):
        motivos.append(
            (
                MOTIVO_REVISAR_VINCULACAO,
                f"cCodigoProduto {c_prod} é o código interno do Omie (PRD...); "
                "confira o vínculo com o cProd do fornecedor.",
            )
        )
    if not motivos:
        return False

    resumo = resultado["divergencias"]
    resumo["total"] += 1
    for motivo, detalhe in motivos:
        resumo[motivo] += 1
        prefixo = "DIV" if motivo == MOTIVO_DIVERGENCIA_PRODUTO else "VINC"
        chave_marcador = f"{prefixo}_{dados['chave']}_{indice}"
        if _marcar_revisao(db, chave_marcador, motivo):
            resumo["marcadores"] += 1
    if len(resumo["itens"]) < LIMITE_AMOSTRA_DIVERGENCIAS:
        ancora = _ancora_do_item(item, getattr(fornecedor, "id", None))
        resumo["itens"].append(
            {
                "chave": dados["chave"],
                "numero": dados["numero"],
                "fornecedor": dados["fornecedor"],
                "item": indice,
                "ancora": _ancora_legivel(ancora),
                "c_prod": item.get("c_prod") or "",
                "ean": item.get("ean") or "",
                "descricao": item.get("descricao") or "",
                "motivos": {motivo: detalhe for motivo, detalhe in motivos},
            }
        )
    return True


# ──────────────────────────────────────────────────────────────────────────────
# Correção da descrição pelo XML da nota (etapa 40)
# ──────────────────────────────────────────────────────────────────────────────
def _tag(elemento) -> str:
    """Nome da tag sem o namespace (``{ns}prod`` -> ``prod``)."""
    return str(getattr(elemento, "tag", "")).rsplit("}", 1)[-1]


def _filho(elemento, nome: str):
    """Primeiro filho com a tag informada (ignora o namespace), ou ``None``.

    Percorrer os filhos em vez de usar ``find`` com caminho evita depender do
    prefixo/namespace do emitente: o XML da Omie pode vir como ``nfeProc``, com
    ``nfe:`` na frente ou sem namespace nenhum (testes e emitentes antigos).
    """
    for filho in elemento or ():
        if _tag(filho) == nome:
            return filho
    return None


def _texto_filho(elemento, nome: str) -> str:
    """Texto de um campo do XML sem espaços e sem entidade HTML/XML."""
    filho = _filho(elemento, nome)
    if filho is None or not filho.text:
        return ""
    return limpar_entidades_texto(filho.text.strip())


def _raiz_do_xml(xml: Any):
    """Raiz do XML a partir do ``cXml`` do Omie (escapado) ou do texto cru.

    O ``cXml`` chega como ``"&lt;?xml ...&gt;"`` (uma camada de escape), então o
    ``html.unescape`` roda UMA vez — o ``&amp;`` interno do xProd continua sendo
    entidade do próprio XML e é decodificado pelo ElementTree (por isso a limpeza
    de ``&amp;amp;`` acontece depois, campo a campo). A declaração de encoding é
    descartada antes do parse de texto porque o ElementTree recusa ``str`` com
    ``encoding=`` (ValueError) e o texto aqui já está decodificado.
    """
    texto = _texto(xml)
    if not texto:
        return None
    tentativas = [texto]
    if "&lt;" in texto:  # cXml escapado pelo Omie
        tentativas.insert(0, html.unescape(texto))
    for tentativa in tentativas:
        for conteudo in (
            tentativa,
            re.sub(r"^\s*<\?xml[^>]*\?>", "", tentativa, count=1),
        ):
            try:
                return ET.fromstring(conteudo)
            except (ET.ParseError, ValueError):
                continue
    return None


def itens_do_xml(xml: Any) -> List[Dict[str, Any]]:
    """Itens (``det/prod``) do XML da NF-e com os campos já normalizados.

    ``cProd`` é o código do produto NO FORNECEDOR e ``xProd`` a descrição do
    fornecedor — é o xProd que substitui a descrição errada do cadastro interno
    do Omie. Os demais campos ficam disponíveis para conferência; o que não
    existe no XML vem zerado (``""``/``0.0``). XML ilegível devolve ``[]``: a
    nota é apenas contada como "sem XML", sem derrubar a sincronização.
    """
    raiz = _raiz_do_xml(xml)
    if raiz is None:
        return []
    itens: List[Dict[str, Any]] = []
    for det in raiz.iter():
        if _tag(det) != "det":
            continue
        prod = _filho(det, "prod")
        if prod is None:
            continue
        itens.append(
            {
                "c_prod": normalizar_codigo_produto(_texto_filho(prod, "cProd")),
                "descricao": _texto_filho(prod, "xProd"),
                "ean": _ean(_texto_filho(prod, "cEAN")),
                "ncm": _digitos(_texto_filho(prod, "NCM"), 8) or "00000000",
                "cfop": _digitos(_texto_filho(prod, "CFOP"), 4) or "0000",
                "unidade": _texto_filho(prod, "uCom"),
                "quantidade": _numero(_texto_filho(prod, "qCom")),
                "valor_unitario": _numero(_texto_filho(prod, "vUnCom")),
                "valor_total": _numero(_texto_filho(prod, "vProd")),
                "desconto": _numero(_texto_filho(prod, "vDesc")),
            }
        )
    return itens


def _codigo_interno(c_prod: Any) -> bool:
    """True para o código INTERNO do Omie (``PRD00001``) — nunca serve de âncora."""
    return normalizar_codigo_produto(c_prod).startswith(PREFIXO_CODIGO_INTERNO_OMIE)


def _mapa_xml_por_item(
    itens_xml: List[Dict[str, Any]], itens_recebimento: List[Dict[str, Any]]
) -> Dict[int, Dict[str, Any]]:
    """Liga cada item do recebimento (1-based) ao item do XML.

    A ordem das tentativas é a âncora de identidade do projeto:

    1. **EAN** — código de barras é global e identifica o produto mesmo quando o
       cProd/descrição do cadastro interno estão errados;
    2. **cProd** — só quando ele é o código do fornecedor (o ``PRD...`` interno
       nunca é âncora, por definição);
    3. **posição** — último recurso, apenas quando as duas listas têm o mesmo
       tamanho (mesma nota, mesma ordem de ``det``). Um par posicional é
       descartado se os dois lados têm EAN válido e diferente — nesse caso não
       há garantia de que seja o mesmo produto.
    """
    mapa: Dict[int, Dict[str, Any]] = {}
    usados: Set[int] = set()

    por_ean: Dict[str, List[int]] = {}
    for posicao, item_xml in enumerate(itens_xml):
        ean = _texto(item_xml.get("ean"))
        if _ean_valido(ean):
            por_ean.setdefault(ean, []).append(posicao)
    for indice, item in enumerate(itens_recebimento, start=1):
        ean = _texto(item.get("ean"))
        if _ean_valido(ean) and por_ean.get(ean):
            posicao = por_ean[ean].pop(0)
            mapa[indice] = itens_xml[posicao]
            usados.add(posicao)

    por_c_prod: Dict[str, List[int]] = {}
    for posicao, item_xml in enumerate(itens_xml):
        if posicao in usados:
            continue
        c_prod = normalizar_codigo_produto(item_xml.get("c_prod"))
        if c_prod:
            por_c_prod.setdefault(c_prod, []).append(posicao)
    for indice, item in enumerate(itens_recebimento, start=1):
        if indice in mapa:
            continue
        c_prod = normalizar_codigo_produto(item.get("c_prod"))
        if not c_prod or _codigo_interno(c_prod) or not por_c_prod.get(c_prod):
            continue
        posicao = por_c_prod[c_prod].pop(0)
        mapa[indice] = itens_xml[posicao]
        usados.add(posicao)

    if len(itens_xml) == len(itens_recebimento):
        for indice, item in enumerate(itens_recebimento, start=1):
            if indice in mapa:
                continue
            posicao = indice - 1
            if posicao in usados:
                continue
            item_xml = itens_xml[posicao]
            ean_item = _texto(item.get("ean"))
            ean_xml = _texto(item_xml.get("ean"))
            if _ean_valido(ean_item) and _ean_valido(ean_xml) and ean_item != ean_xml:
                continue
            mapa[indice] = item_xml
            usados.add(posicao)
    return mapa


def _parear_itens_gravados(
    itens_recebimento: List[Dict[str, Any]], itens_gravados: List[ItemNota]
) -> List[Tuple[int, Dict[str, Any], ItemNota]]:
    """``[(índice, item do recebimento, ItemNota)]`` na ordem da nota.

    A importação grava os itens não bonificados na ordem de ``itensRecebimento``,
    então listas do mesmo tamanho casam por posição (exato). Se algo não bateu
    (nota antiga, item removido), o pareamento cai para EAN e depois cProd: o que
    não casar fica de fora — melhor não corrigir do que trocar a descrição do
    produto errado.
    """
    candidatos = [
        (indice, bruto)
        for indice, bruto in enumerate(itens_recebimento, start=1)
        if isinstance(bruto, dict) and not _item_bonificado(bruto)
    ]
    if len(candidatos) == len(itens_gravados):
        return [
            (indice, bruto, item)
            for (indice, bruto), item in zip(candidatos, itens_gravados)
        ]

    pares: List[Tuple[int, Dict[str, Any], ItemNota]] = []
    disponiveis = list(candidatos)
    for item in itens_gravados:
        achado = None
        ean_item = _texto(item.ean)
        if _ean_valido(ean_item):
            for posicao, (_indice, bruto) in enumerate(disponiveis):
                if _texto(bruto.get("ean")) == ean_item:
                    achado = posicao
                    break
        if achado is None:
            c_prod_item = normalizar_codigo_produto(item.c_prod)
            for posicao, (_indice, bruto) in enumerate(disponiveis):
                if c_prod_item and c_prod_item == normalizar_codigo_produto(
                    bruto.get("c_prod")
                ):
                    achado = posicao
                    break
        if achado is None:
            continue
        indice, bruto = disponiveis.pop(achado)
        pares.append((indice, bruto, item))
    return pares


class _XmlDasNotas:
    """XML das NF-e baixado por ``ListarDocumentos`` e indexado pela chave.

    A listagem não aceita filtro por chave de acesso, então o recorte é o período
    da sincronização: uma única varredura serve todas as notas da execução. Sem
    período (importação do histórico inteiro), a janela é derivada da data de
    emissão da nota — notas do mesmo dia compartilham a chamada.

    A carga é **sob demanda** (só quando alguma nota precisa de conferência) e o
    resultado fica em cache. Falha de API desliga o recurso pelo resto da
    execução (o XML é acessório: a nota e os itens já foram gravados com os dados
    da listagem) e o motivo entra em ``avisos``.
    """

    def __init__(
        self,
        cliente,
        inicio: Optional[date],
        fim: Optional[date],
        *,
        registros_por_pagina: int = REGISTROS_POR_PAGINA,
        maximo_paginas: int = MAXIMO_PAGINAS,
        intervalo: float = 0.0,
    ):
        self.cliente = cliente
        self.inicio = inicio
        self.fim = fim
        self.registros_por_pagina = max(1, int(registros_por_pagina))
        self.maximo_paginas = max(1, int(maximo_paginas))
        self.intervalo = max(0.0, float(intervalo or 0.0))
        self.consultas = 0
        self.avisos: List[str] = []
        self._cache: Dict[Tuple[str, str], Dict[str, str]] = {}
        # O cliente injetado nos testes pode não ter a listagem de documentos:
        # nesse caso a conferência do XML é simplesmente desligada (nenhum aviso,
        # porque o cliente real do Omie sempre expõe ListarDocumentos).
        self.desativado = not callable(
            getattr(cliente, "listar_documentos_entrada", None)
        )

    def _janela(self, emissao: Optional[date]) -> Optional[Tuple[date, date]]:
        """Período usado no ``ListarDocumentos`` para localizar a nota."""
        if self.inicio is not None and self.fim is not None:
            return self.inicio, self.fim
        if isinstance(emissao, date):
            return emissao, emissao
        return None

    def _carregar(self, janela: Tuple[date, date]) -> Dict[str, str]:
        """Baixa (uma vez) todas as páginas da janela e indexa por chave."""
        chave_janela = (janela[0].isoformat(), janela[1].isoformat())
        if chave_janela in self._cache:
            return self._cache[chave_janela]

        indexado: Dict[str, str] = {}
        for pagina in range(1, self.maximo_paginas + 1):
            try:
                documentos = self.cliente.listar_documentos_entrada(
                    janela[0],
                    janela[1],
                    pagina=pagina,
                    registros_por_pagina=self.registros_por_pagina,
                )
            except (ErroOmie, ValueError, TypeError) as exc:
                self.desativado = True
                self.avisos.append(
                    "XML da nota não conferido (ListarDocumentos "
                    f"{janela[0]:%d/%m/%Y}..{janela[1]:%d/%m/%Y}): {exc}"
                )
                break
            self.consultas += 1
            if not documentos:
                break
            novos = 0
            for documento in documentos:
                if not isinstance(documento, dict):
                    continue
                xml = _texto(documento.get("cXml") or documento.get("cXmlNfe"))
                chave = chave_do_xml(xml) or _digitos(
                    documento.get("nChave") or documento.get("cChave")
                )
                if chave and chave not in indexado:
                    indexado[chave] = xml
                    novos += 1
            # Página incompleta (fim da listagem) ou página repetida (a API
            # ignorou a paginação): continuar só gastaria chamada.
            if len(documentos) < self.registros_por_pagina or not novos:
                break
            if pagina == self.maximo_paginas:
                logger.warning(
                    "Omie ListarDocumentos: limite de %s páginas atingido; "
                    "alguns XMLs podem não ter sido conferidos.",
                    self.maximo_paginas,
                )
                break
            if self.intervalo:
                time.sleep(self.intervalo)

        self._cache[chave_janela] = indexado
        return indexado

    def xml_da_nota(self, chave: str, emissao: Optional[date] = None) -> str:
        """``cXml`` da nota (texto cru) ou ``""`` quando não foi possível baixar."""
        if self.desativado or not _texto(chave):
            return ""
        janela = self._janela(emissao)
        if janela is None:
            return ""
        return self._carregar(janela).get(_texto(chave), "")


# ──────────────────────────────────────────────────────────────────────────────
# Credenciais (armazenadas cifradas em empresas.omie_app_key/omie_app_secret)
# ──────────────────────────────────────────────────────────────────────────────
def credenciais_da_empresa(empresa) -> Tuple[str, str, str]:
    """Credenciais Omie da loja: ``(app_key, app_secret, origem)``.

    A loja configurada (colunas cifradas) tem prioridade; quando ela ainda não
    tem credencial, vale o par ``OMIE_APP_KEY``/``OMIE_APP_SECRET`` do ``.env`` —
    é o modo usado pelo script de teste e por instalações de uma loja só. Sem
    nenhum dos dois a sincronização para com :class:`OmieNaoConfigurado` (nunca
    tenta chamar a API sem credencial).

    ``origem`` é ``"empresa"`` ou ``"ambiente"`` e vai para o relatório, para o
    operador saber de onde veio a credencial usada naquela execução.
    """
    if empresa is not None:
        chave = descriptografar(getattr(empresa, "omie_app_key", "") or "")
        segredo = descriptografar(getattr(empresa, "omie_app_secret", "") or "")
        if chave and segredo:
            return chave, segredo, "empresa"

    chave = (os.getenv("OMIE_APP_KEY") or "").strip()
    segredo = (os.getenv("OMIE_APP_SECRET") or "").strip()
    if chave and segredo:
        return chave, segredo, "ambiente"

    raise OmieNaoConfigurado(
        "Credenciais Omie ausentes: grave as da loja "
        "(testar_omie_sync.py --gravar-credenciais) ou defina OMIE_APP_KEY e "
        "OMIE_APP_SECRET no .env."
    )


def definir_credenciais(db, empresa, app_key: str, app_secret: str, commit: bool = True):
    """Grava as credenciais Omie da loja CIFRADAS (nunca em texto puro).

    O ``app_secret`` é o segredo do aplicativo Omie: com ele qualquer pessoa
    lança/consulta notas em nome da loja. As colunas recebem o token do Fernet
    (``fernet:v1:...``); a chave de criptografia fica fora do banco
    (``OMIE_ENC_KEY``/``NFE_SECRET_KEY``/``.secret_key``). Reexecutar com as
    mesmas credenciais é idempotente (o texto já cifrado não é re-cifrado).
    """
    app_key = (app_key or "").strip()
    app_secret = (app_secret or "").strip()
    if not app_key or not app_secret:
        raise ErroSincronizacao(
            "App Key e App Secret do Omie são obrigatórios para gravar a credencial."
        )
    empresa.omie_app_key = criptografar(app_key)
    empresa.omie_app_secret = criptografar(app_secret)
    if commit:
        db.commit()
    return empresa


# ──────────────────────────────────────────────────────────────────────────────
# Apoio da sincronização
# ──────────────────────────────────────────────────────────────────────────────
def _periodo_da_sincronizacao(
    data_inicio: Any, data_fim: Any, dias: Optional[int]
) -> Tuple[Optional[date], Optional[date]]:
    """Período efetivo: datas explícitas > ``dias`` para trás > histórico inteiro.

    ``dias=None`` (sem datas) faz a importação inicial de toda a etapa — foi o
    recorte usado no diagnóstico (743 notas faturadas, 158 recebidas, 990
    conferidas). O script de linha de comando expõe as três formas.
    """
    if data_inicio is not None or data_fim is not None:
        if data_inicio is None or data_fim is None:
            raise ErroSincronizacao(
                "Informe as duas datas (início e fim) ou nenhuma — sem datas a "
                "sincronização percorre o histórico inteiro."
            )
        inicio = _data(data_inicio, "data inicial")
        fim = _data(data_fim, "data final")
        if inicio > fim:
            raise ErroSincronizacao("A data inicial não pode ser posterior à final.")
        return inicio, fim
    if dias is None:
        return None, None
    try:
        janela = int(dias)
    except (TypeError, ValueError) as exc:
        raise ErroSincronizacao(
            f"Janela de dias inválida: {dias!r} (use um inteiro >= 1)."
        ) from exc
    if janela < 1:
        raise ErroSincronizacao(
            "Janela de dias inválida: use >= 1, ou dias=None para o histórico inteiro."
        )
    fim = date.today()
    return fim - timedelta(days=janela - 1), fim


def _commit(db, confirmar: bool) -> None:
    """Grava de fato (modo normal) ou apenas valida no banco (simulação)."""
    if confirmar:
        db.commit()
    else:
        db.flush()


def _contar_itens_sem_marca(db, notas_ids: Iterable[int]) -> int:
    """Itens das notas importadas nesta execução que ficaram com ``marca`` NULL.

    A marca entra na chave do grupo dos relatórios (Produto + Marca): o mesmo
    produto com marca e sem marca vira duas linhas ("... (2)"). O importador de
    XML sempre recalcula as marcas no fim; aqui a contagem alimenta um AVISO no
    resumo da sincronização (e o ``backfill_marcas.py`` corrige a base) em vez de
    esconder o problema. Notas antigas não entram na conta — só as desta execução.
    """
    ids = [identificador for identificador in notas_ids if identificador is not None]
    if not ids:
        return 0
    try:
        return db.query(ItemNota.id).filter(
            ItemNota.nota_id.in_(ids), ItemNota.marca.is_(None)
        ).count()
    except Exception:  # noqa: BLE001 - a contagem é acessório do resumo
        return 0


def _fornecedor_da_nota(
    db, cnpj: str, nome: str, cache: Dict[str, Fornecedor], resultado: Dict[str, Any]
) -> Optional[Fornecedor]:
    """Fornecedor do CNPJ da nota, criando/reaproveitando a linha.

    A nota fiscal traz o CNPJ do emitente; é ele que identifica o fornecedor no
    sistema (o ``nIdFornecedor`` do Omie é específico da conta e não serve como
    chave). O cache evita uma consulta por item/nota quando o mesmo fornecedor
    aparece em várias notas da mesma execução.
    """
    if not cnpj:
        return None
    fornecedor = cache.get(cnpj)
    if fornecedor is not None:
        return fornecedor

    fornecedor = db.query(Fornecedor).filter(Fornecedor.cnpj == cnpj).first()
    if fornecedor is None:
        fornecedor = Fornecedor(cnpj=cnpj, nome=nome or "Fornecedor Desconhecido")
        db.add(fornecedor)
        db.flush()
        resultado["fornecedores_novos"] += 1
    elif nome and not (fornecedor.nome or "").strip():
        fornecedor.nome = nome
        resultado["fornecedores_atualizados"] += 1
    cache[cnpj] = fornecedor
    return fornecedor


# ──────────────────────────────────────────────────────────────────────────────
# Aplicação da correção (descrição do fornecedor x cadastro interno do Omie)
# ──────────────────────────────────────────────────────────────────────────────
def _fonte_da_descricao(etapa: Any) -> str:
    """Rótulo gravado em ``itens_nota.descricao_origem`` e ``itens_auditados.fonte``."""
    etapa = _texto(etapa)
    return f"XML etapa {etapa}" if etapa else "XML"


def _registrar_correcao_descricao(
    db,
    chave: str,
    indice: int,
    antes: str,
    depois: str,
    fonte: str,
    resultado: Dict[str, Any],
) -> None:
    """Grava a trilha da correção em ``itens_auditados`` (uma linha por item).

    A chave começa com ``XML_`` (nunca ``DESC_``) e ``auditado=True``: é registro
    histórico, não pendência — o item continua nos relatórios e a Auditoria não
    precisa aprovar nada. Reexecutar a correção atualiza a mesma linha em vez de
    criar duplicata.
    """
    marcador = f"{PREFIXO_MARCADOR_DESCRICAO}_{chave}_{indice}"
    registro = (
        db.query(ItemAuditado)
        .filter(ItemAuditado.chave_identificacao == marcador)
        .first()
    )
    if registro is None:
        registro = ItemAuditado(
            chave_identificacao=marcador,
            auditado=True,
            motivo=MOTIVO_DESCRICAO_XML,
        )
        db.add(registro)
        resultado["descricoes"]["marcadores"] += 1
    registro.descricao_antes = antes or None
    registro.descricao_depois = depois or None
    registro.fonte = fonte
    registro.registrado_em = datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atualizar_memoria_descricao(mapa_aprendizado, cnpj, c_prod, descricao) -> bool:
    """Grava a descrição do fornecedor na memória de-para (nunca se for ``manual``).

    Sem isso a próxima nota do mesmo fornecedor+cProd voltaria com a descrição
    errada: ``aplicar_identidade`` usa a memória como texto canônico. A correção
    manual feita na Auditoria vence a automática (``origem="manual"``).
    """
    chave = (normalizar_cnpj(cnpj), normalizar_codigo_produto(c_prod))
    mapeamento = (mapa_aprendizado or {}).get(chave)
    if mapeamento is None or not _texto(descricao):
        return False
    if str(getattr(mapeamento, "origem", "") or "").strip().lower() == "manual":
        return False
    if _mesmo_texto(getattr(mapeamento, "descricao_padrao", ""), descricao):
        return False
    mapeamento.descricao_padrao = descricao
    mapeamento.atualizado_em = datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )
    return True


def _tem_marcador_pendente(db, chave: str) -> bool:
    """True quando a nota já tem pendência ``DIV_``/``VINC_`` registrada."""
    if not _texto(chave):
        return False
    filtro = ItemAuditado.chave_identificacao.like(f"DIV_{chave}_%") | (
        ItemAuditado.chave_identificacao.like(f"VINC_{chave}_%")
    )
    return db.query(ItemAuditado.id).filter(filtro).first() is not None


def _conferir_xml_na_importacao(
    etapa: Any, itens_recebimento: List[Dict[str, Any]], sinalizado: bool
) -> bool:
    """True quando a nota NOVA deve ser conferida contra o XML.

    Na etapa 40 (Faturado) o XML está disponível para todas as notas e a
    conferência é o padrão. Em 60/80 só as notas com divergência sinalizada ou
    com código interno ``PRD...`` são conferidas.
    """
    if _texto(etapa) == ETAPA_FATURADO:
        return True
    if sinalizado:
        return True
    return any(_codigo_interno(item.get("c_prod")) for item in itens_recebimento)


def _corrigir_descricoes_da_nota(
    db,
    chave: str,
    etapa: Any,
    itens_recebimento: List[Dict[str, Any]],
    pares: List[Tuple[int, Dict[str, Any], ItemNota]],
    xml: Any,
    cnpj: str,
    mapa_aprendizado,
    resultado: Dict[str, Any],
) -> int:
    """Aplica o ``xProd`` do XML nos itens com descrição divergente.

    Regras:
    - item do XML localizado pela âncora (EAN, depois cProd; o ``PRD...`` interno
      nunca é âncora) — ver :func:`_mapa_xml_por_item`;
    - descrição já igual à do XML (comparação normalizada) não é reescrita;
    - item cuja descrição já veio do XML (``descricao_origem``) não é reescrito de
      novo: em 60/80 o texto da listagem serve só de conferência;
    - o texto anterior fica em ``itens_nota.descricao_produto_anterior`` e cada
      mudança vira uma linha em ``itens_auditados`` (antes -> depois, fonte e
      data/hora);
    - a memória de-para acompanha (exceto a ``manual``), para o mesmo
      fornecedor+cProd não voltar com a descrição errada.

    Devolve quantos itens foram corrigidos.
    """
    itens_xml = itens_do_xml(xml)
    if not itens_xml:
        resultado["descricoes"]["sem_xml"] += 1
        return 0
    resultado["descricoes"]["xmls_lidos"] += 1
    mapa = _mapa_xml_por_item(itens_xml, itens_recebimento)
    fonte = _fonte_da_descricao(etapa)
    corrigidos = 0
    for indice, bruto, item in pares:
        nova = _texto((mapa.get(indice) or {}).get("descricao"))
        if not nova:
            resultado["descricoes"]["nao_encontrados"] += 1
            continue
        atual = _texto(item.descricao_produto)
        if _mesmo_texto(nova, atual):
            # Já está com a descrição do fornecedor: nada muda (no 60/80 o texto
            # da API é só conferência).
            resultado["descricoes"]["conferidas"] += 1
            continue
        if _texto(getattr(item, "descricao_origem", "")).startswith(
            PREFIXO_ORIGEM_XML
        ):
            resultado["descricoes"]["mantidas"] += 1
            continue
        item.descricao_produto = nova
        if not _texto(getattr(item, "descricao_produto_anterior", "")):
            item.descricao_produto_anterior = atual or None
        item.descricao_origem = fonte
        _registrar_correcao_descricao(
            db, chave, indice, atual, nova, fonte, resultado
        )
        if _atualizar_memoria_descricao(
            mapa_aprendizado, cnpj, bruto.get("c_prod"), nova
        ):
            resultado["descricoes"]["memoria_atualizada"] += 1
        resultado["descricoes"]["corrigidas"] += 1
        if len(resultado["descricoes"]["eventos"]) < LIMITE_AMOSTRA_DESCRICOES:
            resultado["descricoes"]["eventos"].append(
                {
                    "chave": chave,
                    "item": indice,
                    "c_prod": _texto(bruto.get("c_prod")),
                    "ean": _texto(bruto.get("ean")),
                    "antes": atual,
                    "depois": nova,
                    "fonte": fonte,
                }
            )
        corrigidos += 1
    return corrigidos


def _corrigir_nota_existente(
    db,
    nota: NotaFiscal,
    dados: Dict[str, Any],
    etapa: Any,
    xmls: "_XmlDasNotas",
    mapa_aprendizado,
    resultado: Dict[str, Any],
) -> int:
    """Corrige a descrição de uma nota JÁ importada (reexecução da sincronização).

    A deduplicação por chave mantém a nota intacta; a exceção é a correção de
    descrição. Baixar o XML de toda nota repetida a cada execução seria caro e sem
    efeito, então só entram as notas com código interno ``PRD...`` nos itens ou com
    pendência DIV_/VINC_ já sinalizada — exatamente as que precisam de conserto.
    """
    itens = (
        db.query(ItemNota)
        .filter(ItemNota.nota_id == nota.id)
        .order_by(ItemNota.id)
        .all()
    )
    if not itens:
        return 0
    itens_recebimento = dados.get("itens") or []
    precisa = any(_codigo_interno(item.c_prod) for item in itens) or any(
        _codigo_interno(item.get("c_prod")) for item in itens_recebimento
    )
    if not precisa and not _tem_marcador_pendente(db, dados["chave"]):
        return 0
    xml = xmls.xml_da_nota(dados["chave"], dados.get("emissao"))
    if not xml:
        resultado["descricoes"]["sem_xml"] += 1
        return 0
    pares = _parear_itens_gravados(itens_recebimento, itens)
    if not pares:
        return 0
    return _corrigir_descricoes_da_nota(
        db,
        dados["chave"],
        etapa,
        itens_recebimento,
        pares,
        xml,
        dados.get("cnpj", ""),
        mapa_aprendizado,
        resultado,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Importação de um recebimento
# ──────────────────────────────────────────────────────────────────────────────
def _importar_recebimento(
    db,
    empresa,
    recebimento: Dict[str, Any],
    resultado: Dict[str, Any],
    vistas: Set[str],
    fornecedores: Dict[str, Fornecedor],
    mapa_aprendizado: Dict[Tuple[str, str], Any],
    historico: _HistoricoIdentidade,
    confirmar: bool,
    notas_novas: List[int],
    etapa: Any,
    xmls: "_XmlDasNotas",
    corrigir_descricoes_xml: bool = True,
) -> str:
    """Grava UM recebimento (cabeçalho + itens) e devolve o contador afetado.

    O retorno é o nome do contador que o laço principal incrementa:
    ``"novas"``, ``"duplicadas"``, ``"canceladas"`` ou ``"ignoradas"``. Erro de
    dados (data inválida, falha de gravação) sobe como exceção para o laço, que
    desfaz a transação daquela nota e continua nas seguintes — a importação de
    1.800 notas não pode parar por causa de uma.

    ``notas_novas`` recebe o id de cada nota criada nesta execução; é o recorte
    usado no fim para contar os itens que ficaram sem marca.

    Antes de aplicar a identidade aprendida, cada item é comparado com o
    histórico por :func:`_sinalizar_divergencias`: divergência de descrição/
    cProd na mesma âncora e código interno ``PRD...`` viram pendências na
    Auditoria. A identidade gravada NÃO muda por causa disso.

    Depois de gravar os itens, a nota é conferida contra o XML da NF-e
    (``xmls``): na etapa 40 sempre, em 60/80 só quando há divergência/``PRD``.
    A descrição divergente vira o ``xProd`` do fornecedor (ver
    :func:`_corrigir_descricoes_da_nota`). Nota já existente (dedupe) também
    passa pela conferência por :func:`_corrigir_nota_existente`.
    """
    dados = _dados_do_recebimento(recebimento)

    if not dados["chave"]:
        cabec = recebimento.get("cabec") if isinstance(recebimento.get("cabec"), dict) else {}
        resultado["erros"].append(
            "recebimento sem chave de acesso (nIdReceb="
            f"{cabec.get('nIdReceb')}); ignorado para não duplicar nota"
        )
        return "ignoradas"

    if dados["chave"] in vistas:
        # Mesma nota em mais de uma etapa (ou repetida na paginação): importa uma vez.
        return "duplicadas"
    vistas.add(dados["chave"])

    if dados["cancelada"]:
        return "canceladas"

    existente = (
        db.query(NotaFiscal)
        .filter(NotaFiscal.chave_acesso == dados["chave"])
        .first()
    )
    if existente is not None:
        if corrigir_descricoes_xml:
            _corrigir_nota_existente(
                db,
                existente,
                dados,
                etapa,
                xmls,
                mapa_aprendizado,
                resultado,
            )
            # A correção mexe em linha já gravada: persiste já (ou fica pendente no
            # modo simulação, como todo o resto).
            _commit(db, confirmar)
        return "duplicadas"

    if dados["emissao"] is None:
        raise ErroSincronizacao(
            f"nota {dados['chave']} sem data de emissão legível; não gravada"
        )

    fornecedor = _fornecedor_da_nota(
        db, dados["cnpj"], dados["fornecedor"], fornecedores, resultado
    )
    nota = NotaFiscal(
        chave_acesso=dados["chave"],
        numero_nota=dados["numero"] or None,
        serie=dados["serie"] or "1",
        data_emissao=dados["emissao"],
        valor_total=dados["valor"],
        empresa_id=getattr(empresa, "id", None),
    )
    db.add(nota)
    # Commita já o cabeçalho (como no import_xmls): a nota existe no Omie mesmo
    # que um item isolado falhe, então ela não pode "sumir" numa nova tentativa.
    _commit(db, confirmar)
    if getattr(nota, "id", None) is not None:
        notas_novas.append(nota.id)

    itens_gravados = 0
    nota_sinalizada = False
    # (índice na nota, item do recebimento, linha gravada) — base da conferência XML.
    pares: List[Tuple[int, Dict[str, Any], ItemNota]] = []
    for indice, bruto in enumerate(dados["itens"], start=1):
        if _item_bonificado(bruto):
            resultado["bonificados"] += 1
            continue

        # Só sinaliza (itens_auditados); a identidade abaixo continua igual.
        if _sinalizar_divergencias(
            db,
            bruto,
            fornecedor,
            dados["cnpj"],
            dados,
            indice,
            historico,
            resultado,
        ) and not nota_sinalizada:
            resultado["divergencias"]["notas"] += 1
            nota_sinalizada = True

        chave_aprendizado = None
        mapeamento = None
        if bruto["c_prod"] and dados["cnpj"]:
            chave_aprendizado = (
                dados["cnpj"],
                normalizar_codigo_produto(bruto["c_prod"]),
            )
            mapeamento = mapa_aprendizado.get(chave_aprendizado)

        # A descrição do XML do Omie também passa pela limpeza (a mesma aplicada
        # na leitura do item), para a identidade e a memória nunca guardarem "&amp;".
        ean_xml = bruto["ean"]
        desc_xml = limpar_entidades_texto(bruto["descricao"])
        ncm_xml = bruto["ncm"]
        ean, desc, ncm = ean_xml, desc_xml, ncm_xml
        if mapeamento is not None:
            resultado["mapeamentos_usados"] += 1
            ean, desc, ncm = aplicar_identidade(mapeamento, ean, desc, ncm)

        embalagem = interpretar_embalagem(bruto["unidade"], desc)
        embalagem = aplicar_embalagem_aprendida(embalagem, mapeamento)
        quantidade_base, valor_unitario_base = normalizar_quantidades(
            bruto["quantidade"], bruto["valor_unitario"], embalagem
        )

        if chave_aprendizado:
            mapeamento, criado, _avisos = registrar_aprendizado(
                db,
                chave_aprendizado[0],
                chave_aprendizado[1],
                ean=ean_xml,
                descricao=desc_xml,
                ncm=ncm_xml,
                embalagem=embalagem,
                mapeamento=mapeamento,
                commit=False,
            )
            mapa_aprendizado[chave_aprendizado] = mapeamento
            if criado:
                resultado["mapeamentos_criados"] += 1

        # Valores CRUS do Omie nas colunas principais (auditoria/reprocessamento);
        # as conversões ficam em *_base, exatamente como no importador de XML.
        item_gravado = ItemNota(
            nota_id=nota.id,
            fornecedor_id=fornecedor.id if fornecedor is not None else None,
            ean=ean,
            descricao_produto=desc,
            quantidade=bruto["quantidade"],
            valor_unitario=bruto["valor_unitario"],
            valor_total=bruto["valor_total"],
            ncm=ncm or "00000000",
            cfop=bruto["cfop"] or "0000",
            c_prod=bruto["c_prod"] or None,
            unidade_compra=bruto["unidade"] or None,
            qtd_por_embalagem=float(embalagem.get("qtd_por_embalagem") or 1.0),
            unidade_base=str(embalagem.get("unidade_base") or "UN"),
            quantidade_base=quantidade_base,
            valor_unitario_base=valor_unitario_base,
        )
        db.add(item_gravado)
        pares.append((indice, bruto, item_gravado))
        itens_gravados += 1

    # ── Conferência contra o XML da nota (passo 3) ────────────────────────────
    # Roda depois de os itens existirem (a linha é atualizada, não recriada) e
    # antes do commit final. Sem XML, com XML ilegível ou quando a nota não
    # precisa de conferência, o resumo apenas conta — nada muda no item.
    if (
        corrigir_descricoes_xml
        and pares
        and _conferir_xml_na_importacao(etapa, dados["itens"], nota_sinalizada)
    ):
        xml = xmls.xml_da_nota(dados["chave"], dados["emissao"])
        if xml:
            _corrigir_descricoes_da_nota(
                db,
                dados["chave"],
                etapa,
                dados["itens"],
                pares,
                xml,
                dados["cnpj"],
                mapa_aprendizado,
                resultado,
            )
        else:
            resultado["descricoes"]["sem_xml"] += 1

    _commit(db, confirmar)
    resultado["itens"] += itens_gravados
    if not itens_gravados:
        # Só bonificação/doação: a nota é mantida (dedupe) mas não entra em preço.
        resultado["notas_sem_itens"] += 1

    if len(resultado["notas_novas"]) < LIMITE_AMOSTRA:
        resultado["notas_novas"].append(
            {
                "chave": dados["chave"],
                "numero": dados["numero"],
                "serie": dados["serie"],
                "data": dados["emissao"].isoformat(),
                "valor": dados["valor"],
                "fornecedor": dados["fornecedor"],
                "itens": itens_gravados,
            }
        )
    return "novas"




# ──────────────────────────────────────────────────────────────────────────────
# Entrada pública
# ──────────────────────────────────────────────────────────────────────────────
def sincronizar_recebimentos(
    db,
    empresa,
    *,
    data_inicio: Any = None,
    data_fim: Any = None,
    dias: Optional[int] = DIAS_PADRAO,
    etapas: Iterable[str] = ETAPAS_SYNC,
    registros_por_pagina: int = REGISTROS_POR_PAGINA,
    maximo_paginas: int = MAXIMO_PAGINAS,
    intervalo: float = INTERVALO_ENTRE_PAGINAS,
    cliente: Optional[OmieClient] = None,
    confirmar: bool = True,
    recalcular_marcas_itens: bool = True,
    atualizar_sync_em: bool = True,
    corrigir_descricoes_xml: bool = True,
) -> Dict[str, Any]:
    """Sincroniza os recebimentos do Omie de UMA loja com o banco.

    Fluxo: lê as credenciais da loja (ou do ``.env``), percorre as etapas
    40/60/80 paginando até o fim, grava cada nota que ainda não existe (dedupe
    pela chave de acesso), cria/atualiza fornecedores e itens com os valores
    crus do Omie e, ao final, recalcula as marcas a partir do cache local.

    Args:
        db: sessão SQLAlchemy. O chamador é dono da sessão: em caso de erro
            dentro de uma nota a transação dela é desfeita aqui, mas o rollback
            geral (exceções de credencial/período) é de responsabilidade de quem
            chamou.
        empresa: instância de ``Empresa`` já persistida.
        data_inicio/data_fim: período por data de emissão da NF-e (``date``,
            ``datetime`` ou texto). Informe as duas ou nenhuma.
        dias: janela para trás quando não há datas (padrão ``DIAS_PADRAO``).
            ``None`` = histórico inteiro da etapa (importação inicial).
        etapas: ``cEtapa`` a importar (padrão 40/60/80).
        registros_por_pagina/maximo_paginas: tamanho de página e teto de
            segurança da listagem.
        intervalo: pausa entre páginas (evita HTTP 425 por rajada).
        cliente: cliente já construído (usado nos testes, sem chamar a API).
        confirmar: ``False`` executa tudo e desfaz no fim (``--simular``);
            útil para conferir o volume antes da importação real.
        recalcular_marcas_itens: recalcula ``itens_nota.marca`` pelo cache local
            quando entrou nota nova (mesma etapa do importador de XML).
        atualizar_sync_em: grava ``empresas.omie_sync_em`` no fim.
        corrigir_descricoes_xml: confere a descrição dos itens contra o ``xProd``
            do XML da nota (``ListarDocumentos``). Na etapa 40 vale para toda nota
            nova; em 60/80 e nas notas já gravadas, só para as que têm
            divergência sinalizada ou código interno ``PRD...``. ``False`` desliga
            a chamada extra à API (usado por scripts/diagnósticos).

    Returns:
        Dict com os contadores da execução: ``novas``, ``duplicadas``,
        ``canceladas``, ``ignoradas``, ``itens``, ``bonificados``,
        ``notas_sem_itens``, ``fornecedores_novos``, ``fornecedores_atualizados``,
        ``mapeamentos_criados``, ``mapeamentos_usados``, ``etapas`` (total por
        etapa), ``notas_novas`` (amostra), ``itens_sem_marca`` (itens das notas
        importadas que ficaram sem marca), ``divergencias`` (itens sinalizados
        para revisão: ``total``, ``notas``, ``divergencia_produto``,
        ``revisar_vinculacao``, ``marcadores`` e a amostra ``itens``),
        ``descricoes`` (correção pelo XML: ``corrigidas``, ``conferidas``,
        ``mantidas``, ``sem_xml``, ``xmls_lidos``, ``nao_encontrados``,
        ``marcadores``, ``memoria_atualizada``, ``consultas`` e a amostra
        ``eventos``), ``avisos`` (lista legível de avisos,
        ex.: itens sem marca, XML indisponível), ``erros`` (lista legível),
        ``periodo``, ``origem_credenciais`` e ``simulado``.

    Raises:
        ErroSincronizacao: empresa/período/etapa inválidos.
        ErroCredenciais: chave de criptografia ausente ou credencial ilegível.
        OmieNaoConfigurado: nenhuma credencial disponível.
    """

    if empresa is None:
        raise ErroSincronizacao("Empresa não informada para a sincronização.")

    inicio, fim = _periodo_da_sincronizacao(data_inicio, data_fim, dias)

    etapas_limpas: List[str] = []
    for etapa in etapas or ():
        valor = str(etapa or "").strip()
        if valor and valor not in etapas_limpas:
            etapas_limpas.append(valor)
    if not etapas_limpas:
        raise ErroSincronizacao(
            "Nenhuma etapa informada: use 40 (Faturado), 60 (Recebido) e/ou "
            "80 (Conferido)."
        )

    origem_credenciais = "informado"
    if cliente is None:
        app_key, app_secret, origem_credenciais = credenciais_da_empresa(empresa)
        cliente = OmieClient(app_key, app_secret)

    resultado: Dict[str, Any] = {
        "empresa_id": getattr(empresa, "id", None),
        "empresa": (
            _texto(getattr(empresa, "nome_loja", ""))
            or _texto(getattr(empresa, "razao_social", ""))
            or _texto(getattr(empresa, "cnpj", ""))
        ),
        "cnpj": normalizar_cnpj(getattr(empresa, "cnpj", "")),
        "periodo": (inicio.isoformat(), fim.isoformat()) if inicio and fim else None,
        "etapas": {},
        "recebimentos": 0,
        "novas": 0,
        "duplicadas": 0,
        "canceladas": 0,
        "ignoradas": 0,
        "itens": 0,
        "bonificados": 0,
        "notas_sem_itens": 0,
        "fornecedores_novos": 0,
        "fornecedores_atualizados": 0,
        "mapeamentos_criados": 0,
        "mapeamentos_usados": 0,
        "notas_novas": [],
        "itens_sem_marca": 0,
        # Resumo da sinalização de identidade (não altera nada): ``total`` conta
        # itens sinalizados, os motivos contam sinalizações (um item pode ter os
        # dois) e ``marcadores`` conta linhas novas em itens_auditados.
        "divergencias": {
            "total": 0,
            "notas": 0,
            "divergencia_produto": 0,
            "revisar_vinculacao": 0,
            "marcadores": 0,
            "itens": [],
        },
        # Correção da descrição pelo XML (passo 3). ``corrigidas`` conta itens
        # reescritos; ``conferidas`` os que já estavam iguais ao xProd;
        # ``mantidas`` os que já tinham vindo de um XML anterior (60/80 só
        # conferem); ``sem_xml`` as notas cujo XML não foi baixado.
        "descricoes": {
            "corrigidas": 0,
            "conferidas": 0,
            "mantidas": 0,
            "sem_xml": 0,
            "xmls_lidos": 0,
            "nao_encontrados": 0,
            "marcadores": 0,
            "memoria_atualizada": 0,
            "consultas": 0,
            "eventos": [],
        },
        "avisos": [],
        "erros": [],
        "origem_credenciais": origem_credenciais,
        "simulado": not confirmar,
        "sincronizado_em": None,
    }

    vistas: Set[str] = set()
    fornecedores: Dict[str, Fornecedor] = {}
    # Ids das notas criadas nesta execução (recorte do aviso de "item sem marca").
    notas_novas: List[int] = []
    # Memória de-para carregada uma vez para a execução inteira, como no import_xmls.
    mapa_aprendizado = {
        (m.cnpj_fornecedor, m.c_prod): m
        for m in db.query(MapeamentoFornecedorProduto).all()
    }
    # Índices de identidade para a sinalização de divergências (só leitura).
    historico = _HistoricoIdentidade(db, mapa_aprendizado)
    # XML das notas (ListarDocumentos): baixado sob demanda e uma vez por período,
    # para conferir/corrigir a descrição do produto na etapa 40.
    xmls = _XmlDasNotas(
        cliente,
        inicio,
        fim,
        registros_por_pagina=registros_por_pagina,
        maximo_paginas=maximo_paginas,
        intervalo=intervalo,
    )

    for etapa in etapas_limpas:
        rotulo = NOME_ETAPA.get(etapa, etapa)
        try:
            lote = cliente.listar_todos_recebimentos(
                inicio,
                fim,
                registros_por_pagina=registros_por_pagina,
                maximo_paginas=maximo_paginas,
                etapa=etapa,
                intervalo=intervalo,
            )
        except ErroOmie as exc:
            resultado["etapas"][etapa] = {
                "rotulo": rotulo,
                "recebimentos": 0,
                "erro": str(exc),
            }
            resultado["erros"].append(f"etapa {etapa} ({rotulo}): {exc}")
            if _bloqueado(exc):
                # A Omie cortou a sequência; insistir nas próximas etapas só
                # aumentaria o bloqueio — a próxima execução continua daqui.
                break
            continue

        resultado["etapas"][etapa] = {"rotulo": rotulo, "recebimentos": len(lote)}

        for recebimento in lote:
            resultado["recebimentos"] += 1
            try:
                contador = _importar_recebimento(
                    db,
                    empresa,
                    recebimento,
                    resultado,
                    vistas,
                    fornecedores,
                    mapa_aprendizado,
                    historico,
                    confirmar,
                    notas_novas,
                    etapa,
                    xmls,
                    corrigir_descricoes_xml,
                )
            except Exception as exc:  # noqa: BLE001 - uma nota não derruba a importação
                db.rollback()
                cabec = (
                    recebimento.get("cabec")
                    if isinstance(recebimento.get("cabec"), dict)
                    else {}
                )
                resultado["erros"].append(
                    f"etapa {etapa} chave {cabec.get('cChaveNFe') or '?'}: {exc}"
                )
                continue
            resultado[contador] += 1

    if confirmar:
        # A marca entra na chave do grupo dos relatórios e depende da descrição:
        # recalculada quando entrou nota nova OU quando alguma descrição foi
        # corrigida pelo XML (a marca do item corrigido pode ter mudado).
        if (
            resultado["novas"] or resultado["descricoes"]["corrigidas"]
        ) and recalcular_marcas_itens:
            try:
                resultado["marcas"] = recalcular_marcas(db, apenas_cache=True)
            except Exception as exc:  # noqa: BLE001 - marca é acessório do relatório
                resultado["erros"].append(f"marcas: {exc}")
        # A marca entra na chave do grupo dos relatórios: se sobrou item sem marca,
        # o mesmo produto aparece em duas linhas ("... (2)"). Avisa sem marcar a
        # sincronização como falha (o import em si deu certo) — a correção é o
        # backfill_marcas.py, que usa a memória de marcas antes da heurística.
        resultado["itens_sem_marca"] = _contar_itens_sem_marca(db, notas_novas)
        if resultado["itens_sem_marca"]:
            resultado["avisos"].append(
                f"{resultado['itens_sem_marca']} item(ns) importado(s) sem marca — "
                'rode "python backfill_marcas.py" (a marca entra na chave do grupo '
                "nos relatórios e o item sem marca abre linha duplicada)."
            )
        if atualizar_sync_em and getattr(empresa, "id", None):
            empresa.omie_sync_em = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )
            resultado["sincronizado_em"] = empresa.omie_sync_em
            db.commit()
    else:
        # Modo simulação: nada do que foi gravado/flushado permanece no banco.
        db.rollback()

    # XML da nota: o que foi consultado e qualquer falha na listagem (a nota em
    # si já está gravada — o XML é acessório e não invalida a sincronização).
    resultado["descricoes"]["consultas"] = xmls.consultas
    resultado["avisos"].extend(xmls.avisos)

    return resultado
