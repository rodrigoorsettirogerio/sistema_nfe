"""Script de teste da sincronização Omie -> banco (fase 2, backend/omie_sync.py).

Prova, SEM chamar a API e SEM tocar na base real (banco SQLite temporário):

1. a migração aditiva cria ``empresas.omie_app_key/omie_app_secret/omie_sync_em``
   numa base antiga e é idempotente (rodar duas vezes não muda nada);
2. a credencial é gravada CIFRADA (``fernet:v1:...``), volta legível pela mesma
   chave e uma chave diferente é recusada com instrução de como regravar;
3. o recebimento do Omie vira nota + itens com o mapeamento certo (chave, número
   sem zeros, série, data, valor, fornecedor, EAN, NCM/CFOP sem pontuação);
4. os valores CRUS do Omie (``nQtdeNFe``/``nPrecoUnit``/``vTotalItem``) ficam nas
   colunas principais e as conversões vão só para os campos ``*_base``;
5. a deduplicação por chave de acesso funciona (mesma nota em duas etapas e
   repetida na paginação = uma única linha);
6. bonificação/doação e nota cancelada não entram na base;
7. o modo ``--simular`` (``confirmar=False``) conta tudo e não deixa nada gravado;
8. o resumo avisa quando um item importado ficou sem marca (a marca entra na
   chave do grupo dos relatórios) e o CLI ``testar_omie_sync.py`` recusa
   ``--sem-marcas`` fora da simulação;
9. a sinalização de divergência de produto grava pendência em ``itens_auditados``
   (motivos ``divergencia_produto``/``revisar_vinculacao``) sem alterar a
   identidade do item: mesma âncora (EAN ou fornecedor+cProd) com descrição/
   cProd diferente é marcada, código ``PRD...`` pede revisão de vinculação e o
   resumo ``divergencias`` conta notas/itens;
10. as entidades HTML/XML do Omie são decodificadas na ENTRADA
    (``"&amp; SKIRT STEAK"`` -> ``"& SKIRT STEAK"``), inclusive na razão social do
    fornecedor, sem tocar em ``&`` que faz parte do texto;
11. o ``cXml`` da nota (HTML-escapado) é lido e o parse devolve cProd/xProd por
    item (``itens_do_xml``), com namespace, sem namespace e com ``&amp;amp;``;
12. a descrição do cadastro interno do Omie é corrigida pelo xProd do XML na
    etapa 40: ``descricao_produto`` passa a ser a do fornecedor, o texto antigo
    fica em ``descricao_produto_anterior``, a trilha vai para ``itens_auditados``
    (antes -> depois, fonte "XML etapa 40", data/hora), a memória de-para é
    atualizada e nota com descrição já correta NÃO muda (nem a nota já gravada,
    quando o XML chega numa execução seguinte).

Uso (na raiz do projeto):
    python tests/test_omie_sync.py
"""

import html
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

import backend.database as bd
from backend import cripto_credenciais as bcripto
from backend import omie_sync as bsync
from backend.models import (
    Empresa,
    ItemAuditado,
    ItemNota,
    MapeamentoFornecedorProduto,
    NotaFiscal,
)

CHAVE = "31261104559938000105550010000958901593724620"
CHAVE_2 = "35261111222333000181550010000012341000012345"
CHAVE_CANCELADA = "43261199888777000166550010000055551000055555"
CHAVE_SIMULADA = "51261144555666000177550010000099991000099999"
# Notas do bloco de divergências: EAN com outra descrição/cProd, cProd sem EAN
# repetido com outra descrição e uma nota sem nenhum conflito.
CHAVE_DIV_EAN = "44261107876067000567550010000011111000011111"
CHAVE_PROD_NOVO = "45261107876067000567550010000022222000022222"
CHAVE_PROD_DIVERGENTE = "46261107876067000567550010000033333000033333"
CHAVE_LIMPA = "47261107876067000567550010000044444000044444"
CHAVE_LIMPA_2 = "48261107876067000567550010000055555000055555"
# Notas do bloco de correção da descrição pelo XML (passo 3).
CHAVE_XML_CORRIGIDA = "49261107876067000567550010000066666000066666"
CHAVE_XML_CONFERIDA = "50261107876067000567550010000077777000077777"
CHAVE_XML_EXISTENTE = "52261107876067000567550010000088888000088888"
CNPJ = "04.559.938/0001-05"
CNPJ_DIGITOS = "04559938000105"
CHAVE_CRIPTO = "segredo-de-teste-omie-0123456789abcdef"


def _conferir(condicao, mensagem):
    print(("OK: " if condicao else "FALHOU: ") + mensagem)
    if not condicao:
        raise AssertionError(mensagem)


def _colunas(engine, tabela):
    """Nomes das colunas de uma tabela do banco temporário."""
    inspetor = inspect(engine)
    if tabela not in inspetor.get_table_names():
        return set()
    return {coluna["name"] for coluna in inspetor.get_columns(tabela)}


def _item(
    codigo="PRD00002",
    descricao="BACON FATIADO F.A. FOODS",
    ean="7898925798101",
    ncm="0406.10.10",
    cfop="6.101",
    unidade="KG",
    quantidade=7.95,
    preco=47.59,
    total=378.34,
    desconto=0,
):
    """Item de ``itensRecebimento`` no envelope real do Omie (itensCabec)."""
    return {
        "itensCabec": {
            "cCodigoProduto": codigo,
            "cDescricaoProduto": descricao,
            "cEAN": ean,
            "cNCM": ncm,
            "cCFOP": cfop,
            "cUnidadeNfe": unidade,
            "nPrecoUnit": preco,
            "nQtdeNFe": quantidade,
            "nDesconto": desconto,
            "vDesconto": desconto,
            "vTotalItem": total,
        },
        "itensAjustes": {"cUnidade": unidade, "nQtdeRecebida": quantidade},
    }


def _recebimento(chave=CHAVE, numero="000095890", cancelada=False, itens=None, valor=378.34):
    """Recebimento como a ``ListarRecebimentos`` devolve com cExibirDetalhes="S"."""
    return {
        "cabec": {
            "cChaveNFe": chave,
            "cNumeroNFe": numero,
            "cSerieNFe": "1",
            "dEmissaoNFe": "10/11/2026",
            "nValorNFe": valor,
            "cCNPJ_CPF": CNPJ,
            "cRazaoSocial": "LATICINIO PIEMONTESE LTDA",
            "cNome": "BORGHESE",
        },
        "infoCadastro": {"cCancelada": "S" if cancelada else "N", "cFaturado": "S"},
        "totais": {"vTotalNFe": valor},
        "itensRecebimento": itens if itens is not None else [_item()],
    }


class ClienteFalso:
    """``OmieClient`` de mentira: devolve os lotes por etapa e registra a chamada.

    ``documentos`` alimenta o ``ListarDocumentos`` (XML das notas) da mesma
    forma: é uma lista fixa devolvida na página 1 (as demais vêm vazias, que é
    o critério de parada da varredura).
    """

    def __init__(self, lotes=None, documentos=None):
        self.lotes = lotes or {}
        self.documentos = list(documentos or [])
        self.chamadas = []
        self.chamadas_documentos = []

    def listar_todos_recebimentos(
        self,
        data_inicio=None,
        data_fim=None,
        *,
        registros_por_pagina=50,
        maximo_paginas=100,
        etapa=None,
        intervalo=0.0,
    ):
        self.chamadas.append(
            {
                "inicio": data_inicio,
                "fim": data_fim,
                "etapa": etapa,
                "por_pagina": registros_por_pagina,
                "intervalo": intervalo,
            }
        )
        return list(self.lotes.get(etapa, []))

    def listar_documentos_entrada(
        self,
        data_inicio,
        data_fim,
        pagina=1,
        registros_por_pagina=50,
    ):
        """Página de ``ListarDocumentos`` (XML completo em ``cXml``)."""
        self.chamadas_documentos.append(
            {
                "inicio": data_inicio,
                "fim": data_fim,
                "pagina": pagina,
                "por_pagina": registros_por_pagina,
            }
        )
        return list(self.documentos) if pagina == 1 else []


def _xml_nota(chave, itens):
    """XML mínimo de NF-e (``nfeProc``, com namespace) para o teste do parse.

    ``itens`` é uma lista de dicionários com ``c_prod``/``xprod`` e, opcional,
    ``ean``/``ncm``/``cfop``/``unidade``/``quantidade``/``preco``/``total``. O
    texto do xProd é gravado como veio (o teste usa ``&amp;``/``&amp;amp;`` para
    conferir a decodificação).
    """
    dets = "".join(
        "<det nItem=\"{posicao}\"><prod>"
        "<cProd>{c_prod}</cProd>"
        "<cEAN>{ean}</cEAN>"
        "<xProd>{xprod}</xProd>"
        "<NCM>{ncm}</NCM>"
        "<CFOP>{cfop}</CFOP>"
        "<uCom>{unidade}</uCom>"
        "<qCom>{quantidade}</qCom>"
        "<vUnCom>{preco}</vUnCom>"
        "<vProd>{total}</vProd>"
        "</prod></det>".format(
            posicao=posicao,
            c_prod=item.get("c_prod", ""),
            ean=item.get("ean") or "SEM GTIN",
            xprod=item["xprod"],
            ncm=item.get("ncm", "04061010"),
            cfop=item.get("cfop", "6101"),
            unidade=item.get("unidade", "KG"),
            quantidade=item.get("quantidade", "1.0000"),
            preco=item.get("preco", "10.00"),
            total=item.get("total", "10.00"),
        )
        for posicao, item in enumerate(itens, start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe">'
        f'<NFe><infNFe Id="NFe{chave}" versao="4.00">{dets}</infNFe></NFe>'
        "</nfeProc>"
    )


def _documento_omie(chave, itens):
    """Documento de ``ListarDocumentos`` como o Omie devolve (``cXml`` escapado)."""
    return {"nChave": chave, "cXml": html.escape(_xml_nota(chave, itens))}




def _checar_entidades_html():
    """Texto do Omie entra SEM entidade HTML/XML (``&amp;`` -> ``&``).

    O Omie devolve o texto escapado; sem a limpeza na ENTRADA o "&amp;" vira parte
    do nome do produto e o resíduo "AMP" podia ser lido como marca. A decodificação
    é a mesma função usada pelo importador de XML (`limpar_entidades_texto`).
    """
    from backend.aprendizado_produtos import limpar_entidades_texto

    _conferir(
        limpar_entidades_texto("&amp; Skirt Steak") == "& Skirt Steak",
        '"&amp; Skirt Steak" vira "& Skirt Steak"',
    )
    _conferir(
        limpar_entidades_texto("&amp;amp;") == "&",
        '"&amp;amp;" vira "&" (decodifica em laço até estabilizar)',
    )
    _conferir(
        limpar_entidades_texto("PEITO & FRANGO") == "PEITO & FRANGO",
        '"&" comum da descrição continua "&" (a guarda não mexe)',
    )

    itens = bsync._itens_do_recebimento(
        _recebimento(itens=[_item(descricao="&amp; SKIRT STEAK")])
    )
    _conferir(
        bool(itens) and itens[0]["descricao"] == "& SKIRT STEAK",
        "cDescricaoProduto (&amp; SKIRT STEAK) entra no item já decodificado",
    )
    recebimento = _recebimento()
    recebimento["cabec"]["cRazaoSocial"] = "ATACADAO &amp; CIA LTDA"
    dados = bsync._dados_do_recebimento(recebimento)
    _conferir(
        dados["fornecedor"] == "ATACADAO & CIA LTDA",
        "cRazaoSocial do fornecedor entra decodificada",
    )


def _checar_itens_do_xml():
    """``itens_do_xml`` lê o ``cXml`` escapado e devolve cProd/xProd por item.

    O XML chega HTML-escapado no ``cXml`` (uma camada) e o xProd pode ter mais
    uma (``&amp;amp;``); as duas precisam sair limpas. O teste também cobre XML
    sem namespace e XML ilegível (``[]``, sem exceção).
    """
    xml = _xml_nota(
        CHAVE_XML_CORRIGIDA,
        [
            {
                "c_prod": "FRALDA123",
                "xprod": "FRALDA GERIATRICA &amp; CONFORT",
                "ean": "7898925798118",
                "ncm": "9619.00.00",
                "cfop": "6.102",
            },
            {
                "c_prod": "DUP-01",
                "xprod": "FRALDA &amp;amp; CONFORT PLUS",
                "ean": "SEM GTIN",
            },
        ],
    )
    itens = bsync.itens_do_xml(html.escape(xml))
    _conferir(
        len(itens) == 2
        and itens[0]["c_prod"] == "FRALDA123"
        and itens[0]["descricao"] == "FRALDA GERIATRICA & CONFORT",
        "itens_do_xml: cXml escapado devolve cProd/xProd do item 1 (&amp; -> &)",
    )
    _conferir(
        itens[0]["ean"] == "7898925798118"
        and itens[0]["ncm"] == "96190000"
        and itens[0]["cfop"] == "6102",
        "itens_do_xml: EAN/NCM/CFOP normalizados como no importador de XML",
    )
    _conferir(
        itens[1]["descricao"] == "FRALDA & CONFORT PLUS"
        and itens[1]["ean"] == "SEM_EAN",
        "itens_do_xml: &amp;amp; decodifica até o & final e SEM GTIN vira SEM_EAN",
    )
    sem_namespace = _xml_nota(
        CHAVE_XML_CONFERIDA, [{"c_prod": "X1", "xprod": "ACUCAR REFINADO"}]
    ).replace(' xmlns="http://www.portalfiscal.inf.br/nfe"', "")
    _conferir(
        [item["descricao"] for item in bsync.itens_do_xml(sem_namespace)]
        == ["ACUCAR REFINADO"],
        "itens_do_xml: XML sem namespace também é lido",
    )
    _conferir(
        bsync.itens_do_xml("<html>erro</html>") == []
        and bsync.itens_do_xml("") == [],
        "itens_do_xml: XML ilegível/vazio devolve [] (não derruba a importação)",
    )


def main():
    # O teste nunca usa as credenciais reais do .env: sobrescreve as variáveis e
    # restaura no final (o .env é lido com override=False, então o ambiente manda).
    ambiente_original = {
        nome: os.environ.get(nome)
        for nome in ("OMIE_ENC_KEY", "OMIE_APP_KEY", "OMIE_APP_SECRET")
    }
    os.environ["OMIE_ENC_KEY"] = CHAVE_CRIPTO
    os.environ["OMIE_APP_KEY"] = "APP-KEY-DO-AMBIENTE"
    os.environ["OMIE_APP_SECRET"] = "APP-SECRET-DO-AMBIENTE"

    try:
        print("=" * 78)
        print("0) ENTIDADES HTML/XML — o texto do Omie entra limpo (&amp; -> &)")
        print("=" * 78)
        _checar_entidades_html()
        _checar_itens_do_xml()

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as diretorio:
            arquivo = Path(diretorio) / "omie_sync_teste.db"
            engine = create_engine(
                "sqlite:///" + str(arquivo).replace("\\", "/"),
                connect_args={"check_same_thread": False},
            )

            print("=" * 78)
            print("1) MIGRAÇÃO ADITIVA — base antiga sem as colunas da integração")
            print("=" * 78)
            # Base "antiga" = esquema atual sem as três colunas da fase 2. É o mesmo
            # estado de um banco criado antes desta entrega (o DROP só toca colunas
            # que o teste acabou de criar, no banco temporário).
            bd.init_db(engine)
            with engine.begin() as conexao:
                for coluna in ("omie_app_key", "omie_app_secret", "omie_sync_em"):
                    conexao.exec_driver_sql(f"ALTER TABLE empresas DROP COLUMN {coluna}")
                # Colunas da correção de descrição pelo XML (passo 3) também nascem
                # numa base antiga — o DROP só toca colunas recém-criadas.
                for coluna in ("descricao_produto_anterior", "descricao_origem"):
                    conexao.exec_driver_sql(
                        f"ALTER TABLE itens_nota DROP COLUMN {coluna}"
                    )
                for coluna in (
                    "descricao_antes",
                    "descricao_depois",
                    "fonte",
                    "registrado_em",
                ):
                    conexao.exec_driver_sql(
                        f"ALTER TABLE itens_auditados DROP COLUMN {coluna}"
                    )
            _conferir(
                "omie_app_key" not in _colunas(engine, "empresas"),
                "a base antiga começa sem empresas.omie_app_key",
            )
            bd.init_db(engine)
            colunas = _colunas(engine, "empresas")
            _conferir(
                {"omie_app_key", "omie_app_secret", "omie_sync_em"} <= colunas,
                "init_db criou omie_app_key, omie_app_secret e omie_sync_em",
            )
            _conferir(
                {"descricao_produto_anterior", "descricao_origem"}
                <= _colunas(engine, "itens_nota")
                and {
                    "descricao_antes",
                    "descricao_depois",
                    "fonte",
                    "registrado_em",
                }
                <= _colunas(engine, "itens_auditados"),
                "init_db criou as colunas da correção de descrição pelo XML",
            )
            bd.init_db(engine)
            _conferir(
                _colunas(engine, "empresas") == colunas,
                "init_db rodado de novo não altera nada (idempotente)",
            )

            db = sessionmaker(bind=engine)()
            empresa = Empresa(
                cnpj="11222333000181",
                razao_social="LOJA TESTE LTDA",
                nome_loja="Loja Teste",
            )
            db.add(empresa)
            db.commit()
            db.refresh(empresa)

            print()
            print("=" * 78)
            print("2) CREDENCIAIS CIFRADAS — Fernet com chave fora do banco")
            print("=" * 78)
            bsync.definir_credenciais(db, empresa, "APP-KEY-123", "APP-SECRET-456")
            salva = db.get(Empresa, empresa.id)
            _conferir(
                (salva.omie_app_key or "").startswith(bcripto.PREFIXO)
                and (salva.omie_app_secret or "").startswith(bcripto.PREFIXO),
                "as duas credenciais foram gravadas no formato fernet:v1:<token>",
            )
            _conferir(
                "APP-SECRET-456" not in (salva.omie_app_key + salva.omie_app_secret),
                "o segredo em texto não aparece em nenhuma coluna",
            )
            lidas = bsync.credenciais_da_empresa(salva)
            _conferir(
                lidas == ("APP-KEY-123", "APP-SECRET-456", "empresa"),
                f"a leitura devolve a credencial da loja (origem {lidas[2]})",
            )
            _conferir(
                bcripto.descriptografar(salva.omie_app_secret) == "APP-SECRET-456",
                "criptografar/descriptografar é reversível",
            )
            os.environ["OMIE_ENC_KEY"] = "outra-chave-de-teste-0123456789abcdefgh"
            try:
                bcripto.descriptografar(salva.omie_app_secret)
                erro_chave = ""
            except bcripto.ErroCredenciais as exc:
                erro_chave = str(exc)
            finally:
                os.environ["OMIE_ENC_KEY"] = CHAVE_CRIPTO
            _conferir(
                "Regrave as credenciais" in erro_chave,
                "chave de criptografia diferente é recusada com instrução de regravar",
            )

            sem_credencial = Empresa(cnpj="22333444000155", razao_social="LOJA DO ENV")
            db.add(sem_credencial)
            db.commit()
            _conferir(
                bsync.credenciais_da_empresa(sem_credencial)
                == ("APP-KEY-DO-AMBIENTE", "APP-SECRET-DO-AMBIENTE", "ambiente"),
                "loja sem credencial própria usa OMIE_APP_KEY/OMIE_APP_SECRET do ambiente",
            )

            print()
            print("=" * 78)
            print("3) SINCRONIZAÇÃO — recebimento do Omie vira nota + itens")
            print("=" * 78)
            cliente = ClienteFalso(
                {
                    "40": [
                        _recebimento(
                            itens=[
                                _item(),
                                # Bonificação na mesma nota: não pode entrar na base.
                                _item(
                                    codigo="PRD00099",
                                    descricao="BRINDE BACON FATIADO",
                                    ean="",
                                    cfop="5.910",
                                    preco=0.0,
                                    total=0.0,
                                    quantidade=1.0,
                                ),
                            ]
                        ),
                        _recebimento(
                            chave=CHAVE_2,
                            numero="000012345",
                            valor=100.0,
                            itens=[
                                _item(
                                    codigo="PRD00003",
                                    descricao="REFRIGERANTE ORIGINAL 600ML",
                                    ean="7894900011517",
                                    ncm="2202.10.00",
                                    cfop="5.102",
                                    unidade="UN",
                                    quantidade=2.0,
                                    preco=50.0,
                                    total=100.0,
                                )
                            ],
                        ),
                        _recebimento(
                            chave=CHAVE_CANCELADA,
                            numero="000005555",
                            cancelada=True,
                        ),
                        # A MESMA nota repetida na paginação: só uma linha no banco.
                        _recebimento(),
                    ],
                    # A mesma nota aparece também na etapa seguinte (mudou de coluna).
                    "60": [_recebimento()],
                    "80": [],
                }
            )
            resultado = bsync.sincronizar_recebimentos(
                db,
                empresa,
                dias=7,
                cliente=cliente,
                intervalo=0.0,
                registros_por_pagina=2,
            )
            _conferir(
                [chamada["etapa"] for chamada in cliente.chamadas] == ["40", "60", "80"],
                "as três etapas foram pedidas ao cliente, na ordem 40/60/80",
            )
            _conferir(
                all(chamada["por_pagina"] == 2 for chamada in cliente.chamadas)
                and cliente.chamadas[0]["inicio"] is not None,
                "paginação e período (7 dias) foram repassados ao cliente",
            )
            _conferir(
                resultado["novas"] == 2
                and resultado["duplicadas"] == 2
                and resultado["canceladas"] == 1,
                "contadores: 2 novas, 2 duplicadas e 1 cancelada "
                f"(recebido {resultado['novas']}/{resultado['duplicadas']}/"
                f"{resultado['canceladas']})",
            )
            _conferir(
                resultado["itens"] == 2 and resultado["bonificados"] == 1,
                "2 itens gravados e 1 bonificação ignorada",
            )
            _conferir(
                resultado["fornecedores_novos"] == 1
                and resultado["mapeamentos_criados"] == 2,
                "fornecedor criado uma vez (CNPJ único) e 2 produtos aprendidos",
            )
            _conferir(resultado["erros"] == [], "nenhum erro na sincronização")
            _conferir(
                resultado["itens_sem_marca"] == 2,
                "os 2 itens importados ficaram sem marca (base sem memória de marcas)",
            )
            _conferir(
                len(resultado["avisos"]) == 1
                and "backfill_marcas.py" in resultado["avisos"][0],
                "o resumo avisa os itens sem marca e indica o backfill_marcas.py",
            )


            nota = db.query(NotaFiscal).filter(NotaFiscal.chave_acesso == CHAVE).one()
            _conferir(
                nota.numero_nota == "95890"
                and nota.serie == "1"
                and nota.data_emissao == date(2026, 11, 10)
                and abs(nota.valor_total - 378.34) < 1e-9
                and nota.empresa_id == empresa.id,
                "cabeçalho: número sem zeros, série, emissão, valor e loja da nota",
            )
            item = db.query(ItemNota).filter(ItemNota.nota_id == nota.id).one()
            fornecedor = item.fornecedor
            _conferir(
                fornecedor.cnpj == CNPJ_DIGITOS
                and fornecedor.nome == "LATICINIO PIEMONTESE LTDA",
                "fornecedor gravado pelo CNPJ (sem máscara), com razão social",
            )
            _conferir(
                item.ean == "7898925798101"
                and item.ncm == "04061010"
                and item.cfop == "6101"
                and item.c_prod == "PRD00002"
                and item.unidade_compra == "KG",
                "EAN/NCM/CFOP/cProd/uCom normalizados como no importador de XML",
            )
            _conferir(
                abs(item.quantidade - 7.95) < 1e-9
                and abs(item.valor_unitario - 47.59) < 1e-9
                and abs(item.valor_total - 378.34) < 1e-9,
                "valores CRUS do Omie (nQtdeNFe/nPrecoUnit/vTotalItem) preservados",
            )
            _conferir(
                abs(item.quantidade_base - 7.95) < 1e-9
                and abs(item.valor_unitario_base - 47.59) < 1e-9
                and item.qtd_por_embalagem == 1.0
                and item.unidade_base == "KG",
                "conversão para unidade base nos campos *_base (sem embalagem: 1:1)",
            )
            _conferir(
                db.query(MapeamentoFornecedorProduto)
                .filter(MapeamentoFornecedorProduto.cnpj_fornecedor == CNPJ_DIGITOS)
                .count()
                == 2,
                "memória de-para gravada para os dois produtos do fornecedor",
            )
            _conferir(
                db.get(Empresa, empresa.id).omie_sync_em is not None,
                "empresas.omie_sync_em marcado no fim da sincronização",
            )
            _conferir(
                not db.query(NotaFiscal)
                .filter(NotaFiscal.chave_acesso == CHAVE_CANCELADA)
                .first(),
                "nota cancelada no Omie não foi gravada",
            )

            print()
            print("=" * 78)
            print("4) DEDUPLICAÇÃO — rodar a sincronização de novo não duplica nada")
            print("=" * 78)
            notas_antes = db.query(NotaFiscal).count()
            itens_antes = db.query(ItemNota).count()
            segunda = bsync.sincronizar_recebimentos(
                db,
                empresa,
                dias=7,
                cliente=ClienteFalso(
                    {
                        "40": [_recebimento(), _recebimento(chave=CHAVE_2, numero="000012345")],
                        "60": [_recebimento()],
                        "80": [_recebimento(chave=CHAVE_CANCELADA, cancelada=True)],
                    }
                ),
                intervalo=0.0,
            )
            _conferir(
                segunda["novas"] == 0 and segunda["duplicadas"] == 3,
                "segunda execução: nenhuma nota nova e 3 duplicadas",
            )
            _conferir(
                db.query(NotaFiscal).count() == notas_antes
                and db.query(ItemNota).count() == itens_antes,
                "contagem de notas e itens não mudou na segunda execução",
            )
            _conferir(
                segunda["itens_sem_marca"] == 0 and segunda["avisos"] == [],
                "sem nota nova não há aviso de item sem marca (recorte da execução)",
            )

            print()
            print("=" * 78)
            print("5) MODO SIMULAÇÃO — conta o que entraria e desfaz no fim")
            print("=" * 78)
            simulado = bsync.sincronizar_recebimentos(
                db,
                empresa,
                dias=7,
                cliente=ClienteFalso(
                    {"40": [_recebimento(chave=CHAVE_SIMULADA, numero="000099999")]}
                ),
                intervalo=0.0,
                confirmar=False,
                recalcular_marcas_itens=False,
            )
            _conferir(
                simulado["novas"] == 1 and simulado["simulado"] is True,
                "a simulação conta a nota nova que entraria",
            )
            _conferir(
                db.query(NotaFiscal).count() == notas_antes
                and db.query(ItemNota).count() == itens_antes,
                "a simulação não deixou nota/item gravado no banco",
            )

            print()
            print("=" * 78)
            print("6) DIVERGÊNCIAS — sinaliza âncora/código PRD sem mudar a identidade")
            print("=" * 78)
            # A primeira importação só tinha códigos PRD (regra b): dois itens,
            # dois marcadores VINC_..., sem conflito de descrição (histórico vazio).
            _conferir(
                resultado["divergencias"]["total"] == 2
                and resultado["divergencias"]["revisar_vinculacao"] == 2
                and resultado["divergencias"]["divergencia_produto"] == 0,
                "importação inicial: 2 itens PRD sinalizados para revisar vinculação",
            )
            marcadores_prd = (
                db.query(ItemAuditado)
                .filter(ItemAuditado.motivo == bsync.MOTIVO_REVISAR_VINCULACAO)
                .all()
            )
            _conferir(
                len(marcadores_prd) == 2
                and all(
                    marcador.chave_identificacao.startswith("VINC_")
                    and marcador.auditado is False
                    for marcador in marcadores_prd
                ),
                "pendências VINC_ gravadas em itens_auditados (auditado=False)",
            )

            # Mesmo EAN já conhecido, mas com outra descrição e outro cProd do
            # mesmo fornecedor: divergência + revisão de vinculação no mesmo item.
            ean_divergente = bsync.sincronizar_recebimentos(
                db,
                empresa,
                dias=7,
                cliente=ClienteFalso(
                    {
                        "40": [
                            _recebimento(
                                chave=CHAVE_DIV_EAN,
                                numero="000001111",
                                valor=50.0,
                                itens=[
                                    _item(
                                        codigo="PRD00077",
                                        descricao="BACON FATIADO OUTRA MARCA",
                                        ean="7898925798101",
                                        quantidade=1.0,
                                        preco=50.0,
                                        total=50.0,
                                    )
                                ],
                            )
                        ]
                    }
                ),
                intervalo=0.0,
            )
            _conferir(
                ean_divergente["divergencias"]["total"] == 1
                and ean_divergente["divergencias"]["divergencia_produto"] == 1
                and ean_divergente["divergencias"]["revisar_vinculacao"] == 1
                and ean_divergente["divergencias"]["notas"] == 1,
                "mesmo EAN com outra descrição/cProd: divergência + PRD no mesmo item",
            )
            nota_div = (
                db.query(NotaFiscal)
                .filter(NotaFiscal.chave_acesso == CHAVE_DIV_EAN)
                .one()
            )
            item_div = db.query(ItemNota).filter(ItemNota.nota_id == nota_div.id).one()
            _conferir(
                item_div.descricao_produto == "BACON FATIADO OUTRA MARCA"
                and item_div.c_prod == "PRD00077"
                and item_div.ean == "7898925798101",
                "a identidade gravada continua a do Omie (a divergência só sinaliza)",
            )

            # Sem EAN a âncora é fornecedor + cProd: a primeira nota aprende o
            # produto e a segunda, com a MESMA chave e outra descrição, diverge.
            produto_novo = bsync.sincronizar_recebimentos(
                db,
                empresa,
                dias=7,
                cliente=ClienteFalso(
                    {
                        "40": [
                            _recebimento(
                                chave=CHAVE_PROD_NOVO,
                                numero="000002222",
                                valor=50.0,
                                itens=[
                                    _item(
                                        codigo="FORN001",
                                        descricao="FARINHA DE TRIGO",
                                        ean="",
                                        ncm="1101.00.10",
                                        quantidade=10.0,
                                        preco=5.0,
                                        total=50.0,
                                    )
                                ],
                            )
                        ]
                    }
                ),
                intervalo=0.0,
            )
            _conferir(
                produto_novo["divergencias"]["total"] == 0,
                "primeira nota de um cProd (sem EAN) não é divergência",
            )
            produto_divergente = bsync.sincronizar_recebimentos(
                db,
                empresa,
                dias=7,
                cliente=ClienteFalso(
                    {
                        "40": [
                            _recebimento(
                                chave=CHAVE_PROD_DIVERGENTE,
                                numero="000003333",
                                valor=60.0,
                                itens=[
                                    _item(
                                        codigo="FORN001",
                                        descricao="FARINHA DE TRIGO INTEGRAL",
                                        ean="",
                                        ncm="1101.00.10",
                                        quantidade=10.0,
                                        preco=6.0,
                                        total=60.0,
                                    )
                                ],
                            )
                        ]
                    }
                ),
                intervalo=0.0,
            )
            _conferir(
                produto_divergente["divergencias"]["total"] == 1
                and produto_divergente["divergencias"]["divergencia_produto"] == 1
                and produto_divergente["divergencias"]["revisar_vinculacao"] == 0
                and produto_divergente["divergencias"]["notas"] == 1,
                "mesmo fornecedor+cProd com outra descrição: divergência de produto",
            )

            # Descrição igual na mesma âncora (duas notas seguidas): nada a sinalizar.
            for chave, numero in (
                (CHAVE_LIMPA, "000004444"),
                (CHAVE_LIMPA_2, "000005555"),
            ):
                limpa = bsync.sincronizar_recebimentos(
                    db,
                    empresa,
                    dias=7,
                    cliente=ClienteFalso(
                        {
                            "40": [
                                _recebimento(
                                    chave=chave,
                                    numero=numero,
                                    valor=40.0,
                                    itens=[
                                        _item(
                                            codigo="FORN002",
                                            descricao="ACUCAR REFINADO",
                                            ean="",
                                            ncm="1701.99.00",
                                            quantidade=10.0,
                                            preco=4.0,
                                            total=40.0,
                                        )
                                    ],
                                )
                            ]
                        }
                    ),
                    intervalo=0.0,
                )
                _conferir(
                    limpa["divergencias"]["total"] == 0,
                    f"descrição igual na mesma âncora não gera divergência ({chave[-6:]})",
                )

            marcadores = (
                db.query(ItemAuditado)
                .filter(
                    ItemAuditado.motivo.in_(
                        [
                            bsync.MOTIVO_DIVERGENCIA_PRODUTO,
                            bsync.MOTIVO_REVISAR_VINCULACAO,
                        ]
                    )
                )
                .all()
            )
            por_motivo = {}
            for marcador in marcadores:
                por_motivo[marcador.motivo] = por_motivo.get(marcador.motivo, 0) + 1
            _conferir(
                por_motivo.get(bsync.MOTIVO_DIVERGENCIA_PRODUTO) == 2
                and por_motivo.get(bsync.MOTIVO_REVISAR_VINCULACAO) == 3,
                f"pendências acumuladas por motivo: {por_motivo}",
            )
            _conferir(
                all(
                    marcador.chave_identificacao.startswith(("DIV_", "VINC_"))
                    and not marcador.chave_identificacao.startswith("DESC_")
                    for marcador in marcadores
                ),
                "chaves DIV_/VINC_ ficam fora do mapa DESC_ usado pelos relatórios",
            )



            print()
            print("=" * 78)
            print("7) XML DA NOTA — descrição do fornecedor corrigida na etapa 40")
            print("=" * 78)
            # A listagem traz a descrição do CADASTRO interno ("SKIRT STEAK") e o
            # código interno PRD...; o XML da nota traz cProd/xProd do fornecedor.
            cliente_xml = ClienteFalso(
                {
                    "40": [
                        _recebimento(
                            chave=CHAVE_XML_CORRIGIDA,
                            numero="000006666",
                            valor=120.0,
                            itens=[
                                _item(
                                    codigo="PRD00050",
                                    descricao="SKIRT STEAK",
                                    ean="7898925798118",
                                    ncm="9619.00.00",
                                    cfop="6.102",
                                    unidade="UN",
                                    quantidade=2.0,
                                    preco=60.0,
                                    total=120.0,
                                )
                            ],
                        )
                    ]
                },
                documentos=[
                    _documento_omie(
                        CHAVE_XML_CORRIGIDA,
                        [
                            {
                                "c_prod": "FRALDA123",
                                "xprod": "FRALDA GERIATRICA &amp; CONFORT",
                                "ean": "7898925798118",
                                "ncm": "9619.00.00",
                                "cfop": "6.102",
                                "unidade": "UN",
                            }
                        ],
                    )
                ],
            )
            corrigida = bsync.sincronizar_recebimentos(
                db, empresa, dias=7, cliente=cliente_xml, intervalo=0.0
            )
            _conferir(
                corrigida["novas"] == 1
                and corrigida["descricoes"]["corrigidas"] == 1
                and corrigida["descricoes"]["xmls_lidos"] == 1,
                "nota nova na etapa 40: 1 descrição corrigida pelo xProd do XML "
                f"({corrigida['descricoes']})",
            )
            _conferir(
                len(cliente_xml.chamadas_documentos) == 1,
                "ListarDocumentos foi chamado uma única vez para o período inteiro",
            )

            nota_corrigida = (
                db.query(NotaFiscal)
                .filter(NotaFiscal.chave_acesso == CHAVE_XML_CORRIGIDA)
                .one()
            )
            item_corrigido = (
                db.query(ItemNota).filter(ItemNota.nota_id == nota_corrigida.id).one()
            )
            _conferir(
                item_corrigido.descricao_produto == "FRALDA GERIATRICA & CONFORT"
                and item_corrigido.descricao_produto_anterior == "SKIRT STEAK"
                and item_corrigido.descricao_origem == "XML etapa 40",
                "item fica com a descrição do fornecedor e preserva a anterior",
            )
            _conferir(
                item_corrigido.ean == "7898925798118"
                and item_corrigido.c_prod == "PRD00050",
                "a correção não mexe na identidade (EAN/cProd continuam os do Omie)",
            )
            marcador_xml = (
                db.query(ItemAuditado)
                .filter(
                    ItemAuditado.chave_identificacao
                    == f"XML_{CHAVE_XML_CORRIGIDA}_1"
                )
                .one()
            )
            _conferir(
                marcador_xml.motivo == bsync.MOTIVO_DESCRICAO_XML
                and marcador_xml.auditado is True
                and marcador_xml.descricao_antes == "SKIRT STEAK"
                and marcador_xml.descricao_depois == "FRALDA GERIATRICA & CONFORT"
                and marcador_xml.fonte == "XML etapa 40"
                and bool(marcador_xml.registrado_em),
                "itens_auditados guarda antes -> depois, fonte e data/hora",
            )
            _conferir(
                (marcador_xml.chave_identificacao or "").startswith("XML_")
                and not (marcador_xml.chave_identificacao or "").startswith("DESC_"),
                "a chave da correção fica fora do mapa DESC_ usado pelos relatórios",
            )
            mapeamento_xml = (
                db.query(MapeamentoFornecedorProduto)
                .filter(MapeamentoFornecedorProduto.c_prod == "PRD00050")
                .one()
            )
            _conferir(
                mapeamento_xml.descricao_padrao == "FRALDA GERIATRICA & CONFORT"
                and corrigida["descricoes"]["memoria_atualizada"] == 1,
                "memória de-para passa a guardar a descrição do fornecedor",
            )

            # XML com a MESMA descrição da lista: nada muda (nem trilha, nem origem).
            conferido = bsync.sincronizar_recebimentos(
                db,
                empresa,
                dias=7,
                cliente=ClienteFalso(
                    {
                        "40": [
                            _recebimento(
                                chave=CHAVE_XML_CONFERIDA,
                                numero="000007777",
                                valor=40.0,
                                itens=[
                                    _item(
                                        codigo="FORN003",
                                        descricao="ACUCAR REFINADO",
                                        ean="7891234567890",
                                        ncm="1701.99.00",
                                        cfop="5.102",
                                        unidade="UN",
                                        quantidade=10.0,
                                        preco=4.0,
                                        total=40.0,
                                    )
                                ],
                            )
                        ]
                    },
                    documentos=[
                        _documento_omie(
                            CHAVE_XML_CONFERIDA,
                            [
                                {
                                    "c_prod": "FORN003",
                                    "xprod": "ACUCAR REFINADO",
                                    "ean": "7891234567890",
                                    "ncm": "1701.99.00",
                                    "cfop": "5102",
                                    "unidade": "UN",
                                }
                            ],
                        )
                    ],
                ),
                intervalo=0.0,
            )
            nota_conferida = (
                db.query(NotaFiscal)
                .filter(NotaFiscal.chave_acesso == CHAVE_XML_CONFERIDA)
                .one()
            )
            item_conferido = (
                db.query(ItemNota)
                .filter(ItemNota.nota_id == nota_conferida.id)
                .one()
            )
            _conferir(
                conferido["descricoes"]["corrigidas"] == 0
                and conferido["descricoes"]["conferidas"] == 1,
                "descrição já igual ao xProd: conferida, sem correção",
            )
            _conferir(
                item_conferido.descricao_produto == "ACUCAR REFINADO"
                and item_conferido.descricao_origem is None
                and item_conferido.descricao_produto_anterior is None
                and not db.query(ItemAuditado)
                .filter(
                    ItemAuditado.chave_identificacao
                    == f"XML_{CHAVE_XML_CONFERIDA}_1"
                )
                .first(),
                "item correto não ganha origem/trilha (nada é sobrescrito)",
            )

            # Nota já gravada (dedupe) com PRD...: na execução seguinte o XML chega
            # e corrige a linha existente — sem duplicar nota nem item.
            recebimento_existente = _recebimento(
                chave=CHAVE_XML_EXISTENTE,
                numero="000008888",
                valor=30.0,
                itens=[
                    _item(
                        codigo="PRD00088",
                        descricao="SKIRT STEAK",
                        ean="7898925798125",
                        ncm="0406.10.10",
                        cfop="6.101",
                        unidade="KG",
                        quantidade=1.0,
                        preco=30.0,
                        total=30.0,
                    )
                ],
            )
            # 1ª execução sem XML: a nota entra com a descrição do cadastro interno.
            bsync.sincronizar_recebimentos(
                db,
                empresa,
                dias=7,
                cliente=ClienteFalso({"40": [recebimento_existente]}),
                intervalo=0.0,
            )
            # 2ª execução: a nota é duplicada, mas o XML corrige a linha gravada.
            conserto = bsync.sincronizar_recebimentos(
                db,
                empresa,
                dias=7,
                cliente=ClienteFalso(
                    {"40": [recebimento_existente]},
                    documentos=[
                        _documento_omie(
                            CHAVE_XML_EXISTENTE,
                            [
                                {
                                    "c_prod": "BACON-01",
                                    "xprod": "BACON FATIADO PREMIUM",
                                    "ean": "7898925798125",
                                    "ncm": "0406.10.10",
                                    "cfop": "6101",
                                    "unidade": "KG",
                                }
                            ],
                        )
                    ],
                ),
                intervalo=0.0,
            )
            _conferir(
                conserto["novas"] == 0
                and conserto["duplicadas"] == 1
                and conserto["descricoes"]["corrigidas"] == 1,
                "nota já gravada com PRD... é corrigida na execução seguinte "
                f"(dedupe mantido: {conserto['descricoes']})",
            )
            nota_existente = (
                db.query(NotaFiscal)
                .filter(NotaFiscal.chave_acesso == CHAVE_XML_EXISTENTE)
                .one()
            )
            itens_existentes = (
                db.query(ItemNota)
                .filter(ItemNota.nota_id == nota_existente.id)
                .all()
            )
            _conferir(
                len(itens_existentes) == 1
                and itens_existentes[0].descricao_produto == "BACON FATIADO PREMIUM"
                and itens_existentes[0].descricao_produto_anterior == "SKIRT STEAK",
                "descrição da nota já gravada foi corrigida pelo XML (sem duplicar item)",
            )

            print()
            print("=" * 78)
            print("8) CLI — --sem-marcas só é aceito junto de --simular")
            print("=" * 78)
            # Import tardio: o módulo do CLI lê o .env no import (load_dotenv sem
            # override), então ele só entra aqui, depois do ambiente de teste.
            import io
            from contextlib import redirect_stdout
            from types import SimpleNamespace

            import testar_omie_sync as cli_omie

            _conferir(
                cli_omie._erro_sem_marcas(
                    SimpleNamespace(sem_marcas=True, simular=False)
                ).startswith("--sem-marcas só é permitido junto de --simular"),
                "a validação recusa --sem-marcas sem --simular",
            )
            _conferir(
                cli_omie._erro_sem_marcas(
                    SimpleNamespace(sem_marcas=True, simular=True)
                )
                == "",
                "--sem-marcas continua válido junto de --simular",
            )
            _conferir(
                cli_omie._erro_sem_marcas(
                    SimpleNamespace(sem_marcas=False, simular=False)
                )
                == "",
                "a importação real (sem o flag) continua válida",
            )

            # O main() para ANTES de abrir o banco: nada é gravado nem consultado.
            saida = io.StringIO()
            with redirect_stdout(saida):
                codigo = cli_omie.main(["--sem-marcas", "--dias", "7"])
            texto = saida.getvalue()
            _conferir(
                codigo != 0 and "sempre recalcula" in texto,
                f"o CLI encerra com código {codigo} e explica que a importação "
                "real sempre recalcula marcas",
            )

            print()
            print("=" * 78)
            print(
                "RESULTADO: OK — sincronização Omie (fase 2) validada sem chamar "
                "a API e sem tocar na base real."
            )
            print("=" * 78)

            # Fecha o banco antes de o diretório temporário ser removido (no Windows
            # o arquivo .db continua travado enquanto a engine existir).
            db.close()
            engine.dispose()
    finally:
        for nome, valor in ambiente_original.items():
            if valor is None:
                os.environ.pop(nome, None)
            else:
                os.environ[nome] = valor


if __name__ == "__main__":
    main()

