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
   resumo ``divergencias`` conta notas/itens.

Uso (na raiz do projeto):
    python tests/test_omie_sync.py
"""

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
    """``OmieClient`` de mentira: devolve os lotes por etapa e registra a chamada."""

    def __init__(self, lotes=None):
        self.lotes = lotes or {}
        self.chamadas = []

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
            print("7) CLI — --sem-marcas só é aceito junto de --simular")
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

