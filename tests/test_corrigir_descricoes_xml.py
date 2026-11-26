"""Testes do backfill `corrigir_descricoes_xml.py` (sem tocar na base real).

Cobrem:
- pareamento 1:1 e dry-run não grava (só `planejar` não altera o banco);
- bonificado mantém o índice original do XML no marcador `XML_<chave>_<item>`;
- fallback por EAN quando as contagens divergem (nota antiga/item removido);
- nota sem arquivo, XML ilegível, XML sem itens e XML sem itens no banco;
- grupo A por nota (todos os itens divergentes entram), grupo B intocado e
  `--incluir-b`;
- `c_prod` alinhado só no grupo A; de-para antigo não é movido;
- de-para de A usa o xProd da nota mais recente e não atualiza o par cujo cProd
  foi alinhado;
- XML em subpasta localiza nota com `caminho_xml` vazio pela chave de acesso;
- descrição atual aprovada protege a troca (e `--incluir-aprovadas` libera);
- descrição de destino já aprovada não bloqueia a correção;
- item com `descricao_origem` começando com XML não é reescrito;
- memória de-para automática acompanha e a manual nunca é sobrescrita;
- `--sem-memoria` (atualizar_memoria=False) não toca no de-para;
- backup cria as três tabelas + CSVs (descrições, c_prod e de-para) e
  `aplicar` sem backup aborta;
- trilha da correção em `itens_auditados` (antes/depois/fonte/data).

Roda sem backend e sem a base real:
    python tests/test_corrigir_descricoes_xml.py
"""

import csv
import sys
import tempfile
import traceback
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import corrigir_descricoes_xml as fix
from backend.auditoria_relatorio import limpar_texto
from backend.models import (
    Base,
    Fornecedor,
    ItemAuditado,
    ItemNota,
    MapeamentoFornecedorProduto,
    NotaFiscal,
)

CNPJ = "04.559.938/0001-05"


def _banco_temporario():
    pasta = Path(tempfile.mkdtemp(prefix="corrigir_descricoes_"))
    engine = create_engine(
        "sqlite:///" + str(pasta / "teste.db").replace("\\", "/")
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)(), engine, pasta


def _xml(itens, chave=""):
    """NF-e mínima: o parser só precisa dos `det/prod` na ordem do XML."""
    dets = "".join(
        (
            f'<det nItem="{posicao}"><prod>'
            f"<cProd>{item.get('c_prod', '')}</cProd>"
            f"<cEAN>{item.get('ean') or 'SEM GTIN'}</cEAN>"
            f"<xProd>{item['xprod']}</xProd>"
            f"<NCM>{item.get('ncm', '04061010')}</NCM>"
            f"<CFOP>{item.get('cfop', '6101')}</CFOP>"
            f"<uCom>{item.get('unidade', 'UN')}</uCom>"
            f"<qCom>{item.get('quantidade', '1.0000')}</qCom>"
            f"<vUnCom>{item.get('preco', '10.00')}</vUnCom>"
            f"<vProd>{item.get('total', '10.00')}</vProd>"
            f"<vDesc>{item.get('desconto', '0.00')}</vDesc>"
            "</prod></det>"
        )
        for posicao, item in enumerate(itens, start=1)
    )
    protocolo = f"<chNFe>{chave}</chNFe>" if chave else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe">'
        '<NFe><infNFe versao="4.00">'
        f"{dets}"
        "</infNFe></NFe>"
        f"{protocolo}"
        "</nfeProc>"
    )


def _criar_nota(
    db,
    chave,
    caminho_xml,
    itens,
    *,
    fornecedor=None,
    data_emissao=None,
):
    if fornecedor is None:
        fornecedor = Fornecedor(cnpj=CNPJ, nome="FORNECEDOR TESTE")
        db.add(fornecedor)
        db.flush()
    nota = NotaFiscal(
        chave_acesso=chave,
        numero_nota="1",
        serie="1",
        data_emissao=data_emissao or date(2026, 1, 1),
        caminho_xml=(
            str(caminho_xml) if caminho_xml is not None else None
        ),
    )
    db.add(nota)
    db.flush()
    registros = []
    for dados in itens:
        item = ItemNota(
            nota_id=nota.id,
            fornecedor_id=fornecedor.id,
            ean=dados.get("ean"),
            c_prod=dados.get("c_prod"),
            descricao_produto=dados["descricao"],
            descricao_origem=dados.get("origem"),
            ncm=dados.get("ncm", "04061010"),
        )
        db.add(item)
        registros.append(item)
    db.commit()
    return nota, fornecedor, registros


def _reabrir(db, modelo, identificador):
    db.expire_all()
    return db.get(modelo, identificador)


def _aprovar(db, descricao, grupo="EAN_7890000000011"):
    db.add(
        ItemAuditado(
            chave_identificacao=(
                f"DESC_{grupo}_{limpar_texto(descricao)}"
            ),
            auditado=True,
            motivo="manual",
        )
    )
    db.commit()


def test_corrige_um_para_um_e_dry_run_nao_grava():
    db, engine, pasta = _banco_temporario()
    try:
        caminho = pasta / "nota.xml"
        caminho.write_text(
            _xml(
                [
                    {
                        "c_prod": "A1",
                        "ean": "7890000000011",
                        "xprod": "PRODUTO NOVO",
                    },
                    {
                        "c_prod": "A2",
                        "ean": "7890000000028",
                        "xprod": "OUTRO PRODUTO",
                    },
                ]
            ),
            encoding="utf-8",
        )
        _nota, _fornecedor, itens = _criar_nota(
            db,
            "CHAVE1",
            caminho,
            [
                {
                    "ean": "7890000000011",
                    "c_prod": "A1",
                    "descricao": "PRODUTO ERRADO",
                },
                {
                    "ean": "7890000000028",
                    "c_prod": "A2",
                    "descricao": "OUTRO PRODUTO",
                },
            ],
        )

        plano = fix.planejar(db)
        assert plano.resumo["notas"] == 1
        assert plano.resumo["xml_itens"] == 2
        assert plano.resumo["bonificados"] == 0
        assert plano.resumo["pares"] == 2
        assert plano.resumo["db_sem_par"] == 0
        assert plano.resumo["xml_sem_par"] == 0
        assert plano.resumo["conferidas"] == 1
        assert plano.resumo["divergentes"] == 1
        assert plano.resumo["a_corrigir"] == 1
        assert plano.resumo["notas_com_correcao"] == 1
        assert len(plano.correcoes) == 1
        correcao = plano.correcoes[0]
        assert correcao.item.id == itens[0].id
        assert correcao.indice == 1
        assert correcao.antes == "PRODUTO ERRADO"
        assert correcao.depois == "PRODUTO NOVO"
        assert correcao.c_prod_xml == "A1"
        assert correcao.ean_xml == "7890000000011"

        # Dry-run: planejar não altera descrição nem cria trilha.
        assert (
            _reabrir(db, ItemNota, itens[0].id).descricao_produto
            == "PRODUTO ERRADO"
        )
        assert db.query(ItemAuditado).count() == 0

        resumo = fix.aplicar(db, plano, exigir_backup=False)
        assert resumo["corrigidas"] == 1
        assert resumo["ignoradas"] == 0
        assert resumo["marcadores_novos"] == 1
        assert resumo["marcadores_atualizados"] == 0

        item = _reabrir(db, ItemNota, itens[0].id)
        assert item.descricao_produto == "PRODUTO NOVO"
        assert item.descricao_produto_anterior == "PRODUTO ERRADO"
        assert item.descricao_origem == fix.FONTE_DESCRICAO

        marcador = (
            db.query(ItemAuditado)
            .filter(ItemAuditado.chave_identificacao == "XML_CHAVE1_1")
            .one()
        )
        assert marcador.auditado is True
        assert marcador.motivo == "descricao_xml"
        assert marcador.descricao_antes == "PRODUTO ERRADO"
        assert marcador.descricao_depois == "PRODUTO NOVO"
        assert marcador.fonte == fix.FONTE_DESCRICAO
        assert marcador.registrado_em

        # O item conferido não tem trilha (nada foi trocado).
        assert (
            db.query(ItemAuditado)
            .filter(ItemAuditado.chave_identificacao == "XML_CHAVE1_2")
            .first()
            is None
        )
    finally:
        db.close()
        engine.dispose()


def test_bonificado_nao_desloca_o_indice_do_xml():
    db, engine, pasta = _banco_temporario()
    try:
        caminho = pasta / "bonificado.xml"
        caminho.write_text(
            _xml(
                [
                    {
                        "c_prod": "A1",
                        "ean": "7890000000011",
                        "xprod": "PRODUTO NOVO A",
                    },
                    {
                        "c_prod": "B1",
                        "ean": "7890000000097",
                        "xprod": "PRODUTO BONIFICADO",
                        "preco": "0.00",
                        "total": "0.00",
                        "cfop": "6910",
                    },
                    {
                        "c_prod": "C1",
                        "ean": "7890000000035",
                        "xprod": "PRODUTO NOVO C",
                    },
                ]
            ),
            encoding="utf-8",
        )
        _nota, _fornecedor, itens = _criar_nota(
            db,
            "CHAVE_BONIF",
            caminho,
            [
                {
                    "ean": "7890000000011",
                    "c_prod": "A1",
                    "descricao": "PRODUTO ANTIGO A",
                },
                {
                    "ean": "7890000000035",
                    "c_prod": "C1",
                    "descricao": "PRODUTO ANTIGO C",
                },
            ],
        )

        plano = fix.planejar(db)
        assert plano.resumo["bonificados"] == 1
        assert plano.resumo["xml_itens"] == 2
        assert plano.resumo["pares"] == 2
        assert len(plano.correcoes) == 2
        # O bonificado entre A e C NÃO pode fazer C virar o item 2 do marcador.
        assert [correcao.indice for correcao in plano.correcoes] == [1, 3]
        assert [correcao.item.id for correcao in plano.correcoes] == [
            itens[0].id,
            itens[1].id,
        ]

        fix.aplicar(db, plano, exigir_backup=False)
        chaves = [
            linha[0]
            for linha in db.query(ItemAuditado.chave_identificacao)
            .order_by(ItemAuditado.chave_identificacao)
            .all()
        ]
        assert chaves == ["XML_CHAVE_BONIF_1", "XML_CHAVE_BONIF_3"]
    finally:
        db.close()
        engine.dispose()


def test_fallback_por_ean_quando_a_contagem_difere():
    db, engine, pasta = _banco_temporario()
    try:
        caminho = pasta / "fallback.xml"
        caminho.write_text(
            _xml(
                [
                    {
                        "c_prod": "A1",
                        "ean": "7890000000011",
                        "xprod": "PRODUTO NOVO A",
                    },
                    {
                        "c_prod": "B1",
                        "ean": "7890000000028",
                        "xprod": "PRODUTO NOVO B",
                    },
                    {
                        "c_prod": "C1",
                        "ean": "7890000000035",
                        "xprod": "PRODUTO NOVO C",
                    },
                ]
            ),
            encoding="utf-8",
        )
        _nota, _fornecedor, itens = _criar_nota(
            db,
            "CHAVE_FALLBACK",
            caminho,
            [
                {
                    "ean": "7890000000011",
                    "c_prod": "A1",
                    "descricao": "PRODUTO ANTIGO A",
                },
                {
                    "ean": "7890000000035",
                    "c_prod": "C1",
                    "descricao": "PRODUTO ANTIGO C",
                },
            ],
        )

        plano = fix.planejar(db)
        assert plano.resumo["xml_itens"] == 3
        assert plano.resumo["pares"] == 2
        assert plano.resumo["xml_sem_par"] == 1
        assert plano.resumo["db_sem_par"] == 0
        assert [correcao.item.id for correcao in plano.correcoes] == [
            itens[0].id,
            itens[1].id,
        ]
        assert [correcao.indice for correcao in plano.correcoes] == [1, 3]
        assert [correcao.depois for correcao in plano.correcoes] == [
            "PRODUTO NOVO A",
            "PRODUTO NOVO C",
        ]
    finally:
        db.close()
        engine.dispose()


def test_notas_sem_arquivo_xml_invalido_e_sem_itens():
    db, engine, pasta = _banco_temporario()
    try:
        nota, fornecedor, _itens = _criar_nota(
            db,
            "CHAVE_SEM_ARQUIVO",
            pasta / "nao_existe.xml",
            [{"descricao": "PRODUTO X"}],
        )
        assert nota.caminho_xml.endswith("nao_existe.xml")

        _criar_nota(
            db,
            "CHAVE_INVALIDA",
            pasta / "invalido.xml",
            [{"descricao": "PRODUTO Y"}],
            fornecedor=fornecedor,
        )
        (pasta / "invalido.xml").write_text(
            "isto nao e um XML", encoding="utf-8"
        )

        caminho_vazio = pasta / "vazio.xml"
        caminho_vazio.write_text(_xml([]), encoding="utf-8")
        _criar_nota(
            db,
            "CHAVE_XML_VAZIO",
            caminho_vazio,
            [{"descricao": "PRODUTO Z"}],
            fornecedor=fornecedor,
        )

        caminho_sem_itens = pasta / "sem_itens_banco.xml"
        caminho_sem_itens.write_text(
            _xml(
                [
                    {
                        "c_prod": "A1",
                        "ean": "7890000000011",
                        "xprod": "PRODUTO NOVO",
                    }
                ]
            ),
            encoding="utf-8",
        )
        _criar_nota(
            db,
            "CHAVE_SEM_ITENS",
            caminho_sem_itens,
            [],
            fornecedor=fornecedor,
        )

        # Nota sem caminho_xml não entra na varredura.
        _criar_nota(
            db,
            "CHAVE_SEM_CAMINHO",
            None,
            [{"descricao": "PRODUTO FORA DO ESCOPO"}],
            fornecedor=fornecedor,
        )

        plano = fix.planejar(db)
        assert plano.resumo["notas"] == 4
        assert plano.resumo["sem_arquivo"] == 1
        assert plano.resumo["xml_invalido"] == 2
        assert plano.resumo["nota_sem_itens"] == 1
        assert plano.resumo["xml_itens"] == 1
        assert plano.resumo["pares"] == 0
        assert plano.resumo["a_corrigir"] == 0
        assert not plano.correcoes
    finally:
        db.close()
        engine.dispose()


def test_descricao_atual_aprovada_protege_a_troca():
    db, engine, pasta = _banco_temporario()
    try:
        caminho = pasta / "aprovada.xml"
        caminho.write_text(
            _xml(
                [
                    {
                        "c_prod": "A1",
                        "ean": "7890000000011",
                        "xprod": "PRODUTO NOVO",
                    }
                ]
            ),
            encoding="utf-8",
        )
        _nota, _fornecedor, itens = _criar_nota(
            db,
            "CHAVE_APROVADA",
            caminho,
            [
                {
                    "ean": "7890000000011",
                    "c_prod": "A1",
                    "descricao": "PRODUTO ANTIGO",
                }
            ],
        )
        _aprovar(db, "PRODUTO ANTIGO", grupo="EAN_7890000000011")

        plano = fix.planejar(db)
        assert plano.resumo["divergentes"] == 1
        assert plano.resumo["protegidas"] == 1
        assert plano.resumo["a_corrigir"] == 0
        assert not plano.correcoes
        assert len(plano.protegidas) == 1
        assert plano.protegidas[0].item.id == itens[0].id
        assert (
            _reabrir(db, ItemNota, itens[0].id).descricao_produto
            == "PRODUTO ANTIGO"
        )

        liberado = fix.planejar(db, incluir_aprovadas=True)
        assert liberado.resumo["protegidas"] == 0
        assert liberado.resumo["a_corrigir"] == 1
        assert len(liberado.correcoes) == 1
        assert liberado.correcoes[0].depois == "PRODUTO NOVO"
    finally:
        db.close()
        engine.dispose()


def test_descricao_de_destino_aprovada_nao_bloqueia():
    db, engine, pasta = _banco_temporario()
    try:
        caminho = pasta / "destino_aprovado.xml"
        caminho.write_text(
            _xml(
                [
                    {
                        "c_prod": "A1",
                        "ean": "7890000000011",
                        "xprod": "PRODUTO NOVO",
                    }
                ]
            ),
            encoding="utf-8",
        )
        _nota, _fornecedor, itens = _criar_nota(
            db,
            "CHAVE_DESTINO",
            caminho,
            [
                {
                    "ean": "7890000000011",
                    "c_prod": "A1",
                    "descricao": "PRODUTO ANTIGO",
                }
            ],
        )
        # A descrição ATUAL e a do XML já foram aprovadas: a troca é segura.
        _aprovar(db, "PRODUTO ANTIGO", grupo="EAN_7890000000011")
        _aprovar(db, "PRODUTO NOVO", grupo="EAN_7890000000011")

        plano = fix.planejar(db)
        assert plano.resumo["protegidas"] == 0
        assert plano.resumo["a_corrigir"] == 1
        assert len(plano.correcoes) == 1
        assert plano.correcoes[0].item.id == itens[0].id
    finally:
        db.close()
        engine.dispose()


def test_item_com_origem_xml_nao_e_reescrito():
    db, engine, pasta = _banco_temporario()
    try:
        caminho = pasta / "origem_xml.xml"
        caminho.write_text(
            _xml(
                [
                    {
                        "c_prod": "A1",
                        "ean": "7890000000011",
                        "xprod": "PRODUTO NOVO",
                    }
                ]
            ),
            encoding="utf-8",
        )
        _nota, _fornecedor, itens = _criar_nota(
            db,
            "CHAVE_ORIGEM_XML",
            caminho,
            [
                {
                    "ean": "7890000000011",
                    "c_prod": "A1",
                    "descricao": "PRODUTO ANTIGO",
                    "origem": "XML etapa 40",
                }
            ],
        )

        plano = fix.planejar(db)
        assert plano.resumo["divergentes"] == 1
        assert plano.resumo["ja_origem_xml"] == 1
        assert plano.resumo["a_corrigir"] == 0
        assert not plano.correcoes
        item = _reabrir(db, ItemNota, itens[0].id)
        assert item.descricao_produto == "PRODUTO ANTIGO"
        assert item.descricao_origem == "XML etapa 40"
    finally:
        db.close()
        engine.dispose()


def test_memoria_automatica_acompanha_a_correcao():
    db, engine, pasta = _banco_temporario()
    try:
        caminho = pasta / "memoria_auto.xml"
        caminho.write_text(
            _xml(
                [
                    {
                        "c_prod": "A1",
                        "ean": "7890000000011",
                        "xprod": "PRODUTO NOVO",
                    }
                ]
            ),
            encoding="utf-8",
        )
        _nota, fornecedor, _itens = _criar_nota(
            db,
            "CHAVE_MEMORIA_AUTO",
            caminho,
            [
                {
                    "ean": "7890000000011",
                    "c_prod": "A1",
                    "descricao": "PRODUTO ANTIGO",
                }
            ],
        )
        mapeamento = MapeamentoFornecedorProduto(
            cnpj_fornecedor=fornecedor.cnpj,
            c_prod="A1",
            descricao_padrao="PRODUTO ANTIGO",
            origem="automatico",
        )
        db.add(mapeamento)
        db.commit()

        plano = fix.planejar(db)
        assert plano.resumo["memoria"] == 1
        assert len(plano.correcoes) == 1
        assert len(plano.atualizacoes_de_para) == 1
        atualizacao = plano.atualizacoes_de_para[0]
        assert atualizacao.mapeamento.id == mapeamento.id
        assert atualizacao.antes == "PRODUTO ANTIGO"
        assert atualizacao.depois == "PRODUTO NOVO"

        fix.aplicar(db, plano, exigir_backup=False)
        atualizado = _reabrir(
            db, MapeamentoFornecedorProduto, mapeamento.id
        )
        assert atualizado.descricao_padrao == "PRODUTO NOVO"
        assert atualizado.origem == "automatico"
        assert atualizado.atualizado_em
    finally:
        db.close()
        engine.dispose()


def test_memoria_manual_e_sem_memoria_nao_sao_sobrescritas():
    db, engine, pasta = _banco_temporario()
    try:
        caminho = pasta / "memoria_manual.xml"
        caminho.write_text(
            _xml(
                [
                    {
                        "c_prod": "A1",
                        "ean": "7890000000011",
                        "xprod": "PRODUTO NOVO",
                    }
                ]
            ),
            encoding="utf-8",
        )
        _nota, fornecedor, _itens = _criar_nota(
            db,
            "CHAVE_MEMORIA_MANUAL",
            caminho,
            [
                {
                    "ean": "7890000000011",
                    "c_prod": "A1",
                    "descricao": "PRODUTO ANTIGO",
                }
            ],
        )
        manual = MapeamentoFornecedorProduto(
            cnpj_fornecedor=fornecedor.cnpj,
            c_prod="A1",
            descricao_padrao="DESCRICAO APROVADA PELO ADMIN",
            origem="manual",
        )
        db.add(manual)
        db.commit()

        plano = fix.planejar(db)
        assert plano.resumo["memoria"] == 0
        assert not plano.atualizacoes_de_para
        fix.aplicar(db, plano, exigir_backup=False)
        manual = _reabrir(db, MapeamentoFornecedorProduto, manual.id)
        assert manual.descricao_padrao == "DESCRICAO APROVADA PELO ADMIN"
        assert manual.origem == "manual"

        # Segunda nota, agora com de-para automático, mas --sem-memoria.
        caminho2 = pasta / "sem_memoria.xml"
        caminho2.write_text(
            _xml(
                [
                    {
                        "c_prod": "B1",
                        "ean": "7890000000028",
                        "xprod": "SEGUNDO PRODUTO NOVO",
                    }
                ]
            ),
            encoding="utf-8",
        )
        _criar_nota(
            db,
            "CHAVE_SEM_MEMORIA",
            caminho2,
            [
                {
                    "ean": "7890000000028",
                    "c_prod": "B1",
                    "descricao": "SEGUNDO ITEM ERRADO",
                }
            ],
            fornecedor=fornecedor,
        )
        automatico = MapeamentoFornecedorProduto(
            cnpj_fornecedor=fornecedor.cnpj,
            c_prod="B1",
            descricao_padrao="SEGUNDO ITEM ERRADO",
            origem="automatico",
        )
        db.add(automatico)
        db.commit()

        sem_memoria = fix.planejar(db, atualizar_memoria=False)
        assert sem_memoria.resumo["memoria"] == 0
        assert not sem_memoria.atualizacoes_de_para
        assert [
            c for c in sem_memoria.correcoes if c.c_prod_xml == "B1"
        ]
        fix.aplicar(db, sem_memoria, exigir_backup=False)
        automatico = _reabrir(
            db, MapeamentoFornecedorProduto, automatico.id
        )
        assert automatico.descricao_padrao == "SEGUNDO ITEM ERRADO"
    finally:
        db.close()
        engine.dispose()


def test_backup_cria_tabelas_e_csv_e_aplicar_usa_o_backup():
    db, engine, pasta = _banco_temporario()
    try:
        caminho = pasta / "backup.xml"
        caminho.write_text(
            _xml(
                [
                    {
                        "c_prod": "A1",
                        "ean": "7890000000011",
                        "xprod": "PRODUTO NOVO",
                    }
                ]
            ),
            encoding="utf-8",
        )
        _nota, _fornecedor, itens = _criar_nota(
            db,
            "CHAVE_BACKUP",
            caminho,
            [
                {
                    "ean": "7890000000011",
                    "c_prod": "A1",
                    "descricao": "PRODUTO ANTIGO",
                }
            ],
        )
        plano = fix.planejar(db)
        info = fix._backup(db, plano, pasta=pasta / "backups")

        tabelas = set(inspect(engine).get_table_names())
        assert info["tabelas"]["itens"]["tabela"] in tabelas
        assert info["tabelas"]["auditados"]["tabela"] in tabelas
        assert info["tabelas"]["mapeamentos"]["tabela"] in tabelas
        assert info["tabelas"]["itens"]["linhas"] == 1
        assert info["tabelas"]["auditados"]["linhas"] == 0
        assert info["tabelas"]["mapeamentos"]["linhas"] == 0
        assert info["csv_protegidas"] == ""

        csv_backup = Path(info["csv"])
        assert csv_backup.is_file()
        with csv_backup.open(encoding="utf-8", newline="") as arquivo:
            linhas = list(csv.reader(arquivo))
        assert linhas[0][0] == "nota_id"
        assert "c_prod_xml" in linhas[0]
        assert "descricao_antes" in linhas[0]
        assert "descricao_depois" in linhas[0]
        assert len(linhas) == 2
        assert linhas[1][8] == "PRODUTO ANTIGO"
        assert linhas[1][9] == "PRODUTO NOVO"
        assert Path(info["csv_cprod"]).is_file()
        assert Path(info["csv_de_para"]).is_file()

        # `aplicar` sem --backup acha a cópia de hoje e confere a contagem.
        resumo = fix.aplicar(db, plano)
        assert resumo["corrigidas"] == 1
        assert (
            _reabrir(db, ItemNota, itens[0].id).descricao_produto
            == "PRODUTO NOVO"
        )
    finally:
        db.close()
        engine.dispose()


def test_backup_exporta_csv_das_protegidas():
    db, engine, pasta = _banco_temporario()
    try:
        caminho = pasta / "protegidas.xml"
        caminho.write_text(
            _xml(
                [
                    {
                        "c_prod": "A1",
                        "ean": "7890000000011",
                        "xprod": "PRODUTO NOVO A",
                    },
                    {
                        "c_prod": "B1",
                        "ean": "7890000000028",
                        "xprod": "PRODUTO NOVO B",
                    },
                ]
            ),
            encoding="utf-8",
        )
        _nota, _fornecedor, _itens = _criar_nota(
            db,
            "CHAVE_PROTEGIDAS",
            caminho,
            [
                {
                    "ean": "7890000000011",
                    "c_prod": "A1",
                    "descricao": "PRODUTO ANTIGO A",
                },
                {
                    "ean": "7890000000028",
                    "c_prod": "B1",
                    "descricao": "PRODUTO ANTIGO B",
                },
            ],
        )
        _aprovar(db, "PRODUTO ANTIGO A", grupo="EAN_7890000000011")

        plano = fix.planejar(db)
        assert len(plano.correcoes) == 1
        assert len(plano.protegidas) == 1
        info = fix._backup(db, plano, pasta=pasta / "backups")

        csv_protegidas = Path(info["csv_protegidas"])
        assert csv_protegidas.is_file()
        with csv_protegidas.open(encoding="utf-8", newline="") as arquivo:
            linhas = list(csv.reader(arquivo))
        assert linhas[0][-1] == "motivo"
        assert len(linhas) == 2
        assert linhas[1][6] == "PRODUTO ANTIGO A"
        assert linhas[1][7] == "PRODUTO NOVO A"
        assert "aprovada" in linhas[1][8]
    finally:
        db.close()
        engine.dispose()


def test_aplicar_sem_backup_aborta_e_nao_grava():
    db, engine, pasta = _banco_temporario()
    try:
        caminho = pasta / "sem_backup.xml"
        caminho.write_text(
            _xml(
                [
                    {
                        "c_prod": "A1",
                        "ean": "7890000000011",
                        "xprod": "PRODUTO NOVO",
                    }
                ]
            ),
            encoding="utf-8",
        )
        _nota, _fornecedor, itens = _criar_nota(
            db,
            "CHAVE_SEM_BACKUP",
            caminho,
            [
                {
                    "ean": "7890000000011",
                    "c_prod": "A1",
                    "descricao": "PRODUTO ANTIGO",
                }
            ],
        )
        plano = fix.planejar(db)
        try:
            fix.aplicar(db, plano)
        except SystemExit as erro:
            assert "backup" in str(erro).lower()
        else:
            raise AssertionError("aplicar sem backup deveria abortar")

        assert (
            _reabrir(db, ItemNota, itens[0].id).descricao_produto
            == "PRODUTO ANTIGO"
        )
        assert db.query(ItemAuditado).count() == 0
    finally:
        db.close()
        engine.dispose()


def test_grupo_a_por_nota_e_grupo_b_intocado():
    db, engine, pasta = _banco_temporario()
    try:
        caminho_a = pasta / "grupo_a.xml"
        caminho_a.write_text(
            _xml(
                [
                    {
                        "c_prod": "A1",
                        "ean": "7890000000011",
                        "xprod": "PRODUTO NOVO ALFA",
                    },
                    {
                        "c_prod": "A2",
                        "ean": "7890000000028",
                        "xprod": "PRODUTO BETA - 2",
                    },
                ]
            ),
            encoding="utf-8",
        )
        _nota_a, fornecedor, itens_a = _criar_nota(
            db,
            "CHAVE_GRUPO_A",
            caminho_a,
            [
                {
                    "ean": "7890000000011",
                    "c_prod": "A1",
                    "descricao": "PRODUTO ANTIGO ALFA",
                },
                {
                    "ean": "7890000000028",
                    "c_prod": "A2",
                    "descricao": "PRODUTO BETA - 1",
                },
            ],
        )
        caminho_b = pasta / "grupo_b.xml"
        caminho_b.write_text(
            _xml(
                [
                    {
                        "c_prod": "B1",
                        "ean": "7890000000035",
                        "xprod": "PRODUTO GAMA - 2",
                    }
                ]
            ),
            encoding="utf-8",
        )
        _nota_b, _fornecedor_b, itens_b = _criar_nota(
            db,
            "CHAVE_GRUPO_B",
            caminho_b,
            [
                {
                    "ean": "7890000000035",
                    "c_prod": "B1",
                    "descricao": "PRODUTO GAMA - 1",
                }
            ],
            fornecedor=fornecedor,
        )

        plano = fix.planejar(db)
        assert plano.resumo["grupo_a_itens"] == 2
        assert plano.resumo["grupo_a_notas"] == 1
        assert plano.resumo["grupo_b_itens"] == 1
        assert plano.resumo["grupo_b_notas"] == 1
        assert {c.item.id for c in plano.correcoes} == {
            itens_a[0].id,
            itens_a[1].id,
        }
        assert [c.item.id for c in plano.grupo_b] == [itens_b[0].id]
        assert (
            _reabrir(db, ItemNota, itens_b[0].id).descricao_produto
            == "PRODUTO GAMA - 1"
        )

        com_b = fix.planejar(db, incluir_b=True)
        assert com_b.resumo["a_corrigir"] == 3
        assert {c.item.id for c in com_b.correcoes} == {
            itens_a[0].id,
            itens_a[1].id,
            itens_b[0].id,
        }
    finally:
        db.close()
        engine.dispose()


def test_cprod_do_grupo_a_alinhado_e_b_intocado():
    db, engine, pasta = _banco_temporario()
    try:
        caminho_a = pasta / "cprod_a.xml"
        caminho_a.write_text(
            _xml(
                [
                    {
                        "c_prod": "39",
                        "ean": "7890000000011",
                        "xprod": "PRODUTO NOVO",
                    }
                ]
            ),
            encoding="utf-8",
        )
        _nota_a, fornecedor, itens_a = _criar_nota(
            db,
            "CHAVE_CPROD_A",
            caminho_a,
            [
                {
                    "ean": "7890000000011",
                    "c_prod": "525",
                    "descricao": "PRODUTO ANTIGO",
                }
            ],
        )
        antigo = MapeamentoFornecedorProduto(
            cnpj_fornecedor=fornecedor.cnpj,
            c_prod="525",
            descricao_padrao="PRODUTO ANTIGO",
            origem="automatico",
        )
        db.add(antigo)
        db.commit()

        caminho_b = pasta / "cprod_b.xml"
        caminho_b.write_text(
            _xml(
                [
                    {
                        "c_prod": "222",
                        "ean": "7890000000028",
                        "xprod": "PRODUTO GAMA - 2",
                    }
                ]
            ),
            encoding="utf-8",
        )
        _nota_b, _fornecedor_b, itens_b = _criar_nota(
            db,
            "CHAVE_CPROD_B",
            caminho_b,
            [
                {
                    "ean": "7890000000028",
                    "c_prod": "111",
                    "descricao": "PRODUTO GAMA - 1",
                }
            ],
            fornecedor=fornecedor,
        )

        plano = fix.planejar(db)
        assert plano.resumo["c_prod"] == 1
        assert len(plano.correcoes_c_prod) == 1
        assert plano.correcoes_c_prod[0].item.id == itens_a[0].id
        assert plano.correcoes_c_prod[0].antes == "525"
        assert plano.correcoes_c_prod[0].depois == "39"
        # O par mudou de cProd: não entra nos 15 pares de descricao_padrao.
        assert not plano.atualizacoes_de_para

        fix.aplicar(db, plano, exigir_backup=False)
        assert _reabrir(db, ItemNota, itens_a[0].id).c_prod == "39"
        assert _reabrir(db, ItemNota, itens_b[0].id).c_prod == "111"
        assert (
            _reabrir(db, MapeamentoFornecedorProduto, antigo.id).c_prod
            == "525"
        )
    finally:
        db.close()
        engine.dispose()


def test_de_para_usa_a_nota_mais_recente():
    db, engine, pasta = _banco_temporario()
    try:
        antigo_xml = pasta / "de_para_antigo.xml"
        antigo_xml.write_text(
            _xml(
                [
                    {
                        "c_prod": "A1",
                        "ean": "7890000000011",
                        "xprod": "PRODUTO NOVO ANTIGO",
                    }
                ]
            ),
            encoding="utf-8",
        )
        _nota, fornecedor, _itens = _criar_nota(
            db,
            "CHAVE_DE_PARA_ANTIGO",
            antigo_xml,
            [
                {
                    "ean": "7890000000011",
                    "c_prod": "A1",
                    "descricao": "PRODUTO BASE ERRADA",
                }
            ],
            data_emissao=date(2026, 1, 1),
        )
        recente_xml = pasta / "de_para_recente.xml"
        recente_xml.write_text(
            _xml(
                [
                    {
                        "c_prod": "A1",
                        "ean": "7890000000011",
                        "xprod": "PRODUTO NOVO RECENTE",
                    }
                ]
            ),
            encoding="utf-8",
        )
        _criar_nota(
            db,
            "CHAVE_DE_PARA_RECENTE",
            recente_xml,
            [
                {
                    "ean": "7890000000011",
                    "c_prod": "A1",
                    "descricao": "PRODUTO BASE ERRADA",
                }
            ],
            fornecedor=fornecedor,
            data_emissao=date(2026, 1, 1) + timedelta(days=30),
        )
        mapeamento = MapeamentoFornecedorProduto(
            cnpj_fornecedor=fornecedor.cnpj,
            c_prod="A1",
            descricao_padrao="PRODUTO BASE ERRADA",
            origem="automatico",
        )
        db.add(mapeamento)
        db.commit()

        plano = fix.planejar(db)
        assert len(plano.correcoes) == 2
        assert len(plano.atualizacoes_de_para) == 1
        atualizacao = plano.atualizacoes_de_para[0]
        assert atualizacao.mapeamento.id == mapeamento.id
        assert atualizacao.depois == "PRODUTO NOVO RECENTE"
        assert atualizacao.nota_id > 0

        fix.aplicar(db, plano, exigir_backup=False)
        assert (
            _reabrir(db, MapeamentoFornecedorProduto, mapeamento.id).descricao_padrao
            == "PRODUTO NOVO RECENTE"
        )
    finally:
        db.close()
        engine.dispose()


def test_pasta_resolve_nota_sem_caminho_xml():
    db, engine, pasta = _banco_temporario()
    try:
        pasta_xml = pasta / "xmls" / "2026" / "11"
        pasta_xml.mkdir(parents=True)
        chave = "35261111240691000117550010004319531805024038"
        (pasta_xml / f"{chave}.xml").write_text(
            _xml(
                [
                    {
                        "c_prod": "39",
                        "ean": "7890000000011",
                        "xprod": "PRODUTO NOVO",
                    }
                ],
                chave=chave,
            ),
            encoding="utf-8",
        )
        _nota, _fornecedor, itens = _criar_nota(
            db,
            chave,
            None,
            [
                {
                    "ean": "7890000000011",
                    "c_prod": "39",
                    "descricao": "PRODUTO ANTIGO",
                }
            ],
        )

        sem_pasta = fix.planejar(db)
        assert sem_pasta.resumo["notas"] == 0
        assert not sem_pasta.correcoes

        plano = fix.planejar(db, pasta=pasta / "xmls")
        assert plano.resumo["notas"] == 1
        assert plano.resumo["a_corrigir"] == 1
        assert plano.correcoes[0].item.id == itens[0].id
        assert plano.correcoes[0].depois == "PRODUTO NOVO"
    finally:
        db.close()
        engine.dispose()


TESTS = [
    test_corrige_um_para_um_e_dry_run_nao_grava,
    test_bonificado_nao_desloca_o_indice_do_xml,
    test_fallback_por_ean_quando_a_contagem_difere,
    test_notas_sem_arquivo_xml_invalido_e_sem_itens,
    test_descricao_atual_aprovada_protege_a_troca,
    test_descricao_de_destino_aprovada_nao_bloqueia,
    test_item_com_origem_xml_nao_e_reescrito,
    test_memoria_automatica_acompanha_a_correcao,
    test_memoria_manual_e_sem_memoria_nao_sao_sobrescritas,
    test_backup_cria_tabelas_e_csv_e_aplicar_usa_o_backup,
    test_backup_exporta_csv_das_protegidas,
    test_aplicar_sem_backup_aborta_e_nao_grava,
    test_grupo_a_por_nota_e_grupo_b_intocado,
    test_cprod_do_grupo_a_alinhado_e_b_intocado,
    test_de_para_usa_a_nota_mais_recente,
    test_pasta_resolve_nota_sem_caminho_xml,
]


def main():
    falhas = 0
    for funcao in TESTS:
        try:
            funcao()
        except Exception:
            falhas += 1
            print(f"FALHOU: {funcao.__name__}")
            traceback.print_exc()
        else:
            print(f"OK: {funcao.__name__}")
    print(f"\n{len(TESTS) - falhas}/{len(TESTS)} testes passaram.")
    return 1 if falhas else 0


if __name__ == "__main__":
    raise SystemExit(main())

