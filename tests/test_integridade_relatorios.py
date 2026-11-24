"""Testes das regras de integridade dos relatórios.

Cobre as regras de negócio dos relatórios (Evolução, Comparativo e Impacto):

1. produto com descrição PENDENTE de aprovação na Auditoria não entra nos cálculos;
2. variação de 50% ou mais é aberração: não é aumento/economia, vai para o fim da lista
   com o alerta "Verificar Variação Brusca" e é registrada automaticamente na Auditoria
   (motivo "aberrante", sem esconder o item da tabela). Quando o auditor VALIDA, a
   variação é considerada real e volta a contar como aumento/economia;
3. item bonificado/doação/brinde não entra na média de preço: CFOP 5910/6910 gravado no
   item (importação e backfill), valor unitário R$ 0,00, desconto total ou texto de
   bonificação;
4. a tabela de Impacto mostra a DATA da compra no tooltip de Preço Atual/Preço Anterior,
   colore a variação (queda/aumento) e mantém as aberrações no fim em qualquer ordenação;
5. filtro de loja com a opção "Minhas Lojas (Todas)" (grupo de CNPJs) e tabelas com
   cabeçalho fixo + barra de rolagem horizontal visível;
6. autenticação: registro/login por e-mail e senha (token JWT), token obrigatório nas
   rotas de lojas, cada empresa pertence a uma conta e o front mostra a tela de login
   quando não há sessão (AppTest);
7. autorização da Auditoria: TODAS as rotas de auditoria (/api/auditados,
   /api/auditados/pendentes, /api/auditar, /api/auditar/pendente, DELETE /api/auditar,
   /api/nota/{item_id} e /api/regras-agrupamento) exigem `is_admin` — conta comum recebe
   403 e o menu da Auditoria nem aparece no front;
8. autenticação em dois fatores (TOTP): /api/auth/2fa/setup devolve o segredo e o QR Code,
   /api/auth/2fa/verify confirma o código de 6 dígitos (`pyotp`) e devolve o token do
   segundo fator; sem `X-2FA-Token` as rotas de auditoria respondem 401 e o admin sem 2FA
   configurado vê o QR Code no lugar dos dados;
9. barreira do certificado digital: /api/empresas/cadastrar é multipart e só vincula a
   loja com o .pfx/.p12 e a senha do certificado (vazio, senha errada, arquivo vencido ou
   de outro CNPJ = 400, nada é gravado);
10. LOGIN do administrador fail-closed: a senha correta NÃO devolve sessão — a resposta
   traz um desafio de 2FA e a sessão só nasce em /api/auth/2fa/verify, depois do código
   de 6 dígitos (admin sem 2FA recebe 403 e configura o QR Code já no login). Conta
   comum (restaurante) continua entrando só com e-mail e senha;
11. inteligência de marcas: `itens_nota.marca` é inferida pela variação das descrições do
   mesmo NCM (backend/marcas.py), entra na chave inteligente Produto + Marca (médias
   separadas entre marcas) e alimenta o tooltip de marcas substitutas dos relatórios
   (mesmo NCM + descrição > 80% parecida) — a Média de Mercado de cada grupo continua
   sendo a média exata das compras do seu EAN/descrição;
12. cadastro de loja SEMPRE renomeia com o **Nome Fantasia da BrasilAPI**
   (`_atualizar_dados_cadastrais`): a loja renomeada na Receita não fica com o nome antigo
   e a reserva de consulta indisponível nunca grava "Loja Exemplo" por cima do nome real;
13. "Excluir Certificado" (DELETE /api/empresas/{id}/certificado): o dono da loja ou o
   administrador (suporte em qualquer loja) apaga (401 sem sessão, 403 loja de outra
   conta para conta comum, 404 id inexistente) e a loja, o dono e as notas continuam
   intactos — só titular/validade/data do certificado saem; o admin vê todas as lojas em
   /api/empresas/vinculadas com `dono_email` e não vira dono ao reenviar o certificado;
14. motor de alertas (WhatsApp): `/api/alertas/configurar` grava as preferências da conta
   (telefone com DDI automático, variação mínima, janela e lojas da própria conta) e
   `/api/alertas/gerar-mensagens` SIMULA o disparo montando o texto com os aumentos e as
   divergências (≥ 50%), sem enviar nada; o token da API do WhatsApp é global e só o
   administrador com 2FA o cadastra (/api/admin/configuracoes), sempre mascarado na
   resposta.
15. consenso com IA (DeepSeek): `/api/auditoria/ia/validar-marcas` e
    `/api/auditoria/ia/resolver-ambiguidade` são rotas de admin+2FA e apenas DEVOLVEM a
    segunda opinião (marca/não marca ou mesmo produto/diferentes) — nenhuma decisão da IA
    altera a base. Sem `DEEPSEEK_API_KEY` a resposta é 503 com a instrução; a chamada HTTP
    é substituída nos testes (nenhuma cota é gasta). A tela mostra o botão de consulta
    somente nos grupos da zona cinzenta (similaridade entre 50% e 90%);
16. raio-x da base em Excel: `GET /api/relatorios/exportar-grupos-completos` varre TODAS
    as notas (não só a janela dos relatórios), usa a mesma chave inteligente da Auditoria
    e devolve um .xlsx com Produto Principal/EAN/NCM/Lista de Descrições para os grupos de
    2+ descrições únicas. EAN e NCM são texto (zeros à esquerda preservados), os totais
    vão nos cabeçalhos `X-Total-Grupos`/`X-Total-Descricoes` e a aba 📥 Exportar Base do
    front guarda os bytes em memória para o `st.download_button`;
17. regra PRODUTO + MARCA: a chave inteligente inclui a marca inferida
    (`itens_nota.marca`). "POLENTA PALITO ARRICO" e "POLENTA PALITO EL CHEF" são grupos
    distintos mesmo com o mesmo EAN; marca nula/"NÃO IDENTIFICADA" agrupa pelo nome base e
    números de controle ("- 1", "- 2") não criam grupos NEM duplicam sugestões da IA (as
    variações equivalentes viram um único card e o Aplicar move todas juntas). A IA
    sugere separação por marca, produto/volume (marca é parte da identidade; ex.: Queijo
    Minas Camanducaia x Ipanema são produtos diferentes). O catálogo MASTER em
    `GET /api/relatorios/exportar-base-completa` traz TODAS as descrições, inclusive as de
    grupos com uma única descrição, nas colunas Produto (Marca)/Marca/EAN/NCM/Descrição
    Original.
18. embalagem é IDENTIDADE do produto (CX24 x UN): `normalizar_embalagem` gera o token
    canônico usado na chave e os relatórios NÃO convertem 1 CX = 24 UN para média/variação;
    a Auditoria de Divergências mostra a última compra de cada descrição (vermelha quando
    a pendência é variação brusca ≥ 50%) e mantém Validar/Mover/Ver DANFE.

Para os testes de admin existe `conta_admin_teste()`: cria uma conta de teste, liga a
flag `is_admin` direto no banco e ativa o 2FA (`_ativar_2fa`), registrando o token do
segundo fator para os helpers de HTTP; tudo é apagado no fim.

Como rodar (backend em http://127.0.0.1:8000):
    python tests/test_integridade_relatorios.py

Os testes que precisam gravar dados usam uma CÓPIA do banco (arquivo temporário), então a
base real não é alterada — exceto os testes dos endpoints de pendência, que criam e apagam
chaves de teste marcadas com "TESTE-INTEGRIDADE".
"""

import html
import json
import shutil
import sys
import tempfile
import traceback
import uuid
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import pandas as pd
import requests
from openpyxl import load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import certificados as bcerts
from backend import ia_consenso
from backend import main as bmain
from backend import aprendizado_produtos as baprendizado
from backend.database import DB_PATH, SessionLocal, init_db
from backend.models import (
    Base,
    CodigoEmail,
    ConfiguracaoAlerta,
    ConfiguracaoSistema,
    Empresa,
    Fornecedor,
    ItemAuditado,
    ItemNota,
    MapeamentoFornecedorProduto,
    NotaFiscal,
    RegraAgrupamento,
    SugestaoCorrecaoIA,
    Usuario,
)
import import_xmls
from import_xmls import item_bonificado

BASE_API = "http://127.0.0.1:8000"
NS = {"nfe": "http://www.portalfiscal.inf.br/nfe"}

TESTS = []


def teste(funcao):
    """Registra a função como teste (funciona como `pytest` e como script direto)."""
    TESTS.append(funcao)
    return funcao


# ──────────────────────────────────────────────────────────────────────────────
# Apoio
# ──────────────────────────────────────────────────────────────────────────────
def _sessao_copia(diretorio):
    """Sessão SQLAlchemy sobre uma CÓPIA do banco (nenhum teste escreve na base real)."""
    copia = Path(diretorio) / "copia_integridade.db"
    shutil.copy(DB_PATH, copia)
    engine = create_engine("sqlite:///" + str(copia).replace("\\", "/"))
    # Aplica na CÓPIA as migrações aditivas do start da API: assim o teste funciona
    # mesmo quando a base real ainda não recebeu uma coluna nova (ex.: as credenciais
    # Omie de empresas).
    init_db(engine)
    return sessionmaker(bind=engine)(), engine


def _det(xml_item):
    """(prod, det) de um trecho de XML de item da NFe (com ou sem namespace)."""
    det = ET.fromstring(xml_item)
    prod = det.find("nfe:prod", NS)
    if prod is None:  # XML de teste sem namespace: o mapa transformaria em {ns}prod
        prod = det.find("prod")
    return prod, det


def _inserir_nota_teste(db, itens, data_nota=None, ncm="00000000"):
    """Nota de teste com os itens informados: [(ean, descricao, valor[, cfop])].

    O CFOP é opcional (None = desconhecido, como ficaram os itens importados antes da
    coluna existir). O NCM vale para todos os itens da nota e é o que agrupa as marcas
    substitutas no relatório. Devolve a nota criada.
    """
    empresa = db.query(Empresa).first()
    fornecedor = db.query(Fornecedor).first()
    nota = NotaFiscal(
        chave_acesso=f"TESTE-INTEGRIDADE-{uuid.uuid4().hex[:12]}",
        numero_nota="1",
        serie="1",
        data_emissao=data_nota or date(2026, 1, 15),
        valor_total=sum(item[2] for item in itens),
        empresa_id=empresa.id if empresa else None,
    )
    db.add(nota)
    db.commit()
    db.refresh(nota)
    for item in itens:
        ean, descricao, valor = item[0], item[1], item[2]
        cfop = item[3] if len(item) > 3 else None
        db.add(
            ItemNota(
                nota_id=nota.id,
                fornecedor_id=fornecedor.id if fornecedor else None,
                ean=ean,
                descricao_produto=descricao,
                quantidade=1.0,
                valor_unitario=valor,
                valor_total=valor,
                ncm=ncm,
                cfop=cfop,
            )
        )
    db.commit()
    return nota


def _carregar(db):
    estatisticas = {}
    itens = bmain._carregar_itens_relatorio(db, estatisticas=estatisticas)
    return itens, estatisticas


def _api(caminho, params=None, token=None):
    """GET na API. Rotas de auditoria precisam do token da sessão e do 2FA."""
    resposta = requests.get(
        BASE_API + caminho, params=params, headers=_cabecalho_admin(token), timeout=180
    )
    if resposta.status_code != 200:
        raise AssertionError(f"{caminho} devolveu {resposta.status_code}: {resposta.text[:300]}")
    return resposta.json()


# ──────────────────────────────────────────────────────────────────────────────
# Regra 3: bonificação / doação / brinde na leitura do XML
# ──────────────────────────────────────────────────────────────────────────────
@teste
def test_item_bonificado_no_xml():
    """XML: valor R$ 0,00, CFOP 5910, desconto total e texto de bonificação são ignorados."""
    casos_bonificados = [
        # valor unitário R$ 0,00 (brinde)
        """<det><prod><xProd>REFRIGERANTE 2L</xProd><CFOP>5102</CFOP>
            <vUnCom>0.00</vUnCom><vProd>0.00</vProd></prod><infAdProd/></det>""",
        # CFOP de remessa em bonificação/doação
        """<det><prod><xProd>AGUA MINERAL</xProd><CFOP>5910</CFOP>
            <vUnCom>1.50</vUnCom><vProd>15.00</vProd></prod><infAdProd/></det>""",
        # desconto igual ao valor do produto (100% de desconto)
        """<det><prod><xProd>SUCO</xProd><CFOP>5102</CFOP><vUnCom>5.00</vUnCom>
            <vProd>50.00</vProd><vDesc>50.00</vDesc></prod><infAdProd/></det>""",
        # texto citando bonificação
        """<det><prod><xProd>PAO DE FORMA</xProd><CFOP>5102</CFOP><vUnCom>8.00</vUnCom>
            <vProd>8.00</vProd></prod><infAdProd>ITEM BONIFICACAO</infAdProd></det>""",
    ]
    for xml in casos_bonificados:
        prod, det = _det(xml)
        assert item_bonificado(prod, det, NS), f"deveria ser bonificado: {xml[:60]}"

    xml_normal = """<det><prod><xProd>ARROZ 5KG</xProd><CFOP>5102</CFOP>
        <vUnCom>22.90</vUnCom><vProd>22.90</vProd></prod><infAdProd/></det>"""
    prod, det = _det(xml_normal)
    assert not item_bonificado(prod, det, NS), "item com preço normal não é bonificação"


# ──────────────────────────────────────────────────────────────────────────────
# Regras 1 e 3 dentro do carregamento dos relatórios
# ──────────────────────────────────────────────────────────────────────────────
@teste
def test_relatorio_ignora_bonificado_e_pendente():
    """Item zerado não entra na média e descrição pendente na auditoria sai do cálculo."""
    with tempfile.TemporaryDirectory() as diretorio:
        db, engine = _sessao_copia(diretorio)
        try:
            _inserir_nota_teste(
                db,
                [
                    ("7891234567890", "PRODUTO TESTE BONIFICADO", 0.0),
                    ("7891234567891", "PRODUTO TESTE NORMAL", 10.0),
                ],
            )
            itens, estatisticas = _carregar(db)
            descricoes = {item["descricao"] for item in itens}

            assert estatisticas["bonificados"] >= 1, "o item R$ 0,00 deveria ser contado"
            assert "PRODUTO TESTE BONIFICADO" not in descricoes, "item zerado entrou no relatório"
            assert "PRODUTO TESTE NORMAL" in descricoes, "item com preço válido ficou de fora"
            assert all(item["valor"] > 0 for item in itens), "média recebeu item sem preço"

            # Marca a descrição do item normal como PENDENTE (fluxo do "enviar para auditoria").
            chave_descricao = next(
                item["chave_descricao"]
                for item in itens
                if item["descricao"] == "PRODUTO TESTE NORMAL"
            )
            db.add(ItemAuditado(chave_identificacao=chave_descricao, auditado=False))
            db.commit()

            itens_pendente, estatisticas_pendente = _carregar(db)
            assert estatisticas_pendente["pendentes_auditoria"] >= 1, "pendência não foi contada"
            assert not [
                item for item in itens_pendente if item["descricao"] == "PRODUTO TESTE NORMAL"
            ], "descrição pendente na auditoria continuou no relatório"

            # Aprovar a mesma chave devolve o item aos relatórios.
            registro = (
                db.query(ItemAuditado)
                .filter(ItemAuditado.chave_identificacao == chave_descricao)
                .first()
            )
            registro.auditado = True
            db.commit()
            itens_aprovado, _ = _carregar(db)
            assert [
                item for item in itens_aprovado if item["descricao"] == "PRODUTO TESTE NORMAL"
            ], "aprovar a chave deveria devolver o item ao relatório"
        finally:
            db.close()
            engine.dispose()


@teste
def test_pendencia_da_aba_auditoria_sai_do_relatorio():
    """Descrição de grupo de exceção (o que a Auditoria mostra como pendente) não entra."""
    with tempfile.TemporaryDirectory() as diretorio:
        db, engine = _sessao_copia(diretorio)
        try:
            # Mesmo EAN e MESMO volume com descrições muito diferentes => grupo de
            # exceção na Auditoria (a regra de volume separaria 5 KG de 1 KG antes).
            _inserir_nota_teste(
                db,
                [
                    ("7891234567892", "ARROZ TIPO 1 5 KG", 20.0),
                    ("7891234567892", "FEIJAO CARIOCA TIPO 1 5 KG", 8.0),
                ],
            )
            itens, estatisticas = _carregar(db)
            descricoes = {item["descricao"] for item in itens}
            assert estatisticas["pendentes_auditoria"] >= 2, (
                "as duas descrições divergentes deveriam ficar pendentes"
            )
            assert "FEIJAO CARIOCA TIPO 1 5 KG" not in descricoes
            assert "ARROZ TIPO 1 5 KG" not in descricoes
        finally:
            db.close()
            engine.dispose()



# ──────────────────────────────────────────────────────────────────────────────
# Regra 2: aberrações (variação >= 50%) fora dos totais e no fim da lista
# ──────────────────────────────────────────────────────────────────────────────
def _linha_impacto(produto, preco_atual, preco_anterior, consumo=1.0):
    impacto = round((preco_atual - preco_anterior) * consumo, 2)
    return {
        "produto": produto,
        "preco_atual": preco_atual,
        "preco_anterior": preco_anterior,
        "variacao_pct": round((preco_atual / preco_anterior - 1) * 100, 2),
        "consumo_medio": consumo,
        "impacto_mensal": impacto,
        "aumento": round(max(impacto, 0.0), 2),
        "economia": round(max(-impacto, 0.0), 2),
    }


@teste
def test_limiar_de_aberrcao_e_de_50_por_cento():
    """O limite da variação brusca passou a ser 50% e o alerta mudou de texto."""
    assert bmain.LIMIAR_ABERRACAO_PCT == 50.0, "o limite deveria ser 50%"
    assert bmain.ALERTA_ABERRACAO == "Verificar Variação Brusca", bmain.ALERTA_ABERRACAO


@teste
def test_aberracoes_ficam_no_fim_e_fora_dos_totais():
    """Variação >= 50% vira aberração: alerta, sem aumento/economia e no fim da lista."""
    linhas = [
        _linha_impacto("ABERRACAO +50%", 15.0, 10.0),  # exatamente no limite
        _linha_impacto("ABERRACAO -50%", 5.0, 10.0),  # queda de 50%
        _linha_impacto("ABERRACAO +100%", 20.0, 10.0),
        _linha_impacto("NORMAL +49%", 14.9, 10.0),
        _linha_impacto("NORMAL +5%", 10.5, 10.0),
        _linha_impacto("NORMAL -10%", 9.0, 10.0),
    ]
    exibicao, normais, aberracoes = bmain._classificar_aberracoes(linhas, limite=500)

    assert len(aberracoes) == 3, "as variações de 50% ou mais deveriam ser aberrações"
    assert all(linha["aberrante"] for linha in aberracoes)
    assert all(linha["alerta"] == "Verificar Variação Brusca" for linha in aberracoes)
    assert all(linha["aumento"] == 0.0 and linha["economia"] == 0.0 for linha in aberracoes)
    assert not any(linha["aberrante"] for linha in normais)
    assert len(normais) == 3, "as variações abaixo de 50% continuam normais"
    # As aberrações ocupam as últimas posições da tabela.
    assert [linha["produto"] for linha in exibicao[-3:]] == [
        linha["produto"] for linha in aberracoes
    ]
    # Totais (só produtos normais): 4.90 + 0.50 de aumento e 1.00 de economia.
    assert round(sum(linha["aumento"] for linha in normais), 2) == 5.4
    assert round(sum(linha["economia"] for linha in normais), 2) == 1.0


@teste
def test_aberração_validada_volta_a_contar():
    """Variação validada na Auditoria sai do alerta e volta para os totais."""
    linhas = [
        _linha_impacto("PENDENTE", 20.0, 10.0),
        _linha_impacto("VALIDADA", 20.0, 10.0),
    ]
    for linha in linhas:
        linha["chave_descricao"] = f"DESC_{linha['produto']}"

    _, normais, aberracoes = bmain._classificar_aberracoes(
        linhas, limite=500, validadas={"DESC_VALIDADA"}
    )
    assert [linha["produto"] for linha in aberracoes] == ["PENDENTE"]
    assert [linha["produto"] for linha in normais] == ["VALIDADA"]
    assert normais[0]["validada"] is True, "faltou o selo de variação validada"
    assert normais[0]["aumento"] > 0, "a variação validada deve voltar a contar"
    assert aberracoes[0]["aberrante"] is True and aberracoes[0]["aumento"] == 0.0


@teste
def test_cfop_de_bonificacao_sai_do_relatorio():
    """Item com CFOP 5910/6910 (bonificação) não entra na média de preço."""
    with tempfile.TemporaryDirectory() as diretorio:
        db, engine = _sessao_copia(diretorio)
        try:
            _inserir_nota_teste(
                db,
                [
                    ("7891234567801", "PRODUTO TESTE CFOP BONIFICADO", 7.45, "5910"),
                    ("7891234567802", "PRODUTO TESTE CFOP BONIFICADO ST", 9.9, "6910"),
                    ("7891234567803", "PRODUTO TESTE CFOP COMPRA NORMAL", 280.94, "5403"),
                    ("7891234567804", "PRODUTO TESTE CFOP DESCONHECIDO", 12.0, None),
                ],
            )
            itens, estatisticas = _carregar(db)
            descricoes = {item["descricao"] for item in itens}

            assert estatisticas["bonificados_cfop"] >= 2, (
                "os itens com CFOP 5910/6910 deveriam ser contados"
            )
            assert "PRODUTO TESTE CFOP BONIFICADO" not in descricoes, "CFOP 5910 entrou no relatório"
            assert "PRODUTO TESTE CFOP BONIFICADO ST" not in descricoes, "CFOP 6910 entrou no relatório"
            assert "PRODUTO TESTE CFOP COMPRA NORMAL" in descricoes
            # CFOP desconhecido (NULL, itens antigos) continua entrando: não há prova de bonificação.
            assert "PRODUTO TESTE CFOP DESCONHECIDO" in descricoes
        finally:
            db.close()
            engine.dispose()


@teste
def test_aberrante_fica_visivel_e_manual_sai_do_relatorio():
    """Pendência automática (aberrante) não esconde o item; a manual esconde."""
    with tempfile.TemporaryDirectory() as diretorio:
        db, engine = _sessao_copia(diretorio)
        try:
            _inserir_nota_teste(
                db, [("7891234567810", "PRODUTO TESTE VARIACAO BRUSCA", 40.0, "5102")]
            )
            chave = bmain._chave_produto_relatorio("7891234567810", "PRODUTO TESTE VARIACAO BRUSCA")
            chave_descricao = bmain.regras_auditoria.chave_descricao(
                chave, "PRODUTO TESTE VARIACAO BRUSCA"
            )
            linhas = [
                {
                    "chave": chave,
                    "chave_descricao": chave_descricao,
                    "aberrante": True,
                    "impacto_mensal": 100.0,
                }
            ]
            assert bmain._registrar_aberracoes_auditoria(db, linhas) == 1, "1ª marcação deveria gravar"
            assert bmain._registrar_aberracoes_auditoria(db, linhas) == 0, "marcação duplicada"

            registro = (
                db.query(ItemAuditado)
                .filter(ItemAuditado.chave_identificacao == chave_descricao)
                .first()
            )
            assert registro is not None and registro.motivo == "aberrante", registro

            itens, estatisticas = _carregar(db)
            assert "PRODUTO TESTE VARIACAO BRUSCA" in {item["descricao"] for item in itens}, (
                "aberração pendente deveria continuar visível na tabela (fora dos totais)"
            )
            assert estatisticas["aberracoes_auditoria"] >= 1

            # A mesma chave marcada manualmente passa a sair dos relatórios.
            registro.motivo = "manual"
            db.commit()
            itens, _ = _carregar(db)
            assert "PRODUTO TESTE VARIACAO BRUSCA" not in {item["descricao"] for item in itens}

            # Validada (auditado=True com motivo "aberrante"): entra no mapa de validadas,
            # então a variação deixa de ser aberração e volta a contar nos totais.
            registro.auditado = True
            registro.motivo = "aberrante"
            db.commit()
            _, manuais, pendentes_aberracoes, validadas = bmain._mapa_auditoria_relatorio(db)
            assert chave_descricao not in manuais
            assert chave_descricao not in pendentes_aberracoes
            assert chave_descricao in validadas, "a chave validada não foi reconhecida"
        finally:
            db.close()
            engine.dispose()


# ──────────────────────────────────────────────────────────────────────────────
# API: relatórios sem itens zerados/pendentes
# ──────────────────────────────────────────────────────────────────────────────
@teste
def test_importador_vincula_nota_ao_destinatario():
    """Multilojas: a nota vai para a loja do CNPJ destinatário (e não sempre a 1ª)."""
    with tempfile.TemporaryDirectory() as diretorio:
        db, engine = _sessao_copia(diretorio)
        try:
            padrao = db.query(Empresa).first()
            # Destinatário já cadastrado: a nota é vinculada àquela loja.
            xml_existente = (
                "<infNFe><dest><CNPJ>50951160000194</CNPJ>"
                "<xNome>MOCA BAR E LANCHONETE LTDA</xNome></dest></infNFe>"
            )
            empresa = import_xmls._empresa_por_destinatario(
                db, ET.fromstring(xml_existente), NS, padrao
            )
            assert empresa.cnpj == "50951160000194", empresa.cnpj
            assert empresa.id != padrao.id, "a nota não pode ficar na empresa padrão"

            # Destinatário novo: cadastra a loja com a razão social do XML.
            xml_novo = (
                "<infNFe><dest><CNPJ>99.999.999/0001-99</CNPJ>"
                "<xNome>LOJA NOVA TESTE</xNome></dest></infNFe>"
            )
            nova = import_xmls._empresa_por_destinatario(db, ET.fromstring(xml_novo), NS, padrao)
            assert nova.cnpj == "99999999000199", nova.cnpj
            assert nova.nome_loja == "LOJA NOVA TESTE", nova.nome_loja
            assert db.query(Empresa).filter(Empresa.cnpj == "99999999000199").first() is not None

            # Sem destinatário legível a nota continua na empresa padrão (nada se perde).
            sem_dest = ET.fromstring("<infNFe><ide/></infNFe>")
            assert (
                import_xmls._empresa_por_destinatario(db, sem_dest, NS, padrao).id == padrao.id
            )
        finally:
            db.close()
            engine.dispose()


@teste
def test_api_multilojas_puxa_as_lojas_do_grupo():
    """O comparativo devolve as lojas do grupo com as compras de cada uma."""
    comparativo = _api(
        "/api/relatorio/comparativo",
        {"dias": 360, "periodicidade": "mensal", "limite": 20000},
    )
    assert len(comparativo["lojas"]) >= 2, (
        f"esperava mais de uma loja no comparativo: {comparativo['lojas']}"
    )
    lojas_com_dados = comparativo["resumo"]["lojas_com_dados"]
    assert len(lojas_com_dados) >= 2, f"esperava compras em 2+ lojas: {lojas_com_dados}"
    # Cada linha traz o preço por loja (None quando a loja não comprou no período).
    for chave in lojas_com_dados:
        assert any(
            linha["precos"].get(chave) is not None for linha in comparativo["linhas"]
        ), f"a loja {chave} não trouxe preço em nenhuma linha"


@teste
def test_api_relatorios_sem_zerados_nem_pendentes():
    """Os três endpoints devolvem dados limpos e informam o que ficou fora do cálculo."""
    impacto = _api("/api/relatorio/impacto", {"meses_consumo": 3})
    assert impacto["linhas"], "impacto financeiro veio vazio"
    assert all(linha["preco_atual"] > 0 for linha in impacto["linhas"]), "preço atual zerado"
    assert all(linha["preco_anterior"] > 0 for linha in impacto["linhas"]), "preço anterior zerado"
    assert impacto["integridade"]["pendentes_auditoria"] >= 0

    # Aberrações (se houver) sempre no fim e nunca somadas aos totais.
    posicoes = [i for i, linha in enumerate(impacto["linhas"]) if linha.get("aberrante")]
    if posicoes:
        assert posicoes == list(range(posicoes[0], len(impacto["linhas"]))), (
            "aberrações deveriam ocupar o fim da lista"
        )
    normais = [linha for linha in impacto["linhas"] if not linha.get("aberrante")]
    assert abs(sum(linha["aumento"] for linha in normais) - impacto["totais"]["aumento"]) < 0.05
    assert abs(sum(linha["economia"] for linha in normais) - impacto["totais"]["economia"]) < 0.05
    assert impacto["totais"]["produtos_comparados"] == len(normais)

    evolucao = _api("/api/relatorio/evolucao", {"dias": 90, "periodicidade": "mensal"})
    assert evolucao["periodos"], "evolução veio vazia"
    compras = [compra for periodo in evolucao["periodos"] for compra in periodo["compras"]]
    assert all(compra["valor"] > 0 for compra in compras), "evolução trouxe compra zerada"
    assert "integridade" in evolucao

    comparativo = _api(
        "/api/relatorio/comparativo",
        {"dias": 90, "periodicidade": "mensal", "limite": 20000},
    )
    assert comparativo["linhas"], "comparativo veio vazio"
    compras = [
        compra
        for linha in comparativo["linhas"]
        for compras_loja in (linha.get("compras") or {}).values()
        for compra in compras_loja
    ]
    assert all(compra["valor"] > 0 for compra in compras), "comparativo trouxe compra zerada"
    assert "integridade" in comparativo

    # Produto pendente na auditoria não pode aparecer nos relatórios.
    with conta_admin_teste() as (_admin, token_admin):
        aprovadas = set(_api("/api/auditados", token=token_admin))
    db = SessionLocal()
    try:
        mapa_regras, mapa_descricoes = bmain._mapas_regras_relatorio(db)
        pendentes = bmain._descricoes_pendentes_auditoria(db, mapa_regras, mapa_descricoes, aprovadas)
    finally:
        db.close()
    descricoes_pendentes = {chave.rsplit("_", 1)[-1] for chave in pendentes}
    produtos_impacto = {linha["produto"] for linha in impacto["linhas"]}
    assert not (descricoes_pendentes & produtos_impacto), (
        "produto pendente na auditoria apareceu no impacto: "
        f"{sorted(descricoes_pendentes & produtos_impacto)[:5]}"
    )
    assert pendentes, "a base deveria ter descrições pendentes para o filtro ser testado"



# ──────────────────────────────────────────────────────────────────────────────
# "Enviar para a auditoria": marca pendente, aprova e limpa a chave de teste
# ──────────────────────────────────────────────────────────────────────────────
@teste
def test_endpoints_de_pendencia_da_auditoria():
    """POST /api/auditar/pendente marca, GET lista, POST /api/auditar aprova e DELETE limpa.

    Todas essas rotas são de administrador: aqui usamos o token de uma conta `is_admin`.
    """
    chave = f"TESTE-INTEGRIDADE-{uuid.uuid4().hex[:10]}"
    with conta_admin_teste() as (_admin, token_admin):
        cabecalho = _cabecalho_admin(token_admin)
        try:
            resposta = requests.post(
                BASE_API + "/api/auditar/pendente",
                json={"chaves": [chave]},
                headers=cabecalho,
                timeout=30,
            )
            assert resposta.status_code == 200, resposta.text
            assert chave in _api("/api/auditados/pendentes", token=token_admin), (
                "chave marcada não foi listada"
            )
            assert chave not in _api("/api/auditados", token=token_admin), (
                "chave pendente apareceu como aprovada"
            )

            # Envio com motivo "aberrante": listado como variação brusca (não sai da tabela).
            resposta = requests.post(
                BASE_API + "/api/auditar/pendente",
                json={"chaves": [chave], "motivo": "aberrante"},
                headers=cabecalho,
                timeout=30,
            )
            assert resposta.status_code == 200, resposta.text
            assert resposta.json()["motivo"] == "aberrante", resposta.text
            assert chave in _api("/api/auditados/pendentes?motivo=aberrante", token=token_admin)
            assert chave not in _api("/api/auditados/pendentes?motivo=manual", token=token_admin)

            # Motivo desconhecido é rejeitado (protege o efeito nos relatórios).
            resposta = requests.post(
                BASE_API + "/api/auditar/pendente",
                json={"chaves": [chave], "motivo": "qualquer"},
                headers=cabecalho,
                timeout=30,
            )
            assert resposta.status_code == 400, resposta.text

            resposta = requests.post(
                BASE_API + "/api/auditar",
                json={"chave_identificacao": chave},
                headers=cabecalho,
                timeout=30,
            )
            assert resposta.status_code == 200, resposta.text
            assert chave not in _api("/api/auditados/pendentes", token=token_admin), (
                "aprovar não removeu a pendência"
            )
            assert chave in _api("/api/auditados", token=token_admin), (
                "chave aprovada não foi listada"
            )
        finally:
            requests.delete(
                f"{BASE_API}/api/auditar/{chave}", headers=cabecalho, timeout=30
            )


@teste
def test_aprovar_descricao_limpa_chaves_antigas_de_aberrracao():
    """Aprovar a chave atual também valida a chave antiga da mesma descrição.

    Sem isso, uma variação brusca gravada antes da regra de marca/volume continuaria
    no aviso amarelo mesmo depois do Aprovar (a chave atual não é a mesma do registro
    antigo, mas a descrição é).
    """
    from datetime import date as _date

    db, engine = _banco_aprendizado_temporario()
    try:
        empresa = Empresa(cnpj="12345678000199", nome_loja="Loja Aprovar")
        fornecedor = Fornecedor(cnpj="98765432000199", nome="Fornecedor Aprovar")
        db.add_all([empresa, fornecedor])
        db.commit()
        nota = NotaFiscal(
            chave_acesso="TESTE-APROVAR-LEGADO",
            data_emissao=_date(2026, 1, 10),
            empresa_id=empresa.id,
        )
        db.add(nota)
        db.commit()
        descricao = "ORIGINAL 600ML - 1"
        db.add(
            ItemNota(
                nota_id=nota.id,
                fornecedor_id=fornecedor.id,
                ean="78905351",
                descricao_produto=descricao,
                quantidade=1.0,
                valor_unitario=10.0,
                valor_total=10.0,
                ncm="22030000",
            )
        )
        normalizada = bmain._limpar_texto_relatorio(descricao)
        chave_atual = f"DESC_EAN_78905351_{normalizada}"
        chave_antiga = f"DESC_EAN_78905351__{normalizada}"
        chave_outra = "DESC_EAN_78905351_OUTRO PRODUTO"
        db.add_all(
            [
                ItemAuditado(
                    chave_identificacao=chave_atual,
                    auditado=False,
                    motivo="aberrante",
                ),
                ItemAuditado(
                    chave_identificacao=chave_antiga,
                    auditado=False,
                    motivo="aberrante",
                ),
                ItemAuditado(
                    chave_identificacao=chave_outra,
                    auditado=False,
                    motivo="aberrante",
                ),
            ]
        )
        db.commit()

        bmain.marcar_auditado(
            data=bmain.AuditRequest(chave_identificacao=chave_atual),
            admin=None,
            db=db,
        )
        estados = {
            registro.chave_identificacao: registro.auditado
            for registro in db.query(ItemAuditado).all()
        }
        assert estados[chave_atual] is True, estados
        assert estados[chave_antiga] is True, estados
        assert estados[chave_outra] is False, estados
    finally:
        db.close()
        engine.dispose()


@teste
def test_e2e_pendencia_remove_produto_do_impacto():
    """Marcar a descrição como pendente tira o produto do relatório; apagar devolve."""
    impacto = _api("/api/relatorio/impacto", {"meses_consumo": 3})
    with conta_admin_teste() as (_admin, token_admin):
        aprovadas = set(_api("/api/auditados", token=token_admin))
        db = SessionLocal()
        try:
            mapa_regras, mapa_descricoes = bmain._mapas_regras_relatorio(db)
            pendentes_excecao = bmain._descricoes_pendentes_auditoria(
                db, mapa_regras, mapa_descricoes, aprovadas
            )
        finally:
            db.close()

        candidato = None
        for linha in impacto["linhas"]:
            chave = linha["chave_descricao"]
            if (
                not linha.get("aberrante")
                and chave not in aprovadas
                and chave not in pendentes_excecao
            ):
                candidato = chave
                break
        assert candidato, "nenhum produto disponível para o teste de pendência"

        cabecalho = _cabecalho_admin(token_admin)
        try:
            resposta = requests.post(
                BASE_API + "/api/auditar/pendente",
                json={"chaves": [candidato]},
                headers=cabecalho,
                timeout=30,
            )
            assert resposta.status_code == 200, resposta.text
            depois = _api("/api/relatorio/impacto", {"meses_consumo": 3})
            assert candidato not in {linha["chave_descricao"] for linha in depois["linhas"]}, (
                "produto pendente continuou no relatório"
            )
            assert (
                depois["integridade"]["pendentes_auditoria"]
                > impacto["integridade"]["pendentes_auditoria"]
            ), "o item pendente não foi contabilizado como ignorado"
        finally:
            requests.delete(
                f"{BASE_API}/api/auditar/{candidato}", headers=cabecalho, timeout=30
            )

        final = _api("/api/relatorio/impacto", {"meses_consumo": 3})
        assert candidato in {linha["chave_descricao"] for linha in final["linhas"]}, (
            "remover a pendência deveria devolver o produto ao relatório"
        )


# ──────────────────────────────────────────────────────────────────────────────
# Etapa de autenticação + multilojas: rotas e telas
# ──────────────────────────────────────────────────────────────────────────────
CNPJ_TESTE = "99999999000191"  # CNPJ fictício: só existe durante estes testes
CNPJ_CERTIFICADO_TESTE = "99999999000191"  # CNPJ que aparece dentro do .pfx de teste
# Loja do motor de alertas: criada com duas notas (preço antigo x atual) e apagada no fim.
CNPJ_ALERTA = "99999999000272"
CNPJ_FORNECEDOR_ALERTA = "99999999000353"
PRODUTOS_ALERTA = ("PRODUTO ALERTA A 1KG", "PRODUTO ALERTA B 1KG")
SENHA_TESTE = "Teste@12345"
SENHA_CERTIFICADO_TESTE = "Cert@12345"
# Chaves de st.session_state equivalentes a SESSAO_USUARIO/SESSAO_TOKEN do app.py.
CHAVE_USUARIO_SESSAO = "usuario_logado"
CHAVE_TOKEN_SESSAO = "token_api"
# Token do segundo fator guardado pelo front depois de /api/auth/2fa/verify.
CHAVE_2FA_SESSAO = "token_2fa"
CHAVE_2FA_EXPIRA_SESSAO = "token_2fa_expira"
# Desafio do login esperando o código do autenticador (SESSAO_LOGIN_2FA do app.py).
CHAVE_LOGIN_2FA_SESSAO = "_login_2fa_pendente"
# Certificados .pfx gerados nos testes (a geração RSA é cara: reaproveita por CNPJ).
_CERTIFICADOS_GERADOS = {}


def _post_api(caminho, corpo, token=None):
    """POST na API; com `token`, os headers de sessão (e de 2FA, se houver) vão junto."""
    return requests.post(
        BASE_API + caminho, json=corpo, headers=_cabecalho_admin(token), timeout=120
    )


def _novo_email_teste():
    return f"teste.integridade.{uuid.uuid4().hex[:10]}@exemplo.com"


def _registrar_usuario_teste(nome="Teste Integridade"):
    """Cria uma conta de teste. Devolve (usuario, token, email, senha).

    O cadastro NÃO abre sessão (o e-mail precisa ser confirmado com o código de 6
    dígitos). O teste liga o flag direto no banco — como `_definir_admin` faz — e entra
    pela rota de login, que é o caminho real depois da confirmação.
    """
    email = _novo_email_teste()
    resposta = _post_api(
        "/api/auth/registrar", {"nome": nome, "email": email, "senha": SENHA_TESTE}
    )
    assert resposta.status_code == 200, (
        f"registro falhou: {resposta.status_code} {resposta.text[:300]}"
    )
    pendente = resposta.json()
    assert pendente.get("verificacao_pendente") is True, pendente
    assert not pendente.get("token"), "o cadastro não pode abrir sessão antes do código"
    assert pendente["email"] == email

    _confirmar_email_usuario_teste(email)
    login = _post_api("/api/auth/login", {"email": email, "senha": SENHA_TESTE})
    assert login.status_code == 200, (
        f"login falhou: {login.status_code} {login.text[:300]}"
    )
    corpo = login.json()
    assert corpo.get("token"), "o login precisa devolver o token da sessão"
    assert corpo["usuario"]["email"] == email
    return corpo["usuario"], corpo["token"], email, SENHA_TESTE


def _confirmar_email_usuario_teste(email):
    """Marca o e-mail da conta de teste como confirmado (sem depender do SMTP).

    O código de confirmação sai por e-mail de verdade; o teste não abre SMTP, então o
    flag é ligado no banco antes do login — exatamente o estado que
    /api/auth/validar-email deixaria depois do código correto.
    """
    db = SessionLocal()
    try:
        db.query(Usuario).filter(Usuario.email == email).update(
            {"email_verificado": True}
        )
        db.commit()
    finally:
        db.close()


def _apagar_usuario_teste(usuario_id):
    """Remove a conta criada pelo teste (códigos, preferências de alerta e lojas)."""
    db = SessionLocal()
    try:
        db.query(CodigoEmail).filter(CodigoEmail.usuario_id == usuario_id).delete()
        db.query(ConfiguracaoAlerta).filter(
            ConfiguracaoAlerta.usuario_id == usuario_id
        ).delete()
        db.query(Usuario).filter(Usuario.id == usuario_id).delete()
        db.commit()
    finally:
        db.close()


def _definir_admin(usuario_id, is_admin=True):
    """Liga/desliga a flag de administrador direto no banco.

    Não existe rota para se promover a admin (é o ponto da autorização): a promoção é um
    comando local (`python criar_admin.py <e-mail>`), então o teste faz o mesmo.
    """
    db = SessionLocal()
    try:
        db.query(Usuario).filter(Usuario.id == usuario_id).update(
            {"is_admin": bool(is_admin)}
        )
        db.commit()
    finally:
        db.close()


# Tokens do segundo fator das contas de teste: {token_sessao: token_2fa}. As rotas de
# auditoria exigem `X-2FA-Token` além do `Authorization`; o dicionário deixa os helpers
# (_api, _post_api, _cabecalho_admin) montarem os dois cabeçalhos sem mudar a assinatura
# dos testes que já existiam.
_TOKENS_2FA = {}


def _cabecalho_admin(token=None):
    """Cabeçalhos de uma conta: Authorization + X-2FA-Token (quando registrado)."""
    cabecalho = {"Authorization": f"Bearer {token}"} if token else {}
    token_2fa = _TOKENS_2FA.get(token)
    if token_2fa:
        cabecalho["X-2FA-Token"] = token_2fa
    return cabecalho


def _ativar_2fa(usuario, token):
    """Configura e confirma o 2FA da conta de teste e registra o token do segundo fator.

    Devolve (segredo, token_2fa, expira_em). É o mesmo caminho da tela: /2fa/setup gera o
    segredo (e o QR Code) e /2fa/verify confirma o código de 6 dígitos do `pyotp`.
    """
    import pyotp

    cabecalho = _cabecalho_admin(token)
    setup = requests.post(
        BASE_API + "/api/auth/2fa/setup",
        json={"reiniciar": True},
        headers=cabecalho,
        timeout=60,
    )
    assert setup.status_code == 200, setup.text[:300]
    segredo = setup.json().get("segredo")
    assert segredo, setup.json()

    verificacao = requests.post(
        BASE_API + "/api/auth/2fa/verify",
        json={"codigo": pyotp.TOTP(segredo).now()},
        headers=cabecalho,
        timeout=60,
    )
    assert verificacao.status_code == 200, verificacao.text[:300]
    corpo = verificacao.json()
    assert corpo.get("token_2fa"), corpo
    _TOKENS_2FA[token] = corpo["token_2fa"]
    return segredo, corpo["token_2fa"], corpo.get("expira_em")


@contextmanager
def conta_admin_teste(nome="Admin Integridade", com_2fa=True):
    """Conta de teste com `is_admin=True` (e 2FA ativo). Devolve (usuario, token).

    O token da sessão já serve: `auth.get_admin_user` relê a conta no banco a cada
    requisição. Com `com_2fa=True` (o estado normal depois do onboarding) o segundo fator
    é ativado e o token fica em `_TOKENS_2FA` para os helpers de HTTP.
    """
    usuario, token, _email, _senha = _registrar_usuario_teste(nome)
    _definir_admin(usuario["id"], True)
    usuario = {**usuario, "is_admin": True}
    try:
        if com_2fa:
            _ativar_2fa(usuario, token)
            usuario = {**usuario, "totp_ativo": True}
        yield usuario, token
    finally:
        _TOKENS_2FA.pop(token, None)
        _apagar_usuario_teste(usuario["id"])


def _gerar_certificado_pfx(
    cnpj=CNPJ_CERTIFICADO_TESTE, senha=SENHA_CERTIFICADO_TESTE, dias_validade=365
):
    """Gera um certificado A1 de teste (.pfx) com o CNPJ no titular.

    Usa `cryptography` (mesma biblioteca da barreira no backend) para produzir um PKCS#12
    real cifrado com a senha — é o que permite testar senha errada e arquivo de outro CNPJ.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID

    chave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nome = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, f"EMPRESA TESTE INTEGRIDADE:{cnpj}"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Empresa Teste Integridade"),
            x509.NameAttribute(NameOID.COUNTRY_NAME, "BR"),
        ]
    )
    agora = datetime.now(timezone.utc)
    fim = agora + timedelta(days=dias_validade)
    inicio = agora - timedelta(days=1)
    if inicio >= fim:  # certificado já vencido: o início precisa ser anterior ao fim
        inicio = fim - timedelta(days=1)
    certificado = (
        x509.CertificateBuilder()
        .subject_name(nome)
        .issuer_name(nome)
        .public_key(chave.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(inicio)
        .not_valid_after(fim)
        .sign(chave, hashes.SHA256())
    )
    return pkcs12.serialize_key_and_certificates(
        name=b"certificado-teste",
        key=chave,
        cert=certificado,
        cas=None,
        encryption_algorithm=serialization.BestAvailableEncryption(senha.encode("utf-8")),
    )


def _certificado_teste(
    cnpj=CNPJ_CERTIFICADO_TESTE, senha=SENHA_CERTIFICADO_TESTE, dias_validade=365
):
    """Conteúdo do .pfx de teste (reaproveitado entre os testes da mesma execução)."""
    chave = (cnpj, senha, dias_validade)
    if chave not in _CERTIFICADOS_GERADOS:
        _CERTIFICADOS_GERADOS[chave] = _gerar_certificado_pfx(cnpj, senha, dias_validade)
    return _CERTIFICADOS_GERADOS[chave]


def _post_cadastro_empresa(
    cnpj,
    token,
    certificado=None,
    senha_certificado=SENHA_CERTIFICADO_TESTE,
    nome_arquivo="certificado-teste.pfx",
    cabecalho_extra=None,
):
    """POST /api/empresas/cadastrar em multipart (arquivo + senha do certificado)."""
    cabecalho = {"Authorization": f"Bearer {token}"} if token else {}
    cabecalho.update(cabecalho_extra or {})
    arquivos = (
        {"certificado": (nome_arquivo, certificado, "application/x-pkcs12")}
        if certificado is not None
        else None
    )
    return requests.post(
        BASE_API + "/api/empresas/cadastrar",
        data={"cnpj": cnpj, "senha_certificado": senha_certificado},
        files=arquivos,
        headers=cabecalho,
        timeout=120,
    )


def _empresas_vinculadas(token):
    """Empresas (CNPJs) da conta, direto da rota protegida."""
    resposta = requests.get(
        BASE_API + "/api/empresas/vinculadas",
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )
    assert resposta.status_code == 200, (
        f"/api/empresas/vinculadas: {resposta.status_code} {resposta.text[:300]}"
    )
    return resposta.json()


def _criar_empresa_teste(cnpj):
    """Empresa órfã (sem dono) usada para testar a adoção por CNPJ."""
    db = SessionLocal()
    try:
        if not db.query(Empresa).filter(Empresa.cnpj == cnpj).first():
            db.add(
                Empresa(
                    cnpj=cnpj,
                    razao_social="EMPRESA TESTE INTEGRIDADE",
                    nome_loja="Loja Teste Integridade",
                    municipio="Teste",
                )
            )
            db.commit()
    finally:
        db.close()


def _apagar_empresa_teste(cnpj):
    db = SessionLocal()
    try:
        db.query(Empresa).filter(Empresa.cnpj == cnpj).delete()
        db.commit()
    finally:
        db.close()


def _criar_loja_para_alertas(usuario_id):
    """Loja do teste do motor de alertas: dono = conta do teste e duas notas.

    A nota mais antiga (10 dias atrás) e a mais nova (1 dia atrás) trazem os mesmos dois
    produtos com preço diferente: +18% (aumento) e +60% (divergência). É exatamente o que
    `backend/alertas.py` compara (última x penúltima compra na janela).
    """
    db = SessionLocal()
    try:
        empresa = db.query(Empresa).filter(Empresa.cnpj == CNPJ_ALERTA).first()
        if empresa is None:
            empresa = Empresa(
                cnpj=CNPJ_ALERTA,
                razao_social="LOJA ALERTA TESTE",
                nome_loja="Loja Alerta Teste",
                municipio="Teste",
            )
            db.add(empresa)
        empresa.usuario_id = usuario_id
        db.commit()
        db.refresh(empresa)

        fornecedor = (
            db.query(Fornecedor)
            .filter(Fornecedor.cnpj == CNPJ_FORNECEDOR_ALERTA)
            .first()
        )
        if fornecedor is None:
            fornecedor = Fornecedor(
                cnpj=CNPJ_FORNECEDOR_ALERTA, nome="FORNECEDOR ALERTA TESTE"
            )
            db.add(fornecedor)
            db.commit()
            db.refresh(fornecedor)

        hoje = date.today()
        for dias_atras, precos in ((10, (5.0, 2.0)), (1, (5.9, 3.2))):
            precos = list(precos)
            nota = NotaFiscal(
                chave_acesso=f"TESTE-ALERTA-{uuid.uuid4().hex[:12]}",
                numero_nota="1",
                serie="1",
                data_emissao=hoje - timedelta(days=dias_atras),
                valor_total=sum(precos),
                empresa_id=empresa.id,
            )
            db.add(nota)
            db.commit()
            db.refresh(nota)
            for indice, (descricao, valor) in enumerate(zip(PRODUTOS_ALERTA, precos)):
                db.add(
                    ItemNota(
                        nota_id=nota.id,
                        fornecedor_id=fornecedor.id,
                        ean=f"78999999001{indice + 10}",
                        descricao_produto=descricao,
                        quantidade=10.0,
                        valor_unitario=valor,
                        valor_total=valor * 10,
                        ncm="00000000",
                        cfop="5102",
                    )
                )
            db.commit()
        return CNPJ_ALERTA
    finally:
        db.close()


def _apagar_loja_para_alertas():
    """Apaga a loja do teste de alertas, as notas, os itens e o fornecedor."""
    db = SessionLocal()
    try:
        empresa = db.query(Empresa).filter(Empresa.cnpj == CNPJ_ALERTA).first()
        if empresa is not None:
            notas = [
                nota.id
                for nota in db.query(NotaFiscal)
                .filter(NotaFiscal.empresa_id == empresa.id)
                .all()
            ]
            if notas:
                db.query(ItemNota).filter(ItemNota.nota_id.in_(notas)).delete(
                    synchronize_session=False
                )
                db.query(NotaFiscal).filter(NotaFiscal.id.in_(notas)).delete(
                    synchronize_session=False
                )
            db.delete(empresa)
        db.query(Fornecedor).filter(Fornecedor.cnpj == CNPJ_FORNECEDOR_ALERTA).delete()
        db.commit()
    finally:
        db.close()


def _vincular_empresas_ao_usuario(usuario_id):
    """Liga todas as empresas ao usuário do teste (como a conta dona faria).

    Devolve o vínculo anterior de cada empresa para o teste desfazer no finally.
    """
    db = SessionLocal()
    try:
        empresas = db.query(Empresa).all()
        anterior = {empresa.id: empresa.usuario_id for empresa in empresas}
        for empresa in empresas:
            empresa.usuario_id = usuario_id
        db.commit()
    finally:
        db.close()
    return anterior


def _restaurar_vinculo(anterior):
    db = SessionLocal()
    try:
        for empresa_id, usuario_id in anterior.items():
            db.query(Empresa).filter(Empresa.id == empresa_id).update(
                {"usuario_id": usuario_id}
            )
        db.commit()
    finally:
        db.close()


@teste
def test_api_autenticacao_e_empresas_vinculadas():
    """Registro/login/token e isolamento das lojas por conta (multilojas)."""
    usuario, token, email, senha = _registrar_usuario_teste()
    try:
        assert _empresas_vinculadas(token) == [], "conta nova não deve ter lojas"

        # Rotas protegidas exigem token válido.
        sem_token = requests.get(BASE_API + "/api/empresas/vinculadas", timeout=30)
        assert sem_token.status_code == 401, sem_token.status_code
        invalido = requests.get(
            BASE_API + "/api/empresas/vinculadas",
            headers={"Authorization": "Bearer token.invalido.aqui"},
            timeout=30,
        )
        assert invalido.status_code == 401, invalido.status_code

        # Login com senha errada e com a senha certa.
        errado = _post_api("/api/auth/login", {"email": email, "senha": "senha-errada"})
        assert errado.status_code == 401, errado.status_code
        certo = _post_api("/api/auth/login", {"email": email, "senha": senha})
        assert certo.status_code == 200 and certo.json().get("token"), certo.text[:200]
        # Conta nova nasce sem privilégio de administrador.
        assert certo.json()["usuario"]["is_admin"] is False, certo.text[:300]

        # Cadastro: e-mail repetido e senha curta.
        repetido = _post_api(
            "/api/auth/registrar", {"nome": "Outra Pessoa", "email": email, "senha": senha}
        )
        assert repetido.status_code == 409, repetido.status_code
        curta = _post_api(
            "/api/auth/registrar",
            {"nome": "Senha Curta", "email": _novo_email_teste(), "senha": "123"},
        )
        assert curta.status_code == 400, curta.status_code

        # CNPJ órfão (importado antes do login) é adotado por quem cadastra — e o vínculo
        # agora exige o certificado digital (.pfx) + senha.
        _criar_empresa_teste(CNPJ_TESTE)
        adocao = _post_cadastro_empresa(
            CNPJ_TESTE, token, certificado=_certificado_teste()
        )
        assert adocao.status_code == 200, adocao.text[:300]
        corpo_adocao = adocao.json()
        assert corpo_adocao["empresa"]["usuario_id"] == usuario["id"]
        assert corpo_adocao["empresa"]["certificado"]["validade"], corpo_adocao
        assert [loja["cnpj"] for loja in _empresas_vinculadas(token)] == [CNPJ_TESTE]

        # Outra conta não pode cadastrar o mesmo CNPJ.
        outro, token_outro, _, _ = _registrar_usuario_teste("Outra Conta Teste")
        try:
            negado = _post_cadastro_empresa(
                CNPJ_TESTE, token_outro, certificado=_certificado_teste()
            )
            assert negado.status_code == 409, negado.text[:300]
            assert _empresas_vinculadas(token_outro) == []
        finally:
            _apagar_usuario_teste(outro["id"])
    finally:
        _apagar_empresa_teste(CNPJ_TESTE)
        _apagar_usuario_teste(usuario["id"])


@teste
def test_api_auditoria_exige_admin():
    """Rotas de auditoria: 401 sem token, 403 para conta comum e 200 para o admin."""
    usuario, token_comum, _, _ = _registrar_usuario_teste("Usuario Comum")
    try:
        assert usuario["is_admin"] is False
        cabecalho = {"Authorization": f"Bearer {token_comum}"}
        chave = f"TESTE-INTEGRIDADE-{uuid.uuid4().hex[:10]}"
        rotas = [
            ("GET", "/api/auditados", None),
            ("GET", "/api/auditados/pendentes", None),
            ("GET", "/api/regras-agrupamento", None),
            ("GET", "/api/nota/1", None),
            ("POST", "/api/auditar", {"chave_identificacao": chave}),
            ("POST", "/api/auditar/pendente", {"chaves": [chave]}),
            ("DELETE", f"/api/auditar/{chave}", None),
        ]
        for metodo, caminho, corpo in rotas:
            sem_token = requests.request(metodo, BASE_API + caminho, json=corpo, timeout=30)
            assert sem_token.status_code == 401, (metodo, caminho, sem_token.status_code)
            comum = requests.request(
                metodo, BASE_API + caminho, json=corpo, headers=cabecalho, timeout=30
            )
            assert comum.status_code == 403, (
                f"{metodo} {caminho} deveria ser 403 para conta comum: "
                f"{comum.status_code} {comum.text[:200]}"
            )

        # O bloqueio não gravou nada na auditoria.
        with conta_admin_teste() as (_admin, token_admin):
            assert chave not in _api("/api/auditados/pendentes", token=token_admin)
            assert _api("/api/auditados", token=token_admin) is not None
            assert _api("/api/regras-agrupamento", token=token_admin) is not None
            # /api/nota/{id} responde 200 (ou 404 se o item não existir) — nunca 403.
            nota = requests.get(
                BASE_API + "/api/nota/1",
                headers=_cabecalho_admin(token_admin),
                timeout=30,
            )
            assert nota.status_code in (200, 404), nota.text[:300]

            # Segundo fator: o token de sessão sozinho NÃO abre a Auditoria (401).
            sem_2fa = requests.get(
                BASE_API + "/api/auditados",
                headers={"Authorization": f"Bearer {token_admin}"},
                timeout=30,
            )
            assert sem_2fa.status_code == 401, sem_2fa.text[:300]
            assert "código" in sem_2fa.json()["detail"].lower(), sem_2fa.json()

        # Admin ainda SEM 2FA configurado: 403 pedindo o setup (a tela mostra o QR Code).
        with conta_admin_teste(com_2fa=False) as (_admin_sem_2fa, token_sem_2fa):
            bloqueado = requests.get(
                BASE_API + "/api/auditados",
                headers=_cabecalho_admin(token_sem_2fa),
                timeout=30,
            )
            assert bloqueado.status_code == 403, bloqueado.text[:300]
            assert bloqueado.headers.get("X-2FA-Required") == "setup", bloqueado.headers
            assert "2fa" in bloqueado.json()["detail"].lower(), bloqueado.json()
    finally:
        _apagar_usuario_teste(usuario["id"])


@teste
def test_api_2fa_setup_e_verify():
    """2FA: setup gera segredo/QR, código errado não ativa e o certo devolve o token."""
    import pyotp

    usuario, token, _, _ = _registrar_usuario_teste("Conta 2FA")
    cabecalho = {"Authorization": f"Bearer {token}"}
    try:
        # Sem sessão não há como gerar o segredo.
        sem_login = requests.post(BASE_API + "/api/auth/2fa/setup", json={}, timeout=30)
        assert sem_login.status_code == 401, sem_login.text[:300]

        # Código informado antes de existir segredo.
        sem_segredo = requests.post(
            BASE_API + "/api/auth/2fa/verify",
            json={"codigo": "123456"},
            headers=cabecalho,
            timeout=30,
        )
        assert sem_segredo.status_code == 400, sem_segredo.text[:300]

        setup = requests.post(
            BASE_API + "/api/auth/2fa/setup",
            json={"reiniciar": True},
            headers=cabecalho,
            timeout=60,
        )
        assert setup.status_code == 200, setup.text[:300]
        corpo = setup.json()
        assert corpo["segredo"], corpo
        assert corpo["ativo"] is False and corpo["ja_configurado"] is False, corpo
        assert corpo["otpauth_url"].startswith("otpauth://totp/"), corpo["otpauth_url"]
        assert corpo["qr_code"].startswith("data:image/png;base64,"), corpo["qr_code"][:40]
        assert corpo["conta"] == usuario["email"] and corpo["emissor"], corpo

        # Gerar o QR ainda NÃO ativa o 2FA (o login continua só com senha).
        assert _api("/api/auth/me", token=token)["usuario"]["totp_ativo"] is False

        # Chamar o setup de novo (a tela rerenderiza a cada interação) reaproveita o MESMO
        # segredo: o QR Code que o usuário escaneou não pode mudar.
        pendente = requests.post(
            BASE_API + "/api/auth/2fa/setup", json={}, headers=cabecalho, timeout=30
        )
        assert pendente.status_code == 200, pendente.text[:300]
        assert pendente.json()["segredo"] == corpo["segredo"], pendente.json()
        assert pendente.json()["ja_configurado"] is False, pendente.json()
        assert pendente.json()["qr_code"].startswith("data:image/png;base64,"), pendente.json()

        # Código fora do formato → 400; código de 6 dígitos errado → 401 e nada ativa.
        curto = _post_api("/api/auth/2fa/verify", {"codigo": "123"}, token=token)
        assert curto.status_code == 400, curto.text[:300]
        atual = pyotp.TOTP(corpo["segredo"]).now()
        errado = "000000" if atual != "000000" else "111111"
        invalido = _post_api("/api/auth/2fa/verify", {"codigo": errado}, token=token)
        assert invalido.status_code == 401, invalido.text[:300]
        assert _api("/api/auth/me", token=token)["usuario"]["totp_ativo"] is False

        # Código correto: ativa o 2FA e devolve o token do segundo fator.
        valido = _post_api(
            "/api/auth/2fa/verify",
            {"codigo": pyotp.TOTP(corpo["segredo"]).now()},
            token=token,
        )
        assert valido.status_code == 200, valido.text[:300]
        dados = valido.json()
        assert dados["ativado"] is True, dados
        assert dados["token_2fa"] and dados["expira_em"], dados
        assert dados["usuario"]["totp_ativo"] is True, dados["usuario"]
        assert _api("/api/auth/me", token=token)["usuario"]["totp_ativo"] is True

        # O token de 2FA não vale como token de sessão.
        como_sessao = requests.get(
            BASE_API + "/api/auth/me",
            headers={"Authorization": f"Bearer {dados['token_2fa']}"},
            timeout=30,
        )
        assert como_sessao.status_code == 401, como_sessao.text[:300]

        # Com o 2FA ativo, o setup não devolve mais o segredo (não rotaciona sem pedir).
        repetido = requests.post(
            BASE_API + "/api/auth/2fa/setup", json={}, headers=cabecalho, timeout=30
        )
        assert repetido.status_code == 200, repetido.text[:300]
        assert repetido.json()["ja_configurado"] is True, repetido.json()
        assert repetido.json()["segredo"] is None, repetido.json()

        # LOGIN do admin: a senha sozinha NÃO abre sessão (2FA é obrigatório no login).
        login = _post_api(
            "/api/auth/login", {"email": usuario["email"], "senha": SENHA_TESTE}
        )
        assert login.status_code == 401, login.text[:300]
        detalhe_login = login.json()["detail"]
        assert detalhe_login["motivo"] == "codigo_ausente", detalhe_login
        assert detalhe_login["codigo_2fa_necessario"] is True, detalhe_login
        assert detalhe_login["desafio"], detalhe_login
        assert "token" not in login.json(), login.json()

        # Código errado no login: continua bloqueado.
        invalido_login = _post_api(
            "/api/auth/login",
            {"email": usuario["email"], "senha": SENHA_TESTE, "codigo_2fa": errado},
        )
        assert invalido_login.status_code == 401, invalido_login.text[:300]
        assert invalido_login.json()["detail"]["motivo"] == "codigo_invalido", invalido_login.json()

        # Código certo: sessão + token do segundo fator (a Auditoria já abre).
        certo_login = _post_api(
            "/api/auth/login",
            {
                "email": usuario["email"],
                "senha": SENHA_TESTE,
                "codigo_2fa": pyotp.TOTP(corpo["segredo"]).now(),
            },
        )
        assert certo_login.status_code == 200, certo_login.text[:300]
        sessao_login = certo_login.json()
        assert sessao_login["usuario"]["totp_ativo"] is True, sessao_login["usuario"]
        assert sessao_login["token"] and sessao_login["token_2fa"], sessao_login
        _TOKENS_2FA.pop(sessao_login["token"], None)
        # O token do segundo fator nunca vale como sessão.
        como_sessao = requests.get(
            BASE_API + "/api/auth/me",
            headers={"Authorization": f"Bearer {sessao_login['token_2fa']}"},
            timeout=30,
        )
        assert como_sessao.status_code == 401, como_sessao.text[:300]
    finally:
        _apagar_usuario_teste(usuario["id"])


@teste
def test_api_cadastro_exige_certificado():
    """Barreira do certificado: sem arquivo/senha (ou com dados inválidos) nada é vinculado."""
    usuario, token, _, _ = _registrar_usuario_teste("Conta Sem Certificado")
    try:
        # Nada é enviado: bloqueia com a mensagem do certificado.
        sem_nada = _post_cadastro_empresa(
            CNPJ_TESTE, token, certificado=None, senha_certificado=""
        )
        assert sem_nada.status_code == 400, sem_nada.text[:300]
        assert bcerts.ERRO_SEM_ARQUIVO in sem_nada.json()["detail"], sem_nada.text[:300]

        # Só a senha, sem o arquivo.
        so_senha = _post_cadastro_empresa(CNPJ_TESTE, token, certificado=None)
        assert so_senha.status_code == 400, so_senha.text[:300]
        assert bcerts.ERRO_SEM_ARQUIVO in so_senha.json()["detail"], so_senha.text[:300]

        # Arquivo sem a senha.
        sem_senha = _post_cadastro_empresa(
            CNPJ_TESTE, token, certificado=_certificado_teste(), senha_certificado=""
        )
        assert sem_senha.status_code == 400, sem_senha.text[:300]
        assert bcerts.ERRO_SEM_SENHA in sem_senha.json()["detail"], sem_senha.text[:300]

        # Extensão errada (o A1 tem que ser .pfx/.p12).
        texto = _post_cadastro_empresa(
            CNPJ_TESTE,
            token,
            certificado=_certificado_teste(),
            nome_arquivo="certificado.txt",
        )
        assert texto.status_code == 400, texto.text[:300]
        assert bcerts.ERRO_EXTENSAO in texto.json()["detail"], texto.text[:300]

        # Senha errada: o PKCS#12 não abre.
        senha_errada = _post_cadastro_empresa(
            CNPJ_TESTE, token, certificado=_certificado_teste(), senha_certificado="errada"
        )
        assert senha_errada.status_code == 400, senha_errada.text[:300]
        assert bcerts.ERRO_SENHA in senha_errada.json()["detail"], senha_errada.text[:300]

        # Certificado de outro CNPJ não vincula a loja.
        outro_certificado = _certificado_teste(cnpj="88888888000188", senha="Outro@123")
        divergente = _post_cadastro_empresa(
            CNPJ_TESTE,
            token,
            certificado=outro_certificado,
            senha_certificado="Outro@123",
        )
        assert divergente.status_code == 400, divergente.text[:300]
        assert (
            divergente.json()["detail"] == bcerts.ERRO_CNPJ
        ), divergente.text[:300]

        # Certificado vencido também é barrado.
        vencido = _post_cadastro_empresa(
            CNPJ_TESTE,
            token,
            certificado=_certificado_teste(senha="Vencido@123", dias_validade=-30),
            senha_certificado="Vencido@123",
        )
        assert vencido.status_code == 400, vencido.text[:300]
        assert vencido.json()["detail"] == bcerts.ERRO_VENCIDO, vencido.text[:300]

        # Nenhuma dessas tentativas criou ou vinculou a loja.
        assert _empresas_vinculadas(token) == []

        # Com o certificado certo e a senha certa, a loja é criada e vinculada.
        valido = _post_cadastro_empresa(CNPJ_TESTE, token, certificado=_certificado_teste())
        assert valido.status_code == 200, valido.text[:300]
        empresa = valido.json()["empresa"]
        assert empresa["cnpj"] == CNPJ_TESTE and empresa["usuario_id"] == usuario["id"]
        assert empresa["certificado"]["validade"], empresa
        assert empresa["certificado"]["titular"], empresa
        assert [loja["cnpj"] for loja in _empresas_vinculadas(token)] == [CNPJ_TESTE]
    finally:
        _apagar_empresa_teste(CNPJ_TESTE)
        _apagar_usuario_teste(usuario["id"])


@teste
def test_acbr_cadastra_loja_local_quando_cnpj_ainda_nao_existe():
    """POST /api/acbr/empresas cria a loja no banco antes de chamar a ACBr.

    Antes, um CNPJ sem vínculo retornava 404 e o cadastro parecia "silencioso"
    (nada aparecia em Minhas Lojas). A rota agora valida o certificado, cria a
    empresa com o dono logado e devolve o objeto persistido. A ACBr é simulada:
    o teste não toca na rede nem no banco real. Fecha ainda a visão de suporte:
    a loja continua sendo da conta que cadastrou, mas o admin a vê em "Minhas
    Lojas" com o e-mail do dono para conferir/reenviar o certificado.
    """
    import asyncio
    from unittest.mock import patch

    db, engine = _banco_aprendizado_temporario()
    try:
        # O admin existe PRIMEIRO e recebe o id 1 (como na base real), para que o
        # teste falhe se a criação voltar a gravar um dono fixo em vez de `usuario.id`.
        admin = Usuario(
            nome="Admin Controle",
            email=f"admin.controle.{uuid.uuid4().hex[:8]}@teste.local",
            senha_hash="nao-usado",
            is_admin=True,
        )
        db.add(admin)
        db.commit()
        db.refresh(admin)

        usuario = Usuario(
            nome="Conta ACBr Direta",
            email=f"acbr.direto.{uuid.uuid4().hex[:8]}@teste.local",
            senha_hash="nao-usado",
            is_admin=False,
        )
        db.add(usuario)
        db.commit()
        db.refresh(usuario)
        assert admin.id != usuario.id
        assert admin.id == 1, admin.id

        # Loja do admin (dono 1) de controle: ela NUNCA pode ser gravada como dona
        # da loja criada pela conta nova nem aparecer em `/api/empresas/vinculadas`.
        cnpj_do_admin = "99887766000155"
        db.add(
            Empresa(
                cnpj=cnpj_do_admin,
                razao_social="LOJA DO ADMIN LTDA",
                nome_loja="Loja do Admin",
                municipio="SAO PAULO",
                usuario_id=admin.id,
            )
        )
        db.commit()

        endereco = {
            "razao_social": "EMPRESA NOVA LTDA",
            "nome_loja": "Loja Nova",
            "municipio": "SAO PAULO",
            "logradouro": "RUA TESTE",
            "numero": "10",
            "complemento": "",
            "bairro": "CENTRO",
            "codigo_municipio": "3550308",
            "uf": "SP",
            "cep": "01001000",
            "email": "financeiro@teste.local",
            "fonte": "brasilapi",
        }

        class ClienteAcbrFalso:
            ambiente_distribuicao = "homologacao"

            def consultar_empresa(self, cnpj):
                return None

            def cadastrar_empresa(self, dados):
                return {"cpf_cnpj": dados["cpf_cnpj"]}

            def enviar_certificado(self, cnpj, conteudo, senha):
                assert conteudo and senha
                return {"serial_number": "TESTE123"}

            def configurar_distribuicao_nfe(self, cnpj, ambiente):
                return {"ambiente": ambiente}

        upload = bmain.UploadFile(
            file=BytesIO(_certificado_teste()),
            filename="certificado-teste.pfx",
        )
        with patch.object(bmain, "_consultar_cnpj", return_value=endereco), patch.object(
            bmain, "_cliente_acbr", return_value=ClienteAcbrFalso()
        ):
            resposta = asyncio.run(
                bmain.cadastrar_empresa_na_acbr(
                    background_tasks=bmain.BackgroundTasks(),
                    usuario=usuario,
                    db=db,
                    cnpj=CNPJ_CERTIFICADO_TESTE,
                    senha_certificado=SENHA_CERTIFICADO_TESTE,
                    certificado=upload,
                    # A chamada é direta (sem FastAPI), então os defaults
                    # `Form("")` precisam ser passados como texto vazio.
                    cep="",
                    logradouro="",
                    numero="",
                    complemento="",
                    bairro="",
                    codigo_municipio="",
                    cidade="",
                    uf="",
                    email="",
                    telefone="",
                    inscricao_estadual="",
                    inscricao_municipal="",
                    configurar_distribuicao=True,
                )
            )

        assert resposta["sucesso"] is True, resposta
        assert resposta["salva_no_banco"] is True, resposta
        assert resposta["banco"]["acao"] == "criada", resposta
        assert resposta["empresa"]["cnpj"] == CNPJ_CERTIFICADO_TESTE, resposta
        assert resposta["empresa"]["usuario_id"] == usuario.id, resposta

        db.expire_all()
        persistida = (
            db.query(Empresa)
            .filter(Empresa.cnpj == CNPJ_CERTIFICADO_TESTE)
            .one()
        )
        assert persistida.usuario_id == usuario.id, persistida.usuario_id
        # Prova explícita de que o vínculo NÃO caiu no admin (o antigo ID fixo 1).
        assert persistida.usuario_id != admin.id, persistida.usuario_id
        assert persistida.nome_loja == "Loja Nova", persistida.nome_loja
        assert persistida.certificado_validade, persistida.certificado_validade
        assert persistida.cep == "01001000", persistida.cep

        # Para uma conta comum, "Minhas Lojas" só devolve as lojas dela: a loja do
        # admin e a loja recém-criada por outra conta nunca aparecem misturadas.
        lojas_do_usuario = bmain.listar_empresas_vinculadas(usuario=usuario, db=db)
        assert [loja["cnpj"] for loja in lojas_do_usuario] == [
            CNPJ_CERTIFICADO_TESTE
        ], lojas_do_usuario
        assert all(loja["usuario_id"] == usuario.id for loja in lojas_do_usuario)
        assert cnpj_do_admin not in {loja["cnpj"] for loja in lojas_do_usuario}

        lojas_do_admin = bmain.listar_empresas_vinculadas(usuario=admin, db=db)
        por_cnpj_admin = {loja["cnpj"]: loja for loja in lojas_do_admin}
        assert cnpj_do_admin in por_cnpj_admin
        # Visão de suporte: o admin vê a loja da outra conta, mas o dono continua
        # quem cadastrou — e o e-mail do dono volta para o cartão do suporte.
        da_outra_conta = por_cnpj_admin[CNPJ_CERTIFICADO_TESTE]
        assert da_outra_conta["usuario_id"] == usuario.id, da_outra_conta
        assert da_outra_conta["dono_email"] == usuario.email, da_outra_conta
        # O alerta continua sendo por dono: a conta do admin NÃO recebe o e-mail.
        assert da_outra_conta["dono_email"] != admin.email, da_outra_conta
    finally:
        db.close()
        engine.dispose()


@teste
def test_acbr_erro_inesperado_retorna_json_500_com_detalhe():
    """Exceção inesperada na rota ACBr aparece como JSON 500 com o erro real.

    Antes, um erro fora dos ``ErroAcbr``/``HTTPException`` subia como 500 opaco
    (sem `detail`), então a tela não conseguia mostrar a causa. A rota agora
    devolve ``{"detail": "Erro interno: ..."}`` e imprime ``DEBUG`` no terminal.
    """
    import asyncio
    from unittest.mock import patch

    db, engine = _banco_aprendizado_temporario()
    try:
        usuario = Usuario(
            nome="Conta Erro ACBr",
            email=f"acbr.erro.{uuid.uuid4().hex[:8]}@teste.local",
            senha_hash="nao-usado",
            is_admin=False,
        )
        db.add(usuario)
        db.commit()
        db.refresh(usuario)

        with patch.object(
            bcerts,
            "validar_certificado",
            side_effect=RuntimeError("falha proposital no certificado"),
        ):
            resposta = asyncio.run(
                bmain.cadastrar_empresa_na_acbr(
                    background_tasks=bmain.BackgroundTasks(),
                    usuario=usuario,
                    db=db,
                    cnpj=CNPJ_CERTIFICADO_TESTE,
                    senha_certificado="",
                    certificado=None,
                    cep="",
                    logradouro="",
                    numero="",
                    complemento="",
                    bairro="",
                    codigo_municipio="",
                    cidade="",
                    uf="",
                    email="",
                    telefone="",
                    inscricao_estadual="",
                    inscricao_municipal="",
                    configurar_distribuicao=True,
                )
            )

        assert resposta.status_code == 500, resposta
        corpo = json.loads(resposta.body)
        assert corpo["detail"] == (
            "Erro interno: falha proposital no certificado"
        ), corpo
    finally:
        db.close()
        engine.dispose()


@teste
def test_acbr_erro_ao_criar_empresa_retorna_json_500_com_detalhe():
    """Falha ao criar a loja no banco devolve 500 com ``Erro ao cadastrar``.

    A criação acontece antes da chamada à ACBr. Se `_consultar_cnpj` (ou o
    commit) falhar, a rota precisa devolver o motivo no ``detail`` — sem isso a
    tela só via uma lista vazia e o cadastro parecia silencioso. O teste também
    confere os `DEBUG` impressos no terminal.
    """
    import asyncio
    import contextlib
    import io
    from unittest.mock import patch

    db, engine = _banco_aprendizado_temporario()
    try:
        usuario = Usuario(
            nome="Conta Erro Criacao",
            email=f"acbr.criacao.{uuid.uuid4().hex[:8]}@teste.local",
            senha_hash="nao-usado",
            is_admin=False,
        )
        db.add(usuario)
        db.commit()
        db.refresh(usuario)

        upload = bmain.UploadFile(
            file=BytesIO(_certificado_teste()),
            filename="certificado-teste.pfx",
        )
        saida = io.StringIO()
        with patch.object(
            bmain,
            "_consultar_cnpj",
            side_effect=RuntimeError("falha proposital ao criar a loja"),
        ), contextlib.redirect_stdout(saida):
            resposta = asyncio.run(
                bmain.cadastrar_empresa_na_acbr(
                    background_tasks=bmain.BackgroundTasks(),
                    usuario=usuario,
                    db=db,
                    cnpj=CNPJ_CERTIFICADO_TESTE,
                    senha_certificado=SENHA_CERTIFICADO_TESTE,
                    certificado=upload,
                    cep="",
                    logradouro="",
                    numero="",
                    complemento="",
                    bairro="",
                    codigo_municipio="",
                    cidade="",
                    uf="",
                    email="",
                    telefone="",
                    inscricao_estadual="",
                    inscricao_municipal="",
                    configurar_distribuicao=True,
                )
            )

        assert resposta.status_code == 500, resposta
        corpo = json.loads(resposta.body)
        assert corpo["detail"] == (
            "Erro ao cadastrar: falha proposital ao criar a loja"
        ), corpo
        logs = saida.getvalue()
        assert (
            f"DEBUG: Tentando cadastrar CNPJ {CNPJ_CERTIFICADO_TESTE}" in logs
        ), logs
        assert "DEBUG: Erro ao cadastrar" in logs, logs
        assert (
            db.query(Empresa)
            .filter(Empresa.cnpj == CNPJ_CERTIFICADO_TESTE)
            .first()
            is None
        ), "a falha não pode deixar uma loja parcialmente gravada"
    finally:
        db.close()
        engine.dispose()


@teste
def test_acbr_erro_de_commit_ao_criar_empresa_retorna_json_500():
    """O 500 de `_salvar_empresa_no_banco` também vira ``Erro ao cadastrar``."""
    import asyncio
    from unittest.mock import patch

    db, engine = _banco_aprendizado_temporario()
    try:
        usuario = Usuario(
            nome="Conta Erro Commit",
            email=f"acbr.commit.{uuid.uuid4().hex[:8]}@teste.local",
            senha_hash="nao-usado",
            is_admin=False,
        )
        db.add(usuario)
        db.commit()
        db.refresh(usuario)

        upload = bmain.UploadFile(
            file=BytesIO(_certificado_teste()),
            filename="certificado-teste.pfx",
        )
        with patch.object(
            bmain,
            "_consultar_cnpj",
            return_value={
                "razao_social": "EMPRESA TESTE",
                "nome_loja": "Loja Teste",
                "municipio": "SAO PAULO",
                "cep": "01001000",
                "logradouro": "RUA TESTE",
                "numero": "1",
                "bairro": "CENTRO",
                "codigo_municipio": "3550308",
                "uf": "SP",
            },
        ), patch.object(
            bmain,
            "_salvar_empresa_no_banco",
            side_effect=bmain.HTTPException(
                status_code=500, detail="falha proposital no commit"
            ),
        ):
            resposta = asyncio.run(
                bmain.cadastrar_empresa_na_acbr(
                    background_tasks=bmain.BackgroundTasks(),
                    usuario=usuario,
                    db=db,
                    cnpj=CNPJ_CERTIFICADO_TESTE,
                    senha_certificado=SENHA_CERTIFICADO_TESTE,
                    certificado=upload,
                    cep="",
                    logradouro="",
                    numero="",
                    complemento="",
                    bairro="",
                    codigo_municipio="",
                    cidade="",
                    uf="",
                    email="",
                    telefone="",
                    inscricao_estadual="",
                    inscricao_municipal="",
                    configurar_distribuicao=True,
                )
            )

        assert resposta.status_code == 500, resposta
        corpo = json.loads(resposta.body)
        assert corpo["detail"] == (
            "Erro ao cadastrar: falha proposital no commit"
        ), corpo
        assert (
            db.query(Empresa)
            .filter(Empresa.cnpj == CNPJ_CERTIFICADO_TESTE)
            .first()
            is None
        )
    finally:
        db.close()
        engine.dispose()


@contextmanager
def app_test_logado(default_timeout=240, admin=False, com_2fa=True):
    """AppTest já logado: conta de teste + empresas vinculadas, tudo desfeito no fim.

    Com `admin=True` a conta recebe `is_admin` e, com `com_2fa=True`, o 2FA é ativado e o
    token do segundo fator entra na sessão do app — é isso que libera a Auditoria. Com
    `com_2fa=False` a página deve mostrar o QR Code no lugar dos dados.
    """
    from streamlit.testing.v1 import AppTest

    usuario, token, _, _ = _registrar_usuario_teste(
        "Admin Integridade" if admin else "Teste Integridade"
    )
    token_2fa, expira_2fa = "", ""
    if admin:
        _definir_admin(usuario["id"], True)
        usuario = {**usuario, "is_admin": True}
        if com_2fa:
            _segredo, token_2fa, expira_2fa = _ativar_2fa(usuario, token)
            usuario = {**usuario, "totp_ativo": True}
    vinculo_anterior = _vincular_empresas_ao_usuario(usuario["id"])
    try:
        app = AppTest.from_file(
            str(RAIZ / "frontend" / "app.py"), default_timeout=default_timeout
        )
        app.session_state[CHAVE_USUARIO_SESSAO] = usuario
        app.session_state[CHAVE_TOKEN_SESSAO] = token
        if token_2fa:
            app.session_state[CHAVE_2FA_SESSAO] = token_2fa
            app.session_state[CHAVE_2FA_EXPIRA_SESSAO] = expira_2fa
        app.run()
        yield app
    finally:
        _TOKENS_2FA.pop(token, None)
        _restaurar_vinculo(vinculo_anterior)
        _apagar_usuario_teste(usuario["id"])


@teste
def test_apptest_login_quando_deslogado():
    """Sem sessão, a tela de Login/Cadastro aparece — e nenhum relatório é exibido."""
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(RAIZ / "frontend" / "app.py"), default_timeout=240)
    app.run()
    assert not app.exception, f"a tela de login levantou exceção: {app.exception}"

    markdowns = "\n".join(elemento.value for elemento in app.markdown)
    assert "login-hero" in markdowns, "o cabeçalho da tela de login não foi renderizado"
    assert "login-rodape" in markdowns, "o rodapé da tela de login não foi renderizado"

    rotulos = [aba.label for aba in app.tabs]
    assert "🔑 Entrar" in rotulos and "✨ Criar conta" in rotulos, rotulos

    campos = {campo.key for campo in app.text_input}
    assert {"login_email", "login_senha", "cad_nome", "cad_email"} <= campos, campos

    titulos = [elemento.value for elemento in app.title]
    assert not any("Relatórios" in titulo or "Auditoria" in titulo for titulo in titulos), (
        titulos
    )
    assert not [radio for radio in app.radio if radio.key == "menu_principal"], (
        "o menu da área logada não pode aparecer antes do login"
    )

    # Credenciais inválidas: continua na tela de entrada, com a mensagem do backend.
    app.text_input(key="login_email").set_value("ninguem@exemplo.com")
    app.text_input(key="login_senha").set_value("senha-errada")
    app.button(key="btn_entrar").click().run()
    assert not app.exception, f"o login inválido levantou exceção: {app.exception}"
    erros = " ".join(elemento.value for elemento in app.error).lower()
    assert "incorretos" in erros, f"mensagem de login inválido ausente: {erros!r}"
    assert not any(
        "Relatórios de Preços" in elemento.value for elemento in app.title
    ), "um login inválido não pode abrir os relatórios"


@teste
def test_apptest_relatorios_quando_logado():
    """Login pela tela → relatórios de todas as lojas → "Minhas Lojas" → logout."""
    from streamlit.testing.v1 import AppTest

    usuario, token, email, senha = _registrar_usuario_teste()
    vinculo_anterior = _vincular_empresas_ao_usuario(usuario["id"])
    try:
        app = AppTest.from_file(str(RAIZ / "frontend" / "app.py"), default_timeout=240)
        app.run()
        # Login pelo formulário (fluxo completo: tela → API → sessão).
        app.text_input(key="login_email").set_value(email)
        app.text_input(key="login_senha").set_value(senha)
        app.button(key="btn_entrar").click().run()
        assert not app.exception, f"o login levantou exceção: {app.exception}"
        assert app.session_state.get(CHAVE_USUARIO_SESSAO, {}).get("email") == email

        titulos = [elemento.value for elemento in app.title]
        assert any("Relatórios de Preços" in titulo for titulo in titulos), titulos

        # Barra lateral: usuário logado e navegação — o seletor "Empresa Ativa" saiu
        # (redundante): o recorte por loja é feito nos filtros de cada aba.
        markdowns = "\n".join(elemento.value for elemento in app.markdown)
        assert usuario["nome"] in markdowns, "o nome do usuário não apareceu na barra lateral"
        assert not [
            campo for campo in app.selectbox if campo.key == "empresa_ativa_sel"
        ], "o seletor de Empresa Ativa deveria ter sido removido da barra lateral"
        assert app.session_state.get("empresa_ativa_id") is None

        # Menu lateral com a opção das lojas.
        menu = app.radio(key="menu_principal")
        assert "🏬 Minhas Lojas" in menu.options, menu.options

        # "Minhas Lojas" lista os CNPJs vinculados à conta.
        lojas = _empresas_vinculadas(token)
        assert lojas, "o usuário do teste deveria ter as empresas vinculadas"
        menu.set_value("🏬 Minhas Lojas").run()
        assert not app.exception, f"Minhas Lojas levantou exceção: {app.exception}"
        titulos = [elemento.value for elemento in app.title]
        assert any("Minhas Lojas" in titulo for titulo in titulos), titulos
        markdowns = "\n".join(elemento.value for elemento in app.markdown)
        for loja in lojas:
            if loja.get("nome_loja"):
                assert loja["nome_loja"] in markdowns, (
                    f"a loja {loja['nome_loja']} não apareceu em Minhas Lojas"
                )
        assert any(campo.key == "nova_loja_cnpj" for campo in app.text_input), (
            "o formulário de novo CNPJ não apareceu"
        )

        # Sair volta para a tela de login.
        app.button(key="btn_sair").click().run()
        assert not app.exception, f"o logout levantou exceção: {app.exception}"
        assert app.session_state.get(CHAVE_USUARIO_SESSAO) is None, (
            "o logout deveria limpar o usuário da sessão"
        )
        assert "login-hero" in "\n".join(elemento.value for elemento in app.markdown), (
            "o logout deveria voltar para a tela de login"
        )
    finally:
        _restaurar_vinculo(vinculo_anterior)
        _apagar_usuario_teste(usuario["id"])


@teste
def test_apptest_login_com_2fa():
    """Login do admin com 2FA: a senha abre a 2ª etapa e só o código correto entra."""
    import pyotp
    from streamlit.testing.v1 import AppTest

    usuario, token, email, senha = _registrar_usuario_teste("Admin Login 2FA")
    _definir_admin(usuario["id"], True)
    segredo, _token_2fa, _expira = _ativar_2fa({**usuario, "is_admin": True}, token)
    try:
        app = AppTest.from_file(str(RAIZ / "frontend" / "app.py"), default_timeout=240)
        app.run()
        app.text_input(key="login_email").set_value(email)
        app.text_input(key="login_senha").set_value(senha)
        app.button(key="btn_entrar").click().run()
        assert not app.exception, f"o login levantou exceção: {app.exception}"

        # Senha correta, mas o código ainda não foi informado: a sessão NÃO abre.
        assert app.session_state.get(CHAVE_USUARIO_SESSAO) is None
        markdowns = "\n".join(elemento.value for elemento in app.markdown)
        assert "Verificação em dois fatores" in markdowns, markdowns[:600]
        assert "2fa_codigo_login" in {campo.key for campo in app.text_input}

        # Código errado: continua na 2ª etapa, com a mensagem do backend.
        errado = "000000" if pyotp.TOTP(segredo).now() != "000000" else "111111"
        app.text_input(key="2fa_codigo_login").set_value(errado)
        app.button(key="btn_2fa_login").click().run()
        assert not app.exception, f"o código inválido levantou exceção: {app.exception}"
        assert app.session_state.get(CHAVE_USUARIO_SESSAO) is None
        erros = " ".join(elemento.value for elemento in app.error).lower()
        assert "inválido" in erros or "expirado" in erros, erros

        # Código correto: sessão aberta, token 2FA guardado e relatórios na tela.
        app.text_input(key="2fa_codigo_login").set_value(pyotp.TOTP(segredo).now())
        app.button(key="btn_2fa_login").click().run()
        assert not app.exception, f"o login com 2FA levantou exceção: {app.exception}"
        assert app.session_state.get(CHAVE_USUARIO_SESSAO, {}).get("email") == email
        assert app.session_state.get(CHAVE_2FA_SESSAO), "o token 2FA não ficou na sessão"
        titulos = [elemento.value for elemento in app.title]
        assert any("Relatórios de Preços" in titulo for titulo in titulos), titulos

        # A Auditoria abre direto (o código já foi confirmado no login).
        app.radio(key="menu_principal").set_value("🔍 Auditoria (Admin)").run()
        assert not app.exception, f"a Auditoria levantou exceção: {app.exception}"
        cabecalhos = [elemento.value for elemento in app.header]
        assert any("Auditoria de Divergências" in texto for texto in cabecalhos), cabecalhos
    finally:
        _TOKENS_2FA.pop(token, None)
        _apagar_usuario_teste(usuario["id"])


@teste
def test_apptest_login_admin_sem_2fa_configura_no_login():
    """Admin sem 2FA: o login mostra o QR Code e só abre a sessão depois do 1º código."""
    import pyotp
    from streamlit.testing.v1 import AppTest

    usuario, token, email, senha = _registrar_usuario_teste("Admin Sem 2FA")
    _definir_admin(usuario["id"], True)
    try:
        app = AppTest.from_file(str(RAIZ / "frontend" / "app.py"), default_timeout=240)
        app.run()
        app.text_input(key="login_email").set_value(email)
        app.text_input(key="login_senha").set_value(senha)
        app.button(key="btn_entrar").click().run()
        assert not app.exception, f"o login levantou exceção: {app.exception}"

        # Senha correta, mas o 2FA ainda não existe: NADA de sessão — a tela pede o
        # cadastro do autenticador (QR Code) e o código de 6 dígitos.
        assert app.session_state.get(CHAVE_USUARIO_SESSAO) is None
        pendente = app.session_state.get(CHAVE_LOGIN_2FA_SESSAO) or {}
        assert pendente.get("modo") == "setup", pendente
        assert pendente.get("desafio"), pendente
        assert app.image, "o QR Code do primeiro acesso não foi desenhado"
        assert "2fa_codigo_login" in {campo.key for campo in app.text_input}

        # O mesmo segredo pendente é devolvido pelo setup (idempotente): o QR que está na
        # tela continua válido mesmo com os reruns do Streamlit.
        setup = requests.post(
            BASE_API + "/api/auth/2fa/setup",
            json={},
            headers={"Authorization": f"Bearer {pendente['desafio']}"},
            timeout=60,
        )
        assert setup.status_code == 200, setup.text[:300]
        segredo = setup.json()["segredo"]
        assert segredo, setup.json()

        # Código correto: ativa o 2FA e conclui o login (sessão + token do 2º fator).
        app.text_input(key="2fa_codigo_login").set_value(pyotp.TOTP(segredo).now())
        app.button(key="btn_2fa_login").click().run()
        assert not app.exception, f"o login com 2FA levantou exceção: {app.exception}"
        sessao = app.session_state.get(CHAVE_USUARIO_SESSAO) or {}
        assert sessao.get("email") == email, sessao
        assert sessao.get("totp_ativo") is True, sessao
        assert app.session_state.get(CHAVE_2FA_SESSAO), "o token 2FA não ficou na sessão"
        assert not [campo for campo in app.text_input if campo.key == "login_email"], (
            "a tela de login continuou aparecendo depois da sessão aberta"
        )
    finally:
        _TOKENS_2FA.pop(token, None)
        _apagar_usuario_teste(usuario["id"])


# ──────────────────────────────────────────────────────────────────────────────
# AppTest: as telas continuam carregando com as regras aplicadas
# ──────────────────────────────────────────────────────────────────────────────
def _linhas_da_tabela_impacto(html_tabela):
    """[(produto, é_aberrante)] na ordem em que as linhas aparecem na tabela HTML."""
    linhas = []
    for pedaco in (html_tabela or "").split("<tr")[1:]:
        inicio = pedaco.find('<td class="prod">')
        if inicio < 0:
            continue
        inicio += len('<td class="prod">')
        fim = pedaco.find("</td>", inicio)
        if fim < 0:
            continue
        linhas.append((html.unescape(pedaco[inicio:fim]), 'class="aberr"' in pedaco))
    return linhas


@teste
def test_apptest_relatorios_carregam():
    """AppTest: com sessão logada de admin, relatórios + aba Auditoria renderizam.

    A conta é administradora porque o teste termina conferindo a Auditoria (menu e página
    só existem para `is_admin`).
    """
    with app_test_logado(admin=True) as app:
        _verificar_tela_de_relatorios(app)


@teste
def test_apptest_menu_auditoria_por_perfil():
    """O menu esconde a Auditoria da conta comum e mostra para o administrador.

    A proteção de verdade é o 403 do backend (test_api_auditoria_exige_admin); aqui
    garantimos que o front não oferece uma tela que responderia 403.
    """
    with app_test_logado() as app_comum:
        assert not app_comum.exception, f"app (conta comum) levantou exceção: {app_comum.exception}"
        menu = app_comum.radio(key="menu_principal")
        assert "🔍 Auditoria (Admin)" not in menu.options, menu.options
        assert menu.options == [
            "📊 Relatórios (Cliente)",
            "🏬 Minhas Lojas",
            "🔔 Configurar Alertas",
        ], menu.options

        # Mesmo forçando a página por baixo dos panos, o app volta para os Relatórios
        # (o menu só oferece as páginas do perfil e a Auditoria não renderiza).
        app_comum.session_state["pagina_atual"] = "🔍 Auditoria (Admin)"
        app_comum.run()
        assert not app_comum.exception, f"app levantou exceção: {app_comum.exception}"
        titulos = [elemento.value for elemento in app_comum.title]
        assert not any("Auditoria" in titulo for titulo in titulos), titulos
        assert app_comum.session_state["pagina_atual"] == "📊 Relatórios (Cliente)", (
            "a página de Auditoria deveria ser descartada para a conta comum"
        )

    with app_test_logado(admin=True) as app_admin:
        assert not app_admin.exception, f"app (admin) levantou exceção: {app_admin.exception}"
        menu_admin = app_admin.radio(key="menu_principal")
        assert "🔍 Auditoria (Admin)" in menu_admin.options, menu_admin.options
        menu_admin.set_value("🔍 Auditoria (Admin)").run()
        assert not app_admin.exception, f"Auditoria levantou exceção: {app_admin.exception}"
        titulos = [elemento.value for elemento in app_admin.title]
        assert any("Auditoria" in titulo for titulo in titulos), titulos
        # O admin do teste já confirmou o código 2FA: os dados da Auditoria aparecem.
        sucessos = " ".join(elemento.value for elemento in app_admin.success)
        assert "2FA verificado" in sucessos, sucessos
        cabecalhos = [elemento.value for elemento in app_admin.header]
        assert any("Auditoria de Divergências" in texto for texto in cabecalhos), cabecalhos
        # A lista de grupos é paginada para não renderizar milhares de cards de uma vez.
        rotulos_select = [elemento.label for elemento in app_admin.selectbox]
        assert "Grupos por página:" in rotulos_select, rotulos_select
        # O formulário de vínculo continua na tela "Minhas Lojas" (agora com certificado).
        app_admin.radio(key="menu_principal").set_value("🏬 Minhas Lojas").run()
        assert not app_admin.exception, f"Minhas Lojas levantou exceção: {app_admin.exception}"
        campos = {campo.key for campo in app_admin.text_input}
        assert "nova_loja_cnpj" in campos and "nova_loja_senha_certificado" in campos, campos
        uploaders = [elemento for elemento in app_admin.get("file_uploader")]
        assert uploaders, "o st.file_uploader do certificado não foi renderizado"


@teste
def test_apptest_auditoria_exige_2fa():
    """Admin SEM 2FA ativo: a Auditoria mostra o QR Code e nenhum dado é carregado."""
    with app_test_logado(admin=True, com_2fa=False) as app_admin:
        assert not app_admin.exception, f"app (admin) levantou exceção: {app_admin.exception}"
        app_admin.radio(key="menu_principal").set_value("🔍 Auditoria (Admin)").run()
        assert not app_admin.exception, f"o portão de 2FA levantou exceção: {app_admin.exception}"

        subcabecalhos = [elemento.value for elemento in app_admin.subheader]
        assert any("Autenticação em dois fatores" in texto for texto in subcabecalhos), (
            subcabecalhos
        )
        legendas = [elemento.value for elemento in app_admin.caption]
        assert any("Segredo para digitação manual" in texto for texto in legendas), legendas
        # QR Code desenhado (PNG decodificado do backend → st.image).
        assert len(app_admin.image) >= 1, "o QR Code do 2FA não foi renderizado"
        # Nada da área de auditoria vazou para a tela bloqueada.
        assert not any(
            "Auditoria de Divergências" in elemento.value for elemento in app_admin.header
        ), [elemento.value for elemento in app_admin.header]
        assert not any("brusca" in elemento.label for elemento in app_admin.expander), [
            elemento.label for elemento in app_admin.expander
        ]
        # O campo do código de 6 dígitos está disponível para ativar o 2FA.
        campos = {campo.key for campo in app_admin.text_input}
        assert "2fa_codigo_setup_auditoria" in campos, campos


@teste
def test_apptest_aba_sugestoes_ia_da_auditoria():
    """AppTest: a Auditoria mantém as abas antigas e mostra 🤖 Sugestões da IA."""
    with app_test_logado(admin=True) as app:
        assert not app.exception, app.exception
        opcoes = app.radio(key="menu_principal").options
        app.radio(key="menu_principal").set_value(opcoes[1]).run()
        assert not app.exception, app.exception
        rotulos = [aba.label for aba in app.tabs]
        assert "🤖 Sugestões da IA" in rotulos, rotulos
        # A aba antiga e os botões manuais continuam presentes.
        assert "🔍 Auditoria de Divergências" in rotulos, rotulos
        cabecalhos = [elemento.value for elemento in app.header]
        assert any("Sugestões da IA" in texto for texto in cabecalhos), cabecalhos


def _verificar_tela_de_relatorios(app):
    """AppTest: os três relatórios renderizam com as regras novas (CFOP, 50%, lojas, CSS)."""
    markdowns = [elemento.value for elemento in app.markdown]
    html_relatorios = "\n".join(markdowns)

    # As três abas existem.
    rotulos = [aba.label for aba in app.tabs]
    for esperado in ("📈 Evolução de Preços", "🏪 Comparativo de Lojas", "💰 Impacto Financeiro"):
        assert esperado in rotulos, f"aba {esperado} não encontrada: {rotulos}"

    # As pivots (Evolução e Comparativo) e a tabela do Impacto foram desenhadas.
    assert html_relatorios.count('class="pvt"') >= 2, "as tabelas pivot não foram renderizadas"
    assert 'title="Última compra em' in html_relatorios, (
        "o tooltip com a data da última compra não apareceu no Preço Atual"
    )
    assert 'title="Compra anterior em' in html_relatorios, (
        "o tooltip com a data da compra anterior não apareceu no Preço Anterior"
    )
    # Regra 2 (variação >= 50%): linha vermelha + Status "Verificar Variação Brusca".
    assert "table.pvt tbody tr.aberr" in html_relatorios, "CSS das aberrações não foi aplicado"
    tabela_impacto_inicial = next(
        (texto for texto in markdowns if "Última compra em" in texto), ""
    )
    linhas_aberrantes = [
        produto
        for produto, aberrante in _linhas_da_tabela_impacto(tabela_impacto_inicial)
        if aberrante
    ]
    if linhas_aberrantes:
        assert "Verificar Variação Brusca" in tabela_impacto_inicial, (
            "o alerta 'Verificar Variação Brusca' não aparece nas linhas de aberração"
        )

    # Cabeçalho fixo + barra de rolagem horizontal sempre visível.
    assert "position: sticky" in html_relatorios, "o cabeçalho sticky não foi aplicado"
    assert "max-height: 74vh" in html_relatorios, "a caixa de rolagem da tabela não foi aplicada"
    assert "::-webkit-scrollbar" in html_relatorios, "a barra de rolagem não foi estilizada"

    # Coluna Variação (%): vermelho na queda e verde no aumento (classe usada nas linhas).
    assert "td.var-neg" in html_relatorios and "td.var-pos" in html_relatorios, (
        "as cores da coluna Variação (%) não foram definidas"
    )
    assert 'class="num var-neg tt"' in html_relatorios, "nenhuma queda saiu em vermelho"
    assert 'class="num var-pos tt"' in html_relatorios, "nenhum aumento saiu em verde"

    # Filtro de loja: "Minhas Lojas (Todas)" + as lojas cadastradas (3 CNPJs do grupo).
    # O AppTest expõe `options` já com o rótulo formatado (format_func) da tela.
    opcoes_loja = next(
        (
            list(seletor.options)
            for seletor in app.selectbox
            if any("Minhas Lojas (Todas)" in str(opcao) for opcao in seletor.options)
        ),
        [],
    )
    assert opcoes_loja, "o filtro de loja não oferece a opção 'Minhas Lojas (Todas)'"
    assert len(opcoes_loja) >= 4, f"esperava o grupo + as 3 lojas cadastradas: {opcoes_loja}"
    assert any("CNPJ" in str(opcao) for opcao in opcoes_loja), (
        f"o rótulo do grupo deveria citar os CNPJs: {opcoes_loja}"
    )

    # Comparativo: o filtro de lojas também traz "Minhas Lojas (Todas)" + todos os CNPJs.
    multiselects = [campo for campo in app.get("multiselect") if campo.key == "r1_lojas"]
    assert multiselects, "o filtro de lojas do comparativo não foi renderizado"
    opcoes_comparativo = list(multiselects[0].options)
    assert any("Minhas Lojas (Todas)" in str(opcao) for opcao in opcoes_comparativo), (
        opcoes_comparativo
    )
    assert len(opcoes_comparativo) >= 4, (
        f"esperava 'Minhas Lojas (Todas)' + as 3 lojas cadastradas: {opcoes_comparativo}"
    )
    assert any("sem compras importadas" in str(opcao) for opcao in opcoes_comparativo) or any(
        "(" in str(opcao) for opcao in opcoes_comparativo
    ), opcoes_comparativo

    # Impacto Financeiro: aumento em VERMELHO com sinal negativo, economia em VERDE com "+".
    assert 'class="metrica-impacto neg"' in html_relatorios, (
        "o cartão vermelho do Aumento de Custo não foi renderizado"
    )
    assert 'class="metrica-impacto pos"' in html_relatorios, (
        "o cartão verde da Economia não foi renderizado"
    )
    totais_impacto = (
        _api("/api/relatorio/impacto", {"loja": "Minhas Lojas (Todas)", "meses_consumo": 3}).get(
            "totais"
        )
        or {}
    )
    valor_aumento = float(totais_impacto.get("aumento") or 0.0)
    valor_economia = float(totais_impacto.get("economia") or 0.0)
    valor_liquido = float(totais_impacto.get("impacto_liquido") or 0.0)
    if valor_aumento > 0:
        assert '<div class="valor">-R$' in html_relatorios, (
            "o Aumento de Custo deveria aparecer com sinal negativo"
        )
    if valor_economia > 0:
        assert '<div class="valor">+R$' in html_relatorios, (
            "a Economia deveria aparecer com sinal positivo"
        )
    if valor_liquido:
        classe_liquido = "neg" if valor_liquido > 0 else "pos"
        assert f'class="metrica-impacto {classe_liquido}"' in html_relatorios, (
            "o Impacto Líquido deveria seguir a cor do seu sinal"
        )

    # Planilha ordenável (st.dataframe) do Impacto: ordenação por clique no cabeçalho.
    colunas_planilhas = [set(df.value.columns) for df in app.dataframe]
    assert any(
        "Variação (%)" in colunas and "Data da Última Compra" in colunas
        for colunas in colunas_planilhas
    ), "a planilha ordenável do Impacto não foi renderizada"

    # Ordenação: trocar para "Produto (A → Z)" reordena e mantém as aberrações no fim.
    app.selectbox(key="r3_ordem").set_value("🔤 Produto (A → Z)").run()
    assert not app.exception, f"a ordenação levantou exceção: {app.exception}"
    markdowns_ordenados = [elemento.value for elemento in app.markdown]
    tabela_impacto = next(
        (texto for texto in markdowns_ordenados if "Última compra em" in texto), ""
    )
    linhas_impacto = _linhas_da_tabela_impacto(tabela_impacto)
    assert linhas_impacto, "a tabela de Impacto não foi encontrada depois de reordenar"
    normais = [produto for produto, aberrante in linhas_impacto if not aberrante]
    assert normais == sorted(normais, key=str.upper), "a ordenação por produto não ficou A → Z"
    posicoes_aberr = [i for i, (_, aberrante) in enumerate(linhas_impacto) if aberrante]
    if posicoes_aberr:
        assert posicoes_aberr == list(range(posicoes_aberr[0], len(linhas_impacto))), (
            "as variações bruscas deveriam ficar no fim da tabela"
        )
    html_relatorios = "\n".join(markdowns_ordenados)

    # Nenhum item pendente na auditoria pode aparecer nas tabelas (rota de admin).
    aprovadas = set(
        _api("/api/auditados", token=app.session_state.get(CHAVE_TOKEN_SESSAO))
    )
    db = SessionLocal()
    try:
        mapa_regras, mapa_descricoes = bmain._mapas_regras_relatorio(db)
        pendentes = bmain._descricoes_pendentes_auditoria(db, mapa_regras, mapa_descricoes, aprovadas)
    finally:
        db.close()
    for chave in pendentes:
        descricao = chave.rsplit("_", 1)[-1].strip()
        if len(descricao) < 12:
            continue
        assert f">{descricao}</td>" not in html_relatorios, (
            f"produto pendente na auditoria apareceu na tabela: {descricao}"
        )

    # Nenhuma célula de preço zerada (R$ 0,00) nas tabelas dos relatórios.
    assert ">R$ 0,00</td>" not in html_relatorios, "relatório exibiu preço R$ 0,00"

    # A aba de Auditoria continua carregando (agora com a seção de pendências marcadas).
    app.radio(key="menu_principal").set_value("🔍 Auditoria (Admin)").run()
    assert not app.exception, f"a auditoria levantou exceção: {app.exception}"
    titulos = [elemento.value for elemento in app.title]
    assert any("Auditoria" in titulo for titulo in titulos), titulos


# ──────────────────────────────────────────────────────────────────────────────
# Inteligência de marcas (chave Produto + Marca e substituições no tooltip)
# ──────────────────────────────────────────────────────────────────────────────
# Três descrições do MESMO item, de marcas diferentes: no NCM elas compartilham as
# palavras do produto (PALHA/MANDIOCA/FINA) e só a marca varia.
DESCRICOES_MANDIOCA = [
    "PALHA DE MANDIOCA FINA YOKI 1KG",
    "PALHA DE MANDIOCA FINA MATHEUS 500G",
    "PALHA MANDIOCA FINA DECK 1KG",
]


@teste
def test_marca_extraida_entre_descricoes_do_mesmo_ncm():
    """A marca é a palavra que VARIA entre as descrições do mesmo NCM."""
    from backend import marcas as bmarcas

    genericos = bmarcas.termos_genericos(DESCRICOES_MANDIOCA)
    # "FINA" nem entra na disputa: é qualificador (QUALIFICADORES), não marca.
    assert {"PALHA", "MANDIOCA"} <= genericos, genericos
    assert "FINA" not in bmarcas.tokens(DESCRICOES_MANDIOCA[0]), DESCRICOES_MANDIOCA[0]
    extraidas = [bmarcas.extrair_marca(descricao, genericos) for descricao in DESCRICOES_MANDIOCA]
    assert extraidas == ["YOKI", "MATHEUS", "DECK"], extraidas

    # Unidade/embalagem (TOLETE, KG, G, CX, UN) nunca é marca; sem descrição própria
    # (só termos do produto) a marca fica NULL.
    assert bmarcas.extrair_marca("PALHA MANDIOCA FINA TOLETE 500G", genericos) is None
    assert bmarcas.extrair_marca("", genericos) is None
    # NCM com uma única descrição não tem variação: nada é genérico (marca não inferível).
    assert bmarcas.termos_genericos([DESCRICOES_MANDIOCA[0]]) == set()
    # A similaridade do PRODUTO tira o nome da marca da conta: é assim que YOKI e MATHEUS
    # são reconhecidos como o mesmo item (peso/embalagem também não atrapalham).
    assert (
        bmarcas.similaridade_produto(DESCRICOES_MANDIOCA[0], "YOKI", DESCRICOES_MANDIOCA[1], "MATHEUS")
        > 0.8
    )
    assert (
        bmarcas.similaridade_produto(DESCRICOES_MANDIOCA[1], "MATHEUS", DESCRICOES_MANDIOCA[2], "DECK")
        > 0.8
    )
    assert bmarcas.similaridade(DESCRICOES_MANDIOCA[0], DESCRICOES_MANDIOCA[2]) > 0.8
    assert (
        bmarcas.similaridade_produto(
            "ARROZ TIPO 1 PACOTE 5 KG", "CAMIL", DESCRICOES_MANDIOCA[0], "YOKI"
        )
        < 0.5
    )
    # "irmãs" (mesmo produto, outra marca) = família de substituição do relatório.
    marcas_do_teste = dict(zip(DESCRICOES_MANDIOCA, ["YOKI", "MATHEUS", "DECK"]))
    assert DESCRICOES_MANDIOCA[0] in bmarcas.descricoes_com_par(
        DESCRICOES_MANDIOCA, marcas_do_teste
    )
    # Hortifrúti: o que varia é a variedade, não a marca → nada de "marca".
    assert (
        bmarcas.descricoes_com_par(
            ["ALFACE AMERICANA", "ALFACE CRESPA HIDROPONICA"],
            {"ALFACE AMERICANA": "AMERICANA", "ALFACE CRESPA HIDROPONICA": "CRESPA"},
        )
        == set()
    )


@teste
def test_marcas_substitutas_nao_alteram_a_media_do_mercado():
    """Marcas do mesmo NCM alimentam SÓ o tooltip: a Média de Mercado segue exata."""
    from backend import marcas as bmarcas

    with tempfile.TemporaryDirectory() as diretorio:
        db, engine = _sessao_copia(diretorio)
        try:
            referencia = bmain._data_referencia_relatorios(db) or date(2026, 1, 15)
            eans = ("7891000100101", "7891000100102", "7891000100103")
            # Mesmo NCM, descrições parecidas (> 80%), EANs e preços diferentes.
            _inserir_nota_teste(
                db,
                [
                    (eans[0], DESCRICOES_MANDIOCA[0], 10.0),
                    (eans[1], DESCRICOES_MANDIOCA[1], 15.0),
                    (eans[2], DESCRICOES_MANDIOCA[2], 20.0),
                ],
                data_nota=referencia,
                ncm="19059090",
            )
            itens, _ = _carregar(db)
            antes = {
                item["descricao"]: (item["chave"], item["valor"])
                for item in itens
                if item["descricao"] in DESCRICOES_MANDIOCA
            }
            assert len(antes) == 3, antes

            # A extração da marca roda sobre o corpus do banco (importador faz o mesmo).
            resumo = bmarcas.recalcular_marcas(db)
            assert resumo["com_marca"] >= 3, resumo
            gravadas = {
                item.descricao_produto: item.marca
                for item in db.query(ItemNota).filter(ItemNota.ean.in_(eans)).all()
            }
            assert gravadas == {
                DESCRICOES_MANDIOCA[0]: "YOKI",
                DESCRICOES_MANDIOCA[1]: "MATHEUS",
                DESCRICOES_MANDIOCA[2]: "DECK",
            }, gravadas

            # A MÉDIA DE MERCADO principal continua a média exata do EAN/descrição: os
            # valores dos itens não mudam (as outras marcas NÃO entram na conta).
            depois, _ = _carregar(db)
            assert {
                item["descricao"]: item["valor"]
                for item in depois
                if item["descricao"] in DESCRICOES_MANDIOCA
            } == {descricao: valor for descricao, (_chave, valor) in antes.items()}

            for descricao, (_chave, valor) in antes.items():
                # Filtro por produto (o teto da consulta paralela é por relatório).
                dados = bmain.relatorio_comparativo(
                    lojas=None,
                    dias=360,
                    periodicidade="mensal",
                    apenas_divergentes=False,
                    produto=descricao,
                    limite=20000,
                    db=db,
                )
                assert len(dados["linhas"]) == 1, (descricao, dados["linhas"])
                linha = dados["linhas"][0]
                assert linha["produto"] == descricao
                assert linha["mercado_preco_medio"] == round(valor, 4), (descricao, linha)
                # Só o tooltip recebe as marcas substitutas (as outras duas do teste).
                substitutas = {marca["marca"] for marca in linha["mercado_marcas"]}
                outras = {"YOKI", "MATHEUS", "DECK"} - {gravadas[descricao]}
                assert outras <= substitutas, (descricao, substitutas)
                for marca in linha["mercado_marcas"]:
                    assert marca["similaridade"] > bmain.SIMILARIDADE_MINIMA_MARCA, marca
                    assert marca["preco_medio"] in {10.0, 15.0, 20.0}, marca
        finally:
            db.close()
            engine.dispose()


@teste
def test_apptest_tooltip_de_marcas_na_coluna_mercado():
    """AppTest: a coluna Mercado do Comparativo traz as marcas substitutas no tooltip."""
    dados = _api(
        "/api/relatorio/comparativo",
        params={"dias": 30, "periodicidade": "mensal", "limite": 20000},
    )
    com_marcas = [
        linha["produto"]
        for linha in dados.get("linhas") or []
        if linha.get("mercado_marcas") and linha.get("mercado_preco_medio") is not None
    ]
    assert com_marcas, "nenhum produto com marca substituta na janela padrão (30 dias)"

    with app_test_logado(admin=True) as app:
        opcoes = list(app.selectbox(key="r1_produto").options)
        alvo = next((produto for produto in com_marcas if produto in opcoes), None)
        assert alvo, (com_marcas[:5], opcoes[:5])
        app.selectbox(key="r1_produto").set_value(alvo).run()
        assert not app.exception, f"o comparativo levantou exceção: {app.exception}"

        html_relatorios = "\n".join(elemento.value for elemento in app.markdown)
        assert "Outras marcas do mesmo NCM" in html_relatorios, html_relatorios[-1500:]
        # O tooltip é só informativo: a célula do Mercado continua com o preço em R$.
        linha_produto = next(
            linha for linha in dados["linhas"] if linha["produto"] == alvo
        )
        valor = f"{float(linha_produto['mercado_preco_medio']):,.2f}"
        valor = valor.replace(",", "X").replace(".", ",").replace("X", ".")
        assert f"R$ {valor}" in html_relatorios, (valor, html_relatorios[-800:])


# ──────────────────────────────────────────────────────────────────────────────
# Regra 14: token da API do WhatsApp (admin) — o valor nunca sai do servidor
# ──────────────────────────────────────────────────────────────────────────────
def _token_whatsapp_salvo():
    """Valor atual do token do WhatsApp no banco (usado para restaurar no fim do teste)."""
    db = SessionLocal()
    try:
        registro = (
            db.query(ConfiguracaoSistema)
            .filter(ConfiguracaoSistema.chave == "whatsapp_token")
            .first()
        )
        return registro.valor if registro else None
    finally:
        db.close()


def _restaurar_token_whatsapp(valor):
    """Devolve o token do WhatsApp ao valor anterior (None = sem token)."""
    db = SessionLocal()
    try:
        registro = (
            db.query(ConfiguracaoSistema)
            .filter(ConfiguracaoSistema.chave == "whatsapp_token")
            .first()
        )
        if valor is None:
            if registro is not None:
                db.delete(registro)
        else:
            if registro is None:
                registro = ConfiguracaoSistema(chave="whatsapp_token")
                db.add(registro)
            registro.valor = valor
        db.commit()
    finally:
        db.close()


@teste
def test_api_admin_configuracoes_token_whatsapp():
    """Token da API do WhatsApp: só admin com 2FA salva, e o valor volta mascarado."""
    token_anterior = _token_whatsapp_salvo()
    usuario, token_comum, _, _ = _registrar_usuario_teste("Conta Comum Config")
    try:
        comum = requests.get(
            BASE_API + "/api/admin/configuracoes",
            headers=_cabecalho_admin(token_comum),
            timeout=30,
        )
        assert comum.status_code == 403, comum.text[:300]

        with conta_admin_teste() as (admin, token_admin):
            # Sessão do admin sem o código do autenticador: 401 (2FA obrigatório).
            sem_2fa = requests.get(
                BASE_API + "/api/admin/configuracoes",
                headers={"Authorization": f"Bearer {token_admin}"},
                timeout=30,
            )
            assert sem_2fa.status_code == 401, sem_2fa.text[:300]

            salvo = _post_api(
                "/api/admin/configuracoes",
                {"whatsapp_token": "EAAG-token-de-teste-1234"},
                token=token_admin,
            )
            assert salvo.status_code == 200, salvo.text[:300]
            corpo = salvo.json()
            assert corpo["whatsapp_token_configurado"] is True, corpo
            assert corpo["whatsapp_token_mascarado"] == "••••1234", corpo
            assert corpo["atualizado_por"] == admin["email"], corpo
            # O token completo NÃO volta para a tela.
            assert "EAAG" not in str(corpo), corpo

            lido = requests.get(
                BASE_API + "/api/admin/configuracoes",
                headers=_cabecalho_admin(token_admin),
                timeout=30,
            )
            assert lido.status_code == 200, lido.text[:300]
            assert lido.json()["whatsapp_token_mascarado"] == "••••1234", lido.json()

            # O cliente vê que o envio está habilitado — sem ver o token.
            usuario_alerta, token_alerta, _, _ = _registrar_usuario_teste(
                "Conta Alerta Token"
            )
            try:
                config = requests.get(
                    BASE_API + "/api/alertas/configurar",
                    headers={"Authorization": f"Bearer {token_alerta}"},
                    timeout=30,
                )
                assert config.status_code == 200, config.text[:300]
                corpo_config = config.json()
                assert corpo_config["whatsapp_configurado"] is True, corpo_config
                assert "eaag" not in str(corpo_config).lower(), corpo_config
            finally:
                _apagar_usuario_teste(usuario_alerta["id"])

            # Campo vazio remove o token (envio real desligado).
            removido = _post_api(
                "/api/admin/configuracoes", {"whatsapp_token": ""}, token=token_admin
            )
            assert removido.status_code == 200, removido.text[:300]
            assert removido.json()["whatsapp_token_configurado"] is False, removido.json()
    finally:
        _restaurar_token_whatsapp(token_anterior)
        _apagar_usuario_teste(usuario["id"])


@teste
def test_api_alertas_configurar_e_gerar_mensagens():
    """Motor de alertas: configuração por conta + simulador monta aumentos/divergências."""
    usuario, token, _, _ = _registrar_usuario_teste("Conta Alertas")
    cabecalho = {"Authorization": f"Bearer {token}"}
    try:
        _criar_loja_para_alertas(usuario["id"])

        # Conta nova: padrões (30 dias, 5%) e nenhuma configuração gravada.
        inicial = requests.get(
            BASE_API + "/api/alertas/configurar", headers=cabecalho, timeout=30
        )
        assert inicial.status_code == 200, inicial.text[:300]
        config = inicial.json()
        assert config["configuracao"]["ativo"] is True, config
        assert config["configuracao"]["janela_dias"] == 30, config
        assert config["configuracao"]["atualizado_em"] is None, config
        assert config["todas_as_lojas"] is True, config
        assert [loja["cnpj"] for loja in config["lojas"]] == [CNPJ_ALERTA], config["lojas"]

        # Telefone inválido é recusado e nada é gravado.
        invalido = _post_api(
            "/api/alertas/configurar",
            {
                "ativo": True,
                "telefone": "123",
                "variacao_minima": 5,
                "janela_dias": 30,
                "incluir_aumentos": True,
                "incluir_divergencias": True,
                "lojas": [],
            },
            token=token,
        )
        assert invalido.status_code == 400, invalido.text[:300]

        # Configuração válida: o telefone ganha o DDI e as lojas são validadas.
        salvo = _post_api(
            "/api/alertas/configurar",
            {
                "ativo": True,
                "telefone": "(11) 98888-7777",
                "variacao_minima": 10,
                "janela_dias": 30,
                "incluir_aumentos": True,
                "incluir_divergencias": True,
                "lojas": [CNPJ_ALERTA],
            },
            token=token,
        )
        assert salvo.status_code == 200, salvo.text[:300]
        corpo = salvo.json()["configuracao"]
        assert corpo["telefone"] == "5511988887777", corpo
        assert corpo["variacao_minima"] == 10.0, corpo
        assert corpo["lojas"] == [CNPJ_ALERTA], corpo
        assert corpo["atualizado_em"], corpo
        assert salvo.json()["telefone_exibicao"] == "+55 11 98888-7777", salvo.json()

        # Loja que não é da conta não pode entrar no alerta.
        alheia = _post_api(
            "/api/alertas/configurar",
            {
                "ativo": True,
                "telefone": "11988887777",
                "variacao_minima": 5,
                "janela_dias": 30,
                "incluir_aumentos": True,
                "incluir_divergencias": True,
                "lojas": ["00000000000100"],
            },
            token=token,
        )
        assert alheia.status_code == 403, alheia.text[:300]

        # Simulador: +18% vira aumento e +60% vira divergência (não é aumento).
        simulacao = _post_api(
            "/api/alertas/gerar-mensagens",
            {
                "ativo": True,
                "telefone": "(11) 98888-7777",
                "variacao_minima": 10,
                "janela_dias": 30,
                "incluir_aumentos": True,
                "incluir_divergencias": True,
                "lojas": [CNPJ_ALERTA],
            },
            token=token,
        )
        assert simulacao.status_code == 200, simulacao.text[:500]
        dados = simulacao.json()
        assert dados["simulado"] is True and dados["enviado"] is False, dados
        resumo = dados["resumo"]
        # Regra nova: TODA variação entra (a de 60% deixou de ser "divergência" e
        # passou a ser um aumento com o selo "conferir").
        assert resumo["aumentos"] == 2, resumo
        assert resumo["quedas"] == 0, resumo
        assert resumo["conferir"] == 1, resumo
        assert resumo["lojas"] == ["Loja Alerta Teste"], resumo
        texto = "\n".join(mensagem["texto"] for mensagem in dados["mensagens"])
        assert "Alerta de Preços" in texto, texto
        assert PRODUTOS_ALERTA[0] in texto, texto
        assert PRODUTOS_ALERTA[1] in texto, texto
        assert "+18,0%" in texto, texto
        assert "R$ 5,00" in texto and "R$ 5,90" in texto, texto
        assert "conferir" in texto and "+60,0%" in texto, texto
        # Cada parte cabe no limite do WhatsApp.
        assert all(
            mensagem["caracteres"] <= 4096 for mensagem in dados["mensagens"]
        ), [(m["titulo"], m["caracteres"]) for m in dados["mensagens"]]

        # Simular NÃO altera a configuração salva.
        atual = requests.get(
            BASE_API + "/api/alertas/configurar", headers=cabecalho, timeout=30
        ).json()["configuracao"]
        assert atual["variacao_minima"] == 10.0, atual

        # Desligar `incluir_divergencias` não esconde mais a variação de 60%: o selo
        # "conferir" é só um aviso (o corte virou filtro inexistente para o cliente).
        sem_divergencias = _post_api(
            "/api/alertas/gerar-mensagens",
            {
                "ativo": True,
                "telefone": "11988887777",
                "variacao_minima": 10,
                "janela_dias": 30,
                "incluir_aumentos": True,
                "incluir_divergencias": False,
                "lojas": [CNPJ_ALERTA],
            },
            token=token,
        )
        assert sem_divergencias.status_code == 200, sem_divergencias.text[:300]
        texto_sem = "\n".join(
            mensagem["texto"] for mensagem in sem_divergencias.json()["mensagens"]
        )
        assert PRODUTOS_ALERTA[0] in texto_sem, texto_sem
        assert PRODUTOS_ALERTA[1] in texto_sem, texto_sem
        assert sem_divergencias.json()["resumo"]["conferir"] == 1, (
            sem_divergencias.json()["resumo"]
        )
    finally:
        _apagar_loja_para_alertas()
        _apagar_usuario_teste(usuario["id"])


@teste
def test_api_alertas_canal_email_e_ambos():
    """Preferência de canal: e-mail sem telefone, 'ambos' exigindo os dois destinos."""
    usuario, token, _, _ = _registrar_usuario_teste("Conta Alertas Email")
    try:
        _criar_loja_para_alertas(usuario["id"])

        sem_email = _post_api(
            "/api/alertas/configurar",
            {
                "ativo": True,
                "telefone": "",
                "email": "",
                "canal": "email",
                "variacao_minima": 5,
                "janela_dias": 30,
                "incluir_aumentos": True,
                "incluir_divergencias": True,
                "lojas": [],
            },
            token=token,
        )
        assert sem_email.status_code == 400, sem_email.text[:300]

        salvo = _post_api(
            "/api/alertas/configurar",
            {
                "ativo": True,
                "telefone": "",
                "email": "Compras@Empresa.COM",
                "email_secundario": "Copia@Empresa.COM",
                "canal": "email",
                "variacao_minima": 7.5,
                "janela_dias": 60,
                "incluir_aumentos": True,
                "incluir_divergencias": True,
                "lojas": [],
            },
            token=token,
        )
        assert salvo.status_code == 200, salvo.text[:300]
        config = salvo.json()["configuracao"]
        assert config["canal"] == "email", config
        assert config["email"] == "compras@empresa.com", config
        assert config["email_secundario"] == "copia@empresa.com", config
        assert config["telefone"] == "", config
        assert salvo.json()["canais"] == ["email"], salvo.json()

        simulado = _post_api(
            "/api/alertas/gerar-mensagens",
            {
                "ativo": True,
                "telefone": "",
                "email": "compras@empresa.com",
                "email_secundario": "copia@empresa.com",
                "canal": "email",
                "variacao_minima": 7.5,
                "janela_dias": 60,
                "incluir_aumentos": True,
                "incluir_divergencias": True,
                "lojas": [],
            },
            token=token,
        )
        assert simulado.status_code == 200, simulado.text[:500]
        corpo = simulado.json()
        assert corpo["canal"] == "email", corpo
        assert corpo["email"] == "compras@empresa.com", corpo
        assert corpo["email_secundario"] == "copia@empresa.com", corpo
        assert corpo["canais"] == ["email"], corpo
        assert corpo["enviado"] is False and corpo["simulado"] is True, corpo

        # "Ambos" também aceita os dois destinos e devolve os dois canais.
        ambos = _post_api(
            "/api/alertas/configurar",
            {
                "ativo": True,
                "telefone": "(11) 97777-6666",
                "email": "alertas@empresa.com",
                "canal": "ambos",
                "variacao_minima": 5,
                "janela_dias": 30,
                "incluir_aumentos": True,
                "incluir_divergencias": False,
                "lojas": [],
            },
            token=token,
        )
        assert ambos.status_code == 200, ambos.text[:300]
        assert ambos.json()["configuracao"]["canal"] == "ambos", ambos.json()
        assert set(ambos.json()["canais"]) == {"whatsapp", "email"}, ambos.json()

        # E-mail secundário, quando informado, também é validado.
        secundario_invalido = _post_api(
            "/api/alertas/configurar",
            {
                "ativo": True,
                "telefone": "",
                "email": "alertas@empresa.com",
                "email_secundario": "sem-arroba",
                "canal": "email",
                "variacao_minima": 5,
                "janela_dias": 30,
                "incluir_aumentos": True,
                "incluir_divergencias": True,
                "lojas": [],
            },
            token=token,
        )
        assert secundario_invalido.status_code == 400, secundario_invalido.text[:300]

        # Canal desconhecido é recusado (não grava uma preferência ambígua).
        invalido = _post_api(
            "/api/alertas/configurar",
            {
                "ativo": True,
                "telefone": "11977776666",
                "email": "alertas@empresa.com",
                "canal": "telegram",
                "variacao_minima": 5,
                "janela_dias": 30,
                "incluir_aumentos": True,
                "incluir_divergencias": True,
                "lojas": [],
            },
            token=token,
        )
        assert invalido.status_code == 400, invalido.text[:300]
    finally:
        _apagar_loja_para_alertas()
        _apagar_usuario_teste(usuario["id"])


@teste
def test_apptest_alertas_e_botao_excluir_certificado():
    """AppTest: aba 🔔 Configurar Alertas (form + simulação) e "Excluir Certificado"."""
    with app_test_logado() as app:
        app.radio(key="menu_principal").set_value("🔔 Configurar Alertas").run()
        assert not app.exception, f"a aba de alertas levantou exceção: {app.exception}"

        titulos = [elemento.value for elemento in app.title]
        assert any("Configurar Alertas" in titulo for titulo in titulos), titulos
        informativos = "\n".join(elemento.value for elemento in app.info)
        assert "Quando chega" in informativos, informativos
        assert "06:00" in informativos, informativos
        campos = {campo.key for campo in app.text_input}
        assert {"alerta_email", "alerta_email_secundario"} <= campos, campos
        assert "alerta_telefone" not in campos, campos
        # Tela simplificada (decisão do cliente): só liga/desliga e e-mails. Canal,
        # variação mínima, janela, checkboxes e seleção de lojas saíram do formulário —
        # os valores já gravados continuam indo para o backend ao salvar.
        assert all(campo.key != "alerta_canal" for campo in app.radio), [
            campo.key for campo in app.radio
        ]
        assert not (
            {"alerta_aumentos", "alerta_divergencias"}
            & {campo.key for campo in app.checkbox}
        ), [campo.key for campo in app.checkbox]
        assert all(campo.key != "alerta_janela" for campo in app.selectbox), [
            campo.key for campo in app.selectbox
        ]
        assert all(campo.key != "alerta_lojas" for campo in app.multiselect), [
            campo.key for campo in app.multiselect
        ]
        assert any(campo.key == "alerta_ativo" for campo in app.toggle), [
            campo.key for campo in app.toggle
        ]

        rotulos = [botao.label for botao in app.button]
        assert any("Salvar preferências" in rotulo for rotulo in rotulos), rotulos
        assert any("Gerar prévia" in rotulo for rotulo in rotulos), rotulos
        assert any("Enviar agora" in rotulo for rotulo in rotulos), rotulos
        # Tipos do relatório automático: os três toggles ficam no formulário.
        chaves_toggle = {campo.key for campo in app.toggle}
        assert {
            "alerta_ativo",
            "alerta_diario",
            "alerta_semanal",
            "alerta_mensal",
        } <= chaves_toggle, chaves_toggle

        # A prévia roda de verdade contra a API e mostra as tabelas do relatório.
        gerar = next(
            botao for botao in app.button if "Gerar prévia" in botao.label
        )
        gerar.click().run()
        assert not app.exception, f"a prévia levantou exceção: {app.exception}"
        subtitulos = "\n".join(elemento.value for elemento in app.subheader)
        assert "Prévia e envio" in subtitulos, subtitulos
        assert "Relatório Diário de Compras" in subtitulos, subtitulos
        assert app.dataframe, "a prévia não mostrou nenhuma tabela"
        legendas = "\n".join(elemento.value for elemento in app.caption)
        assert "WhatsApp" not in legendas, legendas[:800]

        # Salvar mantém a prévia na tela (ela fica na sessão) e mostra o aviso.
        app.text_input(key="alerta_email").set_value("alertas@empresa.com")
        app.text_input(key="alerta_email_secundario").set_value("copia@empresa.com")
        salvar = next(
            botao for botao in app.button if "Salvar preferências" in botao.label
        )
        salvar.click().run()
        assert not app.exception, f"o salvamento levantou exceção: {app.exception}"
        avisos = "\n".join(elemento.value for elemento in app.success)
        assert "Preferências salvas com sucesso!" in avisos, avisos
        assert app.dataframe, "a prévia sumiu depois de salvar"

        # "Limpar prévia" descarta a prévia sem tocar nas preferências salvas.
        limpar = next(
            botao for botao in app.button if "Limpar prévia" in botao.label
        )
        limpar.click().run()
        assert not app.exception, f"limpar a prévia levantou exceção: {app.exception}"
        assert not app.dataframe, "a prévia continuou na tela depois de limpar"

        # Minhas Lojas: TODOS os cartões têm o botão de excluir certificado; o botão só
        # habilita depois da confirmação e o certificado só existe no Moça Bar.
        app.radio(key="menu_principal").set_value("🏬 Minhas Lojas").run()
        assert not app.exception, f"Minhas Lojas levantou exceção: {app.exception}"
        botoes_excluir = [
            botao for botao in app.button if "Excluir Certificado" in botao.label
        ]
        assert botoes_excluir, [botao.label for botao in app.button]
        # Conta comum: nenhum botão operacional da ACBr aparece no cartão.
        rotulos = [botao.label for botao in app.button]
        assert not any("Forçar Download de XMLs" in rotulo for rotulo in rotulos), rotulos
        assert not any("Consultar Status ACBr" in rotulo for rotulo in rotulos), rotulos
        confirmacoes = [
            campo
            for campo in app.checkbox
            if str(campo.key).startswith("confirmar_excluir_cert_")
        ]
        assert len(confirmacoes) == len(botoes_excluir), (
            len(confirmacoes),
            len(botoes_excluir),
        )
        assert all(campo.value is False for campo in confirmacoes), (
            "a confirmação não pode vir marcada por padrão"
        )
        # Sem a confirmação (ou sem certificado) o botão fica desabilitado.
        assert all(
            getattr(botao.proto, "disabled", False) for botao in botoes_excluir
        ), [getattr(botao.proto, "disabled", None) for botao in botoes_excluir]

        # Regressão da limpeza pós-cadastro: preenche os três campos e marca uma
        # confirmação; em seguida aplica os mesmos flags que o sucesso do cadastro usa.
        # O CNPJ inválido faz o submit parar antes de chamar a API (nenhum dado é gravado).
        def uploader_certificado():
            candidatos = [
                campo
                for campo in app.get("file_uploader")
                if str(campo.key).startswith("uploader_certificado_")
            ]
            assert len(candidatos) == 1, [campo.key for campo in candidatos]
            return candidatos[0]

        app.text_input(key="nova_loja_cnpj").set_value("123")
        app.text_input(key="nova_loja_senha_certificado").set_value("senha-teste")
        uploader = uploader_certificado()
        chave_uploader_antes = uploader.key
        uploader.upload("certificado-teste.pfx", b"conteudo", "application/x-pkcs12")
        confirmacao_habilitada = next(
            campo
            for campo in confirmacoes
            if not getattr(campo.proto, "disabled", False)
        )
        chaves_confirmacao_antes = {str(campo.key) for campo in confirmacoes}
        confirmacao_habilitada.check()
        # O cadastro vive em `st.form`: o único botão que envia é o submit.
        enviar_loja = next(
            botao for botao in app.button if "Salvar e vincular loja" in botao.label
        )
        assert getattr(enviar_loja.proto, "is_form_submitter", False), (
            "o cadastro precisa ser o submit de um st.form (sem run por campo)",
            enviar_loja.proto,
        )
        enviar_loja.click().run()
        assert not app.exception, f"o submit do formulário levantou exceção: {app.exception}"
        assert app.radio(key="menu_principal").value == "🏬 Minhas Lojas", (
            "o cadastro não pode trocar a página",
            app.radio(key="menu_principal").value,
        )
        assert app.text_input(key="nova_loja_cnpj").value == "123"
        assert app.text_input(key="nova_loja_senha_certificado").value == "senha-teste"
        uploader = uploader_certificado()
        assert uploader.key == chave_uploader_antes
        assert uploader.value is not None

        # Simula o pós-sucesso: os flags são aplicados e o próximo run limpa tudo.
        app.session_state["_limpar_form_minhas_lojas"] = True
        app.session_state["_limpar_confirmacao_exclusao_cert"] = True
        app.run()
        assert not app.exception, f"a limpeza levantou exceção: {app.exception}"
        assert app.text_input(key="nova_loja_cnpj").value == "", "CNPJ não foi limpo"
        assert app.text_input(key="nova_loja_senha_certificado").value == "", (
            "a senha do certificado não foi limpa"
        )
        uploader_limpo = uploader_certificado()
        assert uploader_limpo.key != chave_uploader_antes, (
            "a chave do uploader precisa mudar para o widget ser recriado vazio",
            uploader_limpo.key,
        )
        assert uploader_limpo.value is None, "o upload do certificado não foi limpo"
        confirmacoes_limpas = [
            campo
            for campo in app.checkbox
            if str(campo.key).startswith("confirmar_excluir_cert_")
        ]
        chaves_confirmacao_depois = {str(campo.key) for campo in confirmacoes_limpas}
        assert chaves_confirmacao_depois.isdisjoint(chaves_confirmacao_antes), (
            "a versão do checkbox precisa mudar para o widget ser recriado vazio",
            chaves_confirmacao_antes,
            chaves_confirmacao_depois,
        )
        assert all(campo.value is False for campo in confirmacoes_limpas), (
            "a confirmação de exclusão continuou marcada",
            [(campo.key, campo.value) for campo in confirmacoes_limpas],
        )

    # Admin: a aba ⚙️ Configurações traz o campo do token da API do WhatsApp.
    with app_test_logado(admin=True) as app_admin:
        app_admin.radio(key="menu_principal").set_value("🔍 Auditoria (Admin)").run()
        assert not app_admin.exception, app_admin.exception
        rotulos_abas = [aba.label for aba in app_admin.tabs]
        assert any("Configurações" in rotulo for rotulo in rotulos_abas), rotulos_abas
        campos_admin = {campo.key for campo in app_admin.text_input}
        assert "admin_whatsapp_token" in campos_admin, campos_admin
        # Em "Minhas Lojas", só o administrador vê as ações técnicas da ACBr.
        app_admin.radio(key="menu_principal").set_value("🏬 Minhas Lojas").run()
        assert not app_admin.exception, app_admin.exception
        rotulos_admin = [botao.label for botao in app_admin.button]
        assert any(
            "Forçar Download de XMLs" in rotulo for rotulo in rotulos_admin
        ), rotulos_admin
        assert any(
            "Consultar Status ACBr" in rotulo for rotulo in rotulos_admin
        ), rotulos_admin
        assert any(
            "Excluir Certificado" in rotulo for rotulo in rotulos_admin
        ), rotulos_admin


@teste
def test_apptest_conta_sem_lojas_abre_minhas_lojas():
    """Conta recém-criada (zero lojas): o aviso de Relatórios leva ao cadastro.

    É o estado da conta não-admin no primeiro acesso (caso Maira): sem esse caminho
    a tela fica em "Relatórios" e o cadastro de CNPJ nunca aparece. Depois do
    clique, `ir_para` + `st.rerun` precisam deixar a página em "Minhas Lojas" — a
    URL não pode desfazer a navegação explícita.
    """
    from streamlit.testing.v1 import AppTest

    usuario, token, _, _ = _registrar_usuario_teste("Conta Sem Lojas")
    try:
        app = AppTest.from_file(str(RAIZ / "frontend" / "app.py"), default_timeout=240)
        app.session_state[CHAVE_USUARIO_SESSAO] = usuario
        app.session_state[CHAVE_TOKEN_SESSAO] = token
        app.run()
        assert not app.exception, app.exception
        titulos = [elemento.value for elemento in app.title]
        assert any("Relatórios de Preços" in titulo for titulo in titulos), titulos
        avisos = "\n".join(elemento.value for elemento in app.warning)
        assert "nenhuma loja vinculada" in avisos, avisos

        app.button(key="btn_ir_minhas_lojas").click().run()
        assert not app.exception, app.exception
        assert app.radio(key="menu_principal").value == "🏬 Minhas Lojas", (
            app.radio(key="menu_principal").value,
        )
        assert app.session_state.get("pagina_atual") == "🏬 Minhas Lojas", (
            app.session_state.get("pagina_atual"),
        )
        titulos = [elemento.value for elemento in app.title]
        assert any("Minhas Lojas" in titulo for titulo in titulos), titulos
        # O formulário nasce limpo e pronto para o primeiro CNPJ.
        assert app.text_input(key="nova_loja_cnpj").value == ""
    finally:
        _apagar_usuario_teste(usuario["id"])


@teste
def test_apptest_cadastro_loja_formulario_e_endereco_fiscal():
    """AppTest: o cadastro é um `st.form` e o endereço do expander vai para a ACBr.

    Regressão do "pula para Relatórios no primeiro clique": nenhum widget do
    cadastro (upload do certificado inclusive) pode trocar a página — só o submit
    chama a API, e a tela continua em "Minhas Lojas" com `Status` + JSON crus.

    O endereço fiscal digitado à mão é o fallback do 429 da BrasilAPI: ele precisa
    chegar no `data` de `POST /api/acbr/empresas` (o backend usa esse valor antes do
    endereço gravado e do retorno da Receita).
    """
    from unittest.mock import patch

    cnpj = "47627864000100"

    class _RespostaCadastro:
        """Resposta mínima de `requests`, com o JSON que a tela imprime."""

        status_code = 200
        text = '{"sucesso": true}'
        headers = {}

        def __init__(self, corpo):
            self._corpo = corpo

        def json(self):
            return self._corpo

    empresa_json = {
        "id": 424242,
        "cnpj": cnpj,
        "nome_loja": "Loja Teste",
        "razao_social": "Loja Teste Ltda",
    }
    chamadas = []

    def _post_falso(url, data=None, files=None, headers=None, timeout=None):
        chamadas.append({"url": url, "data": dict(data or {})})
        if url.endswith("/api/empresas/cadastrar"):
            return _RespostaCadastro(
                {
                    "sucesso": True,
                    "mensagem": "Empresa cadastrada e vinculada à sua conta!",
                    "empresa": empresa_json,
                    "fonte_dados": "brasilapi",
                    "aviso": None,
                }
            )
        return _RespostaCadastro(
            {
                "sucesso": True,
                "mensagem": "Certificado enviado! Iniciando download e importação...",
                "empresa": empresa_json,
            }
        )

    # O campo CNPJ ficou fora do `st.form` e dispara a consulta da Receita ao mudar:
    # o GET é interceptado para o teste continuar sem internet e sem sobrescrever o
    # endereço digitado à mão (o cenário que este teste cobre).
    _get_real = requests.get

    def _get_falso(url, *args, **kwargs):
        if str(url).endswith("/api/empresas/consultar-cnpj"):
            return _RespostaCadastro(
                {
                    "cnpj": cnpj,
                    "encontrado": False,
                    "razao_social": "",
                    "nome_loja": "",
                    "endereco": {},
                    "fonte": "indisponivel",
                    "aviso": "Receita indisponível no teste.",
                    "da_conta": False,
                    "de_outra_conta": False,
                }
            )
        return _get_real(url, *args, **kwargs)

    with app_test_logado() as app:
        app.radio(key="menu_principal").set_value("🏬 Minhas Lojas").run()
        assert not app.exception, app.exception

        campos = {campo.key for campo in app.text_input}
        assert {
            "nova_loja_cep",
            "nova_loja_logradouro",
            "nova_loja_numero",
            "nova_loja_bairro",
            "nova_loja_municipio",
            "nova_loja_uf",
        } <= campos, campos
        # O código IBGE saiu da tela: vem da Receita/CEP e fica só na sessão.
        assert "nova_loja_codigo_municipio" not in campos, campos

        enviar = next(
            botao for botao in app.button if "Salvar e vincular loja" in botao.label
        )
        assert getattr(enviar.proto, "is_form_submitter", False), (
            "o cadastro precisa ser o submit de um st.form",
            enviar.proto,
        )

        # Preenche tudo (upload incluso) sem nenhuma chamada sair para a API.
        app.text_input(key="nova_loja_cnpj").set_value(cnpj)
        app.text_input(key="nova_loja_senha_certificado").set_value("senha-teste")
        uploader = next(
            campo
            for campo in app.get("file_uploader")
            if str(campo.key).startswith("uploader_certificado_")
        )
        uploader.upload("certificado-teste.pfx", b"conteudo", "application/x-pkcs12")
        app.text_input(key="nova_loja_cep").set_value("06708-415")
        app.text_input(key="nova_loja_logradouro").set_value("RUA JOSE FELIX DE OLIVEIRA")
        app.text_input(key="nova_loja_numero").set_value("991")
        app.text_input(key="nova_loja_bairro").set_value("VILA SANTO ANTONIO")
        app.text_input(key="nova_loja_municipio").set_value("COTIA")
        app.text_input(key="nova_loja_uf").set_value("sp")
        # Sem widget de IBGE: o valor vem da consulta do CNPJ/CEP para a sessão.
        app.session_state["nova_loja_codigo_municipio"] = "3513009"
        assert not chamadas, "nenhuma chamada pode sair antes do submit"

        with patch("requests.get", side_effect=_get_falso), patch(
            "requests.post", side_effect=_post_falso
        ):
            enviar.click().run()
        assert not app.exception, app.exception

        # A tela NÃO trocou de página: continua no cadastro, com a resposta na tela.
        assert app.radio(key="menu_principal").value == "🏬 Minhas Lojas", (
            app.radio(key="menu_principal").value,
        )
        assert app.session_state.get("pagina_atual") == "🏬 Minhas Lojas", (
            app.session_state.get("pagina_atual"),
        )
        titulos = [elemento.value for elemento in app.title]
        assert any("Minhas Lojas" in titulo for titulo in titulos), titulos
        assert not any("Relatórios de Preços" in titulo for titulo in titulos), titulos
        sucessos = "\n".join(elemento.value for elemento in app.success)
        # Mensagem do vínculo (CNPJ + loja): ela fecha o fluxo de vincular o CNPJ.
        assert "vinculado com sucesso" in sucessos, sucessos
        assert "Loja Teste" in sucessos, sucessos

        urls = [chamada["url"] for chamada in chamadas]
        assert urls == [
            f"{BASE_API}/api/empresas/cadastrar",
            f"{BASE_API}/api/acbr/empresas",
        ], urls
        dados_acbr = chamadas[1]["data"]
        assert dados_acbr["cnpj"] == cnpj, dados_acbr
        assert dados_acbr["cep"] == "06708415", dados_acbr
        assert dados_acbr["logradouro"] == "RUA JOSE FELIX DE OLIVEIRA", dados_acbr
        assert dados_acbr["numero"] == "991", dados_acbr
        assert dados_acbr["bairro"] == "VILA SANTO ANTONIO", dados_acbr
        assert dados_acbr["cidade"] == "COTIA", dados_acbr
        assert dados_acbr["uf"] == "SP", dados_acbr
        assert dados_acbr["codigo_municipio"] == "3513009", dados_acbr


class _RespostaJson:
    """Resposta HTTP mínima usada pelos dublês de `requests.get` nos AppTests."""

    def __init__(self, status_code, corpo):
        self.status_code = status_code
        self._corpo = corpo
        self.text = json.dumps(corpo, ensure_ascii=False)

    def json(self):
        return self._corpo


@teste
def test_apptest_minhas_lojas_sem_botoes_de_diagnostico():
    """Tela "Minhas Lojas" sem "Testar Conexão"/"Atualizar Lista" e sem URL/JSON crus.

    Os botões de diagnóstico e a legenda ``API: ...`` saíram da tela (decisão do
    cliente). Regressão do vazamento: a rota /api/lojas já não devolve a lista global
    e nada de JSON/URL pode aparecer para o usuário.
    """
    with app_test_logado() as app:
        app.radio(key="menu_principal").set_value("🏬 Minhas Lojas").run()
        assert not app.exception, app.exception

        rotulos = [botao.label for botao in app.button]
        assert not [rotulo for rotulo in rotulos if "Testar Conexão" in rotulo], rotulos
        assert not [rotulo for rotulo in rotulos if "Atualizar Lista" in rotulo], rotulos

        # Nenhum bloco de código/JSON sobra na tela.
        assert not [elemento.value for elemento in app.code], [
            elemento.value[:200] for elemento in app.code
        ]

        textos = "\n".join(
            str(elemento.value)
            for elemento in list(app.markdown) + list(app.caption) + list(app.text)
        )
        assert "URL testada" not in textos, textos[:500]
        assert "API: `" not in textos, textos[:500]
        assert '"cnpj"' not in textos, textos[:500]


@teste
def test_apptest_minhas_lojas_do_admin_mostra_dono_e_aviso_de_suporte():
    """AppTest: cartão com o dono da loja e aviso ao consultar CNPJ de outra conta.

    `dono_email` só existe para administradores (visão de suporte): o cartão mostra
    "👤 Conta: ..." para o suporte saber de quem é a loja sem entrar na conta do
    cliente. E ao consultar o CNPJ de uma loja de outra conta a tela avisa que
    reenviar o certificado atualiza SÓ o certificado — a loja continua do cliente.
    """
    from unittest.mock import patch

    cnpj_cliente = "11222333000181"
    empresa_cliente = {
        "id": 424242,
        "cnpj": cnpj_cliente,
        "nome_loja": "Loja do Cliente",
        "razao_social": "Loja do Cliente Ltda",
        "municipio": "COTIA",
        "total_notas": 7,
        "certificado": {"validade": "2027-03-31"},
        "ultimo_nsu_acbr": "0",
        "dono_email": "cliente@exemplo.com",
    }
    _get_real = requests.get

    def _get_falso(url, *args, **kwargs):
        alvo = str(url)
        if alvo.endswith("/api/empresas/vinculadas"):
            return _RespostaJson(200, [empresa_cliente])
        if alvo.endswith("/api/empresas/consultar-cnpj"):
            return _RespostaJson(
                200,
                {
                    "cnpj": cnpj_cliente,
                    "encontrado": True,
                    "razao_social": empresa_cliente["razao_social"],
                    "nome_loja": empresa_cliente["nome_loja"],
                    "fonte": "brasilapi",
                    "aviso": None,
                    "da_conta": False,
                    "de_outra_conta": True,
                    "dono_email": empresa_cliente["dono_email"],
                    "endereco": {
                        "logradouro": "RUA TESTE",
                        "numero": "",
                        "complemento": "",
                        "bairro": "CENTRO",
                        "cidade": "COTIA",
                        "uf": "SP",
                        "cep": "06700000",
                        "codigo_municipio": "3513009",
                    },
                },
            )
        return _get_real(url, *args, **kwargs)

    with patch("requests.get", side_effect=_get_falso):
        with app_test_logado(admin=True) as app:
            app.radio(key="menu_principal").set_value("🏬 Minhas Lojas").run()
            assert not app.exception, f"Minhas Lojas levantou exceção: {app.exception}"

            legendas = "\n".join(str(elemento.value) for elemento in app.caption)
            assert "👤 Conta: cliente@exemplo.com" in legendas, legendas[:800]
            assert "Visão do administrador" in legendas, legendas[:800]

            # Consultar o CNPJ da loja de um cliente: o admin vê o dono e o aviso.
            app.text_input(key="nova_loja_cnpj").set_value("11.222.333/0001-81")
            app.button(key="btn_buscar_cnpj_loja").click().run()
            assert not app.exception, (
                f"a consulta do CNPJ levantou exceção: {app.exception}"
            )
            avisos = "\n".join(str(elemento.value) for elemento in app.warning)
            assert "somente o certificado" in avisos, avisos[:800]
            assert "cliente@exemplo.com" in avisos, avisos[:800]


@teste
def test_apptest_previa_tem_serie_historica_e_memoria_de_calculo():
    """AppTest: a prévia tem "Série histórica" e o botão da memória de cálculo (Excel).

    A série histórica perdeu as colunas Índice/Cobertura (decisão do cliente: só mês e
    variação) e a conta aberta do mês fechado fica no Excel servido pela rota
    /api/alertas/relatorio/memoria-calculo. A conta sem compras no mês anterior recebe
    o 404 explicativo no lugar do arquivo — o teste aceita os dois estados.
    """
    with app_test_logado() as app:
        app.radio(key="menu_principal").set_value("🔔 Configurar Alertas").run()
        assert not app.exception, app.exception
        next(botao for botao in app.button if "Gerar prévia" in botao.label).click().run()
        assert not app.exception, f"a prévia levantou exceção: {app.exception}"

        markdowns = "\n".join(str(elemento.value) for elemento in app.markdown)
        assert "Memória de cálculo (Excel)" in markdowns, markdowns[-800:]
        rotulos = [botao.label for botao in app.button]
        assert any("Preparar memória de cálculo" in rotulo for rotulo in rotulos), rotulos

        previa = app.session_state["_previa_relatorio_alerta"] or {}
        serie = ((previa.get("inflacao") or {}).get("serie")) or []
        if serie:
            assert "Série histórica" in markdowns, markdowns[-800:]
            colunas = [list(tabela.value.columns) for tabela in app.dataframe]
            assert ["Mês", "Variação", "Obs."] in colunas, colunas

        next(
            botao for botao in app.button if "Preparar memória de cálculo" in botao.label
        ).click().run()
        assert not app.exception, f"a memória de cálculo levantou exceção: {app.exception}"
        memoria = app.session_state.get("_memoria_calculo_alerta") or {}
        if memoria:
            assert memoria["conteudo"][:2] == b"PK", memoria["conteudo"][:20]
            assert str(memoria["nome"]).endswith(".xlsx"), memoria["nome"]
        rotulos_download = [botao.label for botao in app.download_button]
        erros = "\n".join(str(elemento.value) for elemento in app.error)
        assert any(
            "Baixar memória de cálculo" in (rotulo or "") for rotulo in rotulos_download
        ) or "mês anterior" in erros, (
            "nem o download nem o motivo apareceram",
            erros[:500],
            rotulos_download,
        )


@teste
def test_apptest_cep_preenche_endereco_sem_salvar():
    """CEP preenche rua/bairro/município/UF (e o IBGE na sessão) SEM salvar a loja.

    O campo CEP fica FORA do `st.form`: dentro do formulário nenhum `on_change`
    dispara até o submit, e era por isso que o endereço ficava vazio quando a
    Receita não respondia (a ACBr recusava com "Complete o cadastro da empresa").
    """
    from unittest.mock import patch

    cep = "06708415"
    with app_test_logado() as app:
        app.radio(key="menu_principal").set_value("🏬 Minhas Lojas").run()
        assert not app.exception, app.exception

        _get_real = requests.get

        def _get_falso(url, *args, **kwargs):
            if str(url).endswith("/api/empresas/consultar-cep"):
                return _RespostaJson(
                    200,
                    {
                        "cep": cep,
                        "encontrado": True,
                        "endereco": {
                            "logradouro": "RUA JOSE FELIX DE OLIVEIRA",
                            "bairro": "VILA SANTO ANTONIO",
                            "cidade": "COTIA",
                            "uf": "SP",
                            "codigo_municipio": "3513009",
                        },
                        "fonte": "viacep",
                        "aviso": None,
                    },
                )
            return _get_real(url, *args, **kwargs)

        with patch("requests.get", side_effect=_get_falso), patch(
            "requests.post"
        ) as post_falso:
            app.text_input(key="nova_loja_cep").set_value(cep).run()

        assert not app.exception, f"a consulta do CEP levantou exceção: {app.exception}"
        assert not post_falso.called, "consultar o CEP não pode salvar a loja"
        assert (
            app.text_input(key="nova_loja_logradouro").value
            == "RUA JOSE FELIX DE OLIVEIRA"
        )
        assert app.text_input(key="nova_loja_bairro").value == "VILA SANTO ANTONIO"
        assert app.text_input(key="nova_loja_municipio").value == "COTIA"
        assert app.text_input(key="nova_loja_uf").value == "SP"
        assert app.session_state["nova_loja_codigo_municipio"] == "3513009"


@teste
def test_apptest_cnpj_preenche_endereco_sem_salvar():
    """Consultar o CNPJ preenche o endereço fiscal e NÃO envia o cadastro.

    O CNPJ fica FORA do `st.form` justamente para isso: dentro do formulário nenhum
    campo dispara run até o submit, então o endereço nunca era preenchido (e a ACBr
    recusava o cadastro com "Complete o cadastro da empresa").
    """
    from unittest.mock import patch

    cnpj = "07269638000171"
    with app_test_logado() as app:
        app.radio(key="menu_principal").set_value("🏬 Minhas Lojas").run()
        assert not app.exception, app.exception

        _get_real = requests.get

        def _get_falso(url, *args, **kwargs):
            if str(url).endswith("/api/empresas/consultar-cnpj"):
                return _RespostaJson(
                    200,
                    {
                        "cnpj": cnpj,
                        "encontrado": True,
                        "razao_social": "PORTUCHO BAR E LANCHONETE LTDA",
                        "nome_loja": "Portucho Matriz",
                        "municipio": "COTIA",
                        "endereco": {
                            "cep": "06708415",
                            "logradouro": "RUA JOSE FELIX DE OLIVEIRA",
                            "numero": "991",
                            "complemento": "",
                            "bairro": "VILA SANTO ANTONIO",
                            "cidade": "COTIA",
                            "codigo_municipio": "3513009",
                            "uf": "SP",
                        },
                        "fonte": "brasilapi",
                        "aviso": None,
                        "da_conta": False,
                        "de_outra_conta": False,
                    },
                )
            return _get_real(url, *args, **kwargs)

        app.text_input(key="nova_loja_cnpj").set_value(cnpj)
        with patch("requests.get", side_effect=_get_falso), patch(
            "requests.post"
        ) as post_falso:
            buscar = next(
                botao for botao in app.button if "Buscar dados do CNPJ" in botao.label
            )
            buscar.click().run()

        assert not app.exception, f"a consulta do CNPJ levantou exceção: {app.exception}"
        assert not post_falso.called, "consultar o CNPJ não pode salvar a loja"
        assert app.text_input(key="nova_loja_cnpj").value == cnpj
        assert app.text_input(key="nova_loja_cep").value == "06708415"
        assert (
            app.text_input(key="nova_loja_logradouro").value
            == "RUA JOSE FELIX DE OLIVEIRA"
        )
        assert app.text_input(key="nova_loja_numero").value == "991"
        assert app.text_input(key="nova_loja_bairro").value == "VILA SANTO ANTONIO"
        assert app.text_input(key="nova_loja_municipio").value == "COTIA"
        assert app.text_input(key="nova_loja_uf").value == "SP"
        # O IBGE não tem widget: foi para a sessão e vai no payload da ACBr.
        assert app.session_state["nova_loja_codigo_municipio"] == "3513009"
        sucessos = "\n".join(str(elemento.value) for elemento in app.success)
        assert "Receita Federal" in sucessos, sucessos


# ──────────────────────────────────────────────────────────────────────────────
# Execução direta (sem pytest): python tests/test_integridade_relatorios.py
# ──────────────────────────────────────────────────────────────────────────────
def main():
    try:
        requests.get(BASE_API + "/", timeout=10).raise_for_status()
    except Exception as erro:
        print(f"❌ Backend indisponível em {BASE_API}: {erro}")
        print("   Suba a API antes de rodar os testes: python -m uvicorn backend.main:app --port 8000")
        return 1

    falhas = 0
    for funcao in TESTS:
        try:
            funcao()
        except Exception:
            falhas += 1
            print(f"❌ {funcao.__name__}")
            traceback.print_exc()
        else:
            print(f"✅ {funcao.__name__}")
    print(f"\n{len(TESTS) - falhas}/{len(TESTS)} testes passaram.")
    return 1 if falhas else 0


# ──────────────────────────────────────────────────────────────────────────────
# Regra 12: cadastro sempre grava o Nome Fantasia da BrasilAPI
# ──────────────────────────────────────────────────────────────────────────────
@teste
def test_cadastro_atualiza_nome_loja_com_brasilapi():
    """`/api/empresas/cadastrar` SEMPRE renomeia a loja com o Nome Fantasia da Receita.

    Testado na função que o cadastro usa (`_atualizar_dados_cadastrais`), sem depender da
    rede: a resposta da BrasilAPI sobrescreve o nome antigo ("Deck Burger" -> "Moça Bar")
    e o placeholder de consulta indisponível NUNCA troca o nome da loja.
    """
    empresa = Empresa(
        cnpj="50951160000194",
        nome_loja="Deck Burger",
        razao_social="Deck Burger Ltda",
        municipio="",
    )
    dados_receita = {
        "razao_social": "Moca Bar e Lanchonete Ltda",
        "nome_loja": "Moça Bar",
        "municipio": "COTIA",
        "fonte": "brasilapi",
    }
    assert bmain._atualizar_dados_cadastrais(empresa, dados_receita) is True
    assert empresa.nome_loja == "Moça Bar", empresa.nome_loja
    assert empresa.razao_social == "Moca Bar e Lanchonete Ltda", empresa.razao_social
    assert empresa.municipio == "COTIA", empresa.municipio

    # Consulta indisponível (offline / 429 da BrasilAPI): nada é sobrescrito.
    antes = (empresa.nome_loja, empresa.razao_social, empresa.municipio)
    indisponivel = {
        "razao_social": "Restaurante Exemplo",
        "nome_loja": "Loja Exemplo",
        "municipio": "",
        "fonte": "indisponivel",
    }
    assert bmain._atualizar_dados_cadastrais(empresa, indisponivel) is False
    assert (empresa.nome_loja, empresa.razao_social, empresa.municipio) == antes, antes
    # A reserva do cadastro marca a origem do dado (o cadastro usa isso para decidir).
    assert "fonte" in (bmain._consultar_cnpj.__doc__ or "")


@teste
def test_cadastro_avisa_quando_receita_indisponivel():
    """Sem resposta da Receita, o cadastro devolve `fonte_dados`/`aviso` ao front.

    O 429 da BrasilAPI não pode mais "sumir": a loja é criada (o vínculo não depende
    da Receita), mas a resposta avisa que o endereço pode precisar ser informado à mão
    — é esse `aviso` que a tela "Minhas Lojas" mostra em `st.warning`.
    """
    import asyncio
    from unittest.mock import patch

    db, engine = _banco_aprendizado_temporario()
    try:
        usuario = Usuario(
            nome="Conta Receita Fora",
            email=f"receita.fora.{uuid.uuid4().hex[:8]}@teste.local",
            senha_hash="nao-usado",
            is_admin=False,
        )
        db.add(usuario)
        db.commit()
        db.refresh(usuario)

        indisponivel = {
            "razao_social": "Restaurante Exemplo",
            "nome_loja": "Loja Exemplo",
            "municipio": "",
            "cep": "",
            "uf": "",
            "codigo_municipio": "",
            "bairro": "",
            "logradouro": "",
            "numero": "",
            "complemento": "",
            "email": "",
            "telefone": "",
            "fonte": "indisponivel",
        }
        receita = {
            "razao_social": "LOJA TESTE LTDA",
            "nome_loja": "Loja Teste",
            "municipio": "COTIA",
            "cep": "06708415",
            "uf": "SP",
            "codigo_municipio": "3513009",
            "bairro": "VILA SANTO ANTONIO",
            "logradouro": "RUA JOSE FELIX DE OLIVEIRA",
            "numero": "991",
            "complemento": "",
            "email": "loja@teste.local",
            "telefone": "",
            "fonte": "brasilapi",
        }

        def _cadastrar():
            upload = bmain.UploadFile(
                file=BytesIO(_certificado_teste()),
                filename="certificado-teste.pfx",
            )
            return asyncio.run(
                bmain.cadastrar_empresa(
                    usuario=usuario,
                    db=db,
                    cnpj=CNPJ_CERTIFICADO_TESTE,
                    senha_certificado=SENHA_CERTIFICADO_TESTE,
                    certificado=upload,
                )
            )

        with patch.object(bmain, "_consultar_cnpj", return_value=dict(indisponivel)):
            corpo = _cadastrar()
        assert corpo["sucesso"] is True, corpo
        assert corpo["fonte_dados"] == "indisponivel", corpo
        assert corpo["aviso"], "a resposta precisa avisar que o endereço pode faltar"
        assert "endereço" in corpo["aviso"].lower(), corpo["aviso"]

        # Mesmo CNPJ, agora com a Receita respondendo: nenhum aviso pendente.
        with patch.object(bmain, "_consultar_cnpj", return_value=dict(receita)):
            corpo_atualizado = _cadastrar()
        assert corpo_atualizado["fonte_dados"] == "brasilapi", corpo_atualizado
        assert corpo_atualizado["aviso"] is None, corpo_atualizado
        loja = db.query(Empresa).filter(Empresa.cnpj == CNPJ_CERTIFICADO_TESTE).first()
        assert loja is not None, "a loja precisa continuar no banco"
        assert loja.cep == "06708415", loja.cep
        assert loja.bairro == "VILA SANTO ANTONIO", loja.bairro
    finally:
        db.close()
        engine.dispose()


# ──────────────────────────────────────────────────────────────────────────────
# Regra 13: "Excluir Certificado" do cartão da loja
# ──────────────────────────────────────────────────────────────────────────────
@teste
def test_api_excluir_certificado_da_loja():
    """Excluir certificado: dono OU admin (suporte), e só o certificado (a loja continua)."""
    usuario, token, email, _ = _registrar_usuario_teste("Conta Certificado")
    outro, token_outro, _, _ = _registrar_usuario_teste("Outra Conta Certificado")
    try:
        _criar_empresa_teste(CNPJ_TESTE)
        vinculo = _post_cadastro_empresa(
            CNPJ_TESTE, token, certificado=_certificado_teste()
        )
        assert vinculo.status_code == 200, vinculo.text[:300]
        empresa = vinculo.json()["empresa"]
        assert empresa["certificado"]["validade"], empresa
        url = BASE_API + f"/api/empresas/{empresa['id']}/certificado"

        # Barreiras: sem sessão (401), loja de outra conta (403), id inexistente (404).
        assert requests.delete(url, timeout=30).status_code == 401
        assert (
            requests.delete(
                url,
                headers={"Authorization": f"Bearer {token_outro}"},
                timeout=30,
            ).status_code
            == 403
        )
        assert (
            requests.delete(
                BASE_API + "/api/empresas/99999999/certificado",
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            ).status_code
            == 404
        )
        # Nenhuma tentativa bloqueada apagou o certificado.
        assert _empresas_vinculadas(token)[0]["certificado"]["validade"]

        apagado = requests.delete(
            url, headers={"Authorization": f"Bearer {token}"}, timeout=30
        )
        assert apagado.status_code == 200, apagado.text[:300]
        corpo = apagado.json()
        assert corpo["certificado_excluido"] is True, corpo
        assert corpo["empresa"]["certificado"] is None, corpo
        loja = _empresas_vinculadas(token)[0]
        assert loja["cnpj"] == CNPJ_TESTE and loja["usuario_id"] == usuario["id"], loja
        assert loja["certificado"] is None, loja

        # Excluir de novo não é erro: a loja já estava sem certificado.
        de_novo = requests.delete(
            url, headers={"Authorization": f"Bearer {token}"}, timeout=30
        )
        assert de_novo.status_code == 200, de_novo.text[:300]
        assert de_novo.json()["certificado_excluido"] is False, de_novo.json()

        # Visão de suporte: o admin vê TODAS as lojas (com o dono) e também gerencia o
        # certificado de uma loja de cliente — reenviar não rouba a loja.
        with conta_admin_teste() as (admin, token_admin):
            vinculadas = requests.get(
                BASE_API + "/api/empresas/vinculadas",
                headers=_cabecalho_admin(token_admin),
                timeout=60,
            )
            assert vinculadas.status_code == 200, vinculadas.text[:300]
            do_cliente = [
                loja for loja in vinculadas.json() if loja["cnpj"] == CNPJ_TESTE
            ]
            assert do_cliente, vinculadas.json()
            assert do_cliente[0]["usuario_id"] == usuario["id"], do_cliente[0]
            assert do_cliente[0]["dono_email"] == email, do_cliente[0]

            # Reenviar o certificado de uma loja de cliente: troca SÓ o certificado,
            # o dono continua o cliente (`acao_banco="certificado_admin"`).
            reenviado = _post_cadastro_empresa(
                CNPJ_TESTE,
                token_admin,
                certificado=_certificado_teste(),
                cabecalho_extra=_cabecalho_admin(token_admin),
            )
            assert reenviado.status_code == 200, reenviado.text[:300]
            corpo_reenvio = reenviado.json()
            assert corpo_reenvio["acao_banco"] == "certificado_admin", corpo_reenvio
            assert corpo_reenvio["adotada"] is False, corpo_reenvio
            assert corpo_reenvio["empresa"]["usuario_id"] == usuario["id"], corpo_reenvio
            assert corpo_reenvio["empresa"]["certificado"]["validade"], corpo_reenvio

            # E o admin também consegue excluir o certificado dessa loja.
            pelo_admin = requests.delete(
                url, headers=_cabecalho_admin(token_admin), timeout=30
            )
            assert pelo_admin.status_code == 200, pelo_admin.text[:300]
            assert pelo_admin.json()["certificado_excluido"] is True, pelo_admin.json()

        # A loja continua sendo do cliente, agora sem certificado.
        depois = _empresas_vinculadas(token)
        assert len(depois) == 1, depois
        assert depois[0]["cnpj"] == CNPJ_TESTE, depois
        assert depois[0]["usuario_id"] == usuario["id"], depois
        assert depois[0]["certificado"] is None, depois
    finally:
        _apagar_empresa_teste(CNPJ_TESTE)
        _apagar_usuario_teste(outro["id"])
        _apagar_usuario_teste(usuario["id"])



# ──────────────────────────────────────────────────────────────────────────────
# Regra 15: memória de aprendizado (fornecedor + cProd) e embalagens
# ──────────────────────────────────────────────────────────────────────────────
def _banco_aprendizado_temporario():
    """SQLAlchemy em um SQLite temporário (nenhum teste escreve na base real)."""
    pasta = Path(tempfile.mkdtemp(prefix="aprendizado_"))
    engine = create_engine("sqlite:///" + str(pasta / "memoria.db").replace("\\", "/"))
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)(), engine


@teste
def test_embalagem_caixa_para_unidade():
    """A embalagem é IDENTIDADE: não converte quantidade/preço para a média.

    - ``normalizar_embalagem`` produz o token canônico (CX24, UN, CX15KG...);
    - ``normalizar_quantidades`` preserva os valores CRUS do XML (2 CX a
      R$ 240,00 continuam 2 e R$ 240,00 — nunca 48 UN a R$ 10,00);
    - ``interpretar_embalagem`` continua lendo o fator só para EXIBIÇÃO.
    """
    assert baprendizado.normalizar_embalagem("CX", "CAIXA C/ 24") == "CX24"
    assert baprendizado.normalizar_embalagem("CX", "CX 24") == "CX24"
    assert baprendizado.normalizar_embalagem("", "1X24") == "CX24"
    assert baprendizado.normalizar_embalagem("UN", "UNIDADE") == "UN"
    assert baprendizado.normalizar_embalagem("", "1X1") == "UN"
    assert baprendizado.normalizar_embalagem("CX", "CX 15KG") == "CX15KG"
    assert baprendizado.normalizar_embalagem("FD", "10X1KG") == "FD10X1KG"

    casos = {
        ("CX", "REFRIGERANTE COCA COLA 24 UN"): (24.0, "UN"),
        ("CX", "SUCO DEL VALLE 12X300 ML"): (12.0, "UN"),
        ("CX", "FILE PEITO CX 15,000KG"): (15.0, "KG"),
        ("CX027", "AGUA MINERAL 500ML"): (27.0, "UN"),
        ("UN", "ARROZ TIO JOAO 5KG"): (1.0, "UN"),
        ("CX", "SACO LIXO 30X40CM"): (1.0, "UN"),
    }
    for (u_com, descricao), (fator, unidade) in casos.items():
        embalagem = baprendizado.interpretar_embalagem(u_com, descricao)
        assert float(embalagem["qtd_por_embalagem"]) == fator, (u_com, descricao, embalagem)
        assert embalagem["unidade_base"] == unidade, (u_com, descricao, embalagem)
        assert embalagem["confirmada"] is (fator > 1.0), (u_com, descricao, embalagem)

    # A conversão matemática NÃO acontece mais: o XML cru é soberano.
    caixa = baprendizado.interpretar_embalagem("CX", "REFRIGERANTE 24 UN")
    assert baprendizado.normalizar_quantidades(2, 240, caixa) == (2.0, 240.0)
    assert baprendizado.formatar_embalagem(caixa) == "CAIXA 24 UN"
    peso = baprendizado.interpretar_embalagem("CX", "CX 15KG")
    assert baprendizado.normalizar_quantidades(1, 300, peso) == (1.0, 300.0)
    assert baprendizado.formatar_embalagem(peso) == "CAIXA 15 KG"
    sem_fator = baprendizado.interpretar_embalagem("UN", "ARROZ 5KG")
    assert baprendizado.normalizar_quantidades(3, 7.5, sem_fator) == (3.0, 7.5)
    assert baprendizado.formatar_embalagem(sem_fator) == ""


@teste
def test_chave_separa_embalagem_cx24_de_un():
    """CX24 e UN são PRODUTOS diferentes na chave inteligente (mesmo EAN/volume)."""
    ean = "7891234567890"
    caixa = "ORIGINAL 600ML CX C/24"
    unidade = "ORIGINAL 600ML UN"
    chave_caixa = bmain._chave_produto_relatorio(ean, caixa, None, "CX")
    chave_unidade = bmain._chave_produto_relatorio(ean, unidade, None, "UN")
    assert chave_caixa != chave_unidade, (chave_caixa, chave_unidade)
    assert chave_caixa.endswith("_EMB_CX24"), chave_caixa
    assert "_EMB_" not in chave_unidade, chave_unidade

    # A embalagem é reaplicada depois de uma regra "juntar" forçada.
    final_caixa = bmain._chave_final_relatorio(
        chave_caixa, caixa, {chave_caixa: chave_unidade}, {}, None, "CX"
    )
    assert final_caixa != chave_unidade, final_caixa
    assert final_caixa.endswith("_EMB_CX24"), final_caixa


@teste
def test_memoria_de_para_aprende_e_reutiliza():
    """O de-para (CNPJ + cProd) grava o canônico e a importação reusa na nota seguinte."""
    db, engine = _banco_aprendizado_temporario()
    try:
        cnpj = "12.345.678/0001-99"
        caixa = baprendizado.interpretar_embalagem("CX", "REFRIGERANTE 24 UN")
        mapa, criado, avisos = baprendizado.registrar_aprendizado(
            db,
            cnpj,
            " sku-1 ",
            ean="7891234567890",
            descricao="REFRIGERANTE COLA LATA 350ML CX 24",
            ncm="22021000",
            embalagem=caixa,
        )
        assert criado is True and avisos == [], avisos
        assert mapa.cnpj_fornecedor == "12345678000199", mapa.cnpj_fornecedor
        assert mapa.c_prod == "SKU-1", mapa.c_prod
        assert mapa.qtd_por_embalagem == 24.0 and mapa.unidade_base == "UN", mapa.qtd_por_embalagem

        # Nota seguinte: XML sem EAN e com descrição abreviada — a memória manda.
        achado = baprendizado.consultar_mapeamento(db, "12345678000199", "sku-1")
        ean, descricao, ncm = baprendizado.aplicar_identidade(
            achado, "SEM_EAN", "REFRIG COLA", "00000000"
        )
        assert ean == "7891234567890", ean
        assert descricao == "REFRIGERANTE COLA LATA 350ML CX 24", descricao
        assert ncm == "22021000", ncm
        memoria = baprendizado.aplicar_embalagem_aprendida(
            baprendizado.interpretar_embalagem("CX", "REFRIG COLA"), achado
        )
        assert memoria["origem"] == "memoria", memoria
        assert memoria["qtd_por_embalagem"] == 24.0, memoria
        assert baprendizado.normalizar_quantidades(2, 240, memoria) == (2.0, 240.0)

        # EAN/descrição/embalagem divergentes NÃO sobrescrevem o canônico: geram aviso.
        mapa2, criado2, avisos2 = baprendizado.registrar_aprendizado(
            db,
            cnpj,
            "SKU-1",
            ean="7899999999999",
            descricao="OUTRA DESCRICAO",
            ncm="99999999",
            embalagem=baprendizado.interpretar_embalagem("CX", "OUTRA CX 12"),
        )
        assert criado2 is False, criado2
        assert any("EAN divergente" in aviso for aviso in avisos2), avisos2
        assert any("Embalagem divergente" in aviso for aviso in avisos2), avisos2
        assert mapa2.ean_padrao == "7891234567890", mapa2.ean_padrao
        assert mapa2.descricao_padrao == "REFRIGERANTE COLA LATA 350ML CX 24", mapa2.descricao_padrao
        assert mapa2.ncm_gravado == "22021000", mapa2.ncm_gravado
        assert mapa2.qtd_por_embalagem == 24.0, mapa2.qtd_por_embalagem

        # Correção manual (Auditoria): a importação nunca sobrescreve.
        mapa2.origem = "manual"
        mapa2.descricao_padrao = "PRODUTO CORRIGIDO NA AUDITORIA"
        mapa2.qtd_por_embalagem = 6.0
        db.commit()
        mapa3, _criado, _avisos = baprendizado.registrar_aprendizado(
            db,
            cnpj,
            "SKU-1",
            ean="1112223334445",
            descricao="XML QUALQUER",
            ncm="11111111",
            embalagem=baprendizado.interpretar_embalagem("CX", "CX 24"),
        )
        assert mapa3.descricao_padrao == "PRODUTO CORRIGIDO NA AUDITORIA", mapa3.descricao_padrao
        assert mapa3.qtd_por_embalagem == 6.0, mapa3.qtd_por_embalagem
        assert mapa3.ean_padrao == "7891234567890", mapa3.ean_padrao
        assert mapa3.origem == "manual", mapa3.origem
    finally:
        db.close()
        engine.dispose()


@teste
def test_backfill_normaliza_historico_e_preserva_correcao_manual():
    """`sincronizar_aprendizado` (backfill) normaliza os itens antigos sem perder o manual.

    - o item guarda o valor CRU do XML (`quantidade`/`valor_unitario`) e os campos
      `quantidade_base`/`valor_unitario_base` permanecem IGUAIS ao cru (a embalagem é
      identidade, não conversão);
    - a descrição antiga alimenta a memória do fornecedor (cProd);
    - um de-para corrigido na Auditoria (`origem="manual"`) não é sobrescrito;
    - `aplicar_mapeamento_historico` reaplica a correção nas notas já importadas.
    """
    db, engine = _banco_aprendizado_temporario()
    try:
        cnpj = "12345678000199"
        empresa = Empresa(cnpj="50.951.160/0001-94", nome_loja="Loja Teste")
        fornecedor = Fornecedor(cnpj=cnpj, nome="Fornecedor Teste")
        db.add_all([empresa, fornecedor])
        db.commit()
        nota = NotaFiscal(
            chave_acesso="TESTE-APRENDIZADO-0001",
            numero_nota="1",
            serie="1",
            data_emissao=date(2026, 1, 15),
            valor_total=340.0,
            empresa_id=empresa.id,
        )
        db.add(nota)
        db.commit()
        db.add_all(
            [
                ItemNota(
                    nota_id=nota.id,
                    fornecedor_id=fornecedor.id,
                    ean="SEM_EAN",
                    descricao_produto="SUCO DEL VALLE 12X300 ML",
                    quantidade=2,
                    valor_unitario=120,
                    valor_total=240,
                    ncm="22021000",
                    c_prod="SKU-2",
                    unidade_compra="CX",
                ),
                ItemNota(
                    nota_id=nota.id,
                    fornecedor_id=fornecedor.id,
                    ean="7890000000002",
                    descricao_produto="CAIXA COM 24 UN",
                    quantidade=1,
                    valor_unitario=100,
                    valor_total=100,
                    ncm="22021000",
                    c_prod="SKU-3",
                    unidade_compra="CX",
                ),
            ]
        )
        db.add(
            MapeamentoFornecedorProduto(
                cnpj_fornecedor="12345678000199",
                c_prod="SKU-3",
                ean_padrao="7890000000002",
                descricao_padrao="CORRIGIDO NA AUDITORIA",
                qtd_por_embalagem=6.0,
                unidade_base="UN",
                origem="manual",
            )
        )
        db.commit()

        resumo = baprendizado.sincronizar_aprendizado(db)
        assert resumo["itens"] == 2, resumo
        assert resumo["itens_com_embalagem"] == 2, resumo
        assert resumo["mapeamentos_criados"] == 1, resumo
        assert resumo["sem_codigo_fornecedor"] == 0, resumo

        suco = db.query(ItemNota).filter_by(c_prod="SKU-2").one()
        assert suco.quantidade == 2 and suco.valor_unitario == 120, "o XML cru foi alterado"
        assert suco.qtd_por_embalagem == 12.0, suco.qtd_por_embalagem
        assert suco.unidade_base == "UN", suco.unidade_base
        # Sem conversão: a base é o valor cru do XML (embalagem = identidade).
        assert suco.quantidade_base == 2.0, suco.quantidade_base
        assert suco.valor_unitario_base == 120.0, suco.valor_unitario_base
        aprendido = baprendizado.consultar_mapeamento(db, cnpj, "SKU-2")
        assert aprendido is not None, "a descrição antiga não alimentou a memória"
        assert aprendido.descricao_padrao == "SUCO DEL VALLE 12X300 ML", aprendido.descricao_padrao

        # A correção manual prevalece: o fator do auditor (6) vence os 24 do XML.
        caixa = db.query(ItemNota).filter_by(c_prod="SKU-3").one()
        assert caixa.qtd_por_embalagem == 6.0, caixa.qtd_por_embalagem
        assert caixa.quantidade_base == 1.0, caixa.quantidade_base
        assert caixa.valor_unitario_base == 100.0, caixa.valor_unitario_base
        manual = db.query(MapeamentoFornecedorProduto).filter_by(c_prod="SKU-3").one()
        assert manual.origem == "manual" and manual.descricao_padrao == "CORRIGIDO NA AUDITORIA"
        assert manual.qtd_por_embalagem == 6.0, manual.qtd_por_embalagem

        # Correção na Auditoria (6 → 12) reescreve o histórico e o preço normalizado.
        manual.qtd_por_embalagem = 12.0
        manual.ncm_gravado = "20099000"
        db.commit()
        afetados = baprendizado.aplicar_mapeamento_historico(db, manual)
        assert afetados == 1, afetados
        db.refresh(caixa)
        assert caixa.descricao_produto == "CORRIGIDO NA AUDITORIA", caixa.descricao_produto
        assert caixa.ncm == "20099000", caixa.ncm
        assert caixa.qtd_por_embalagem == 12.0, caixa.qtd_por_embalagem
        assert caixa.quantidade_base == 1.0, caixa.quantidade_base
        assert caixa.valor_unitario_base == 100.0, caixa.valor_unitario_base
        assert caixa.quantidade == 1 and caixa.valor_unitario == 100, "o XML cru foi alterado"
    finally:
        db.close()
        engine.dispose()


@teste
def test_backfill_recupera_cprod_do_xml_salvo():
    """Base antiga sem cProd: o backfill relê o XML e casa o item por descrição + qtde.

    O XML de teste tem um item de bonificação que não existe na base — a leitura NÃO
    pode casar por posição. Um XML ausente só conta em `xmls_nao_encontrados`.
    """
    db, engine = _banco_aprendizado_temporario()
    pasta = Path(tempfile.mkdtemp(prefix="xml_aprendizado_"))
    try:
        xml = pasta / "nota.xml"
        xml.write_text(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
            "<nfeProc xmlns=\"http://www.portalfiscal.inf.br/nfe\"><NFe><infNFe>"
            "<det nItem=\"1\"><prod><cProd>SKU-9</cProd><cEAN>7891234567890</cEAN>"
            "<xProd>REFRIGERANTE COLA 24 UN</xProd><NCM>22021000</NCM>"
            "<qCom>2.0000</qCom><vUnCom>120.00</vUnCom><uCom>CX</uCom></prod></det>"
            "<det nItem=\"2\"><prod><cProd>BRINDE</cProd><cEAN>SEM GTIN</cEAN>"
            "<xProd>BRINDE PROMOCIONAL</xProd><NCM>22021000</NCM><qCom>1.0000</qCom>"
            "<vUnCom>0.00</vUnCom><uCom>UN</uCom></prod></det>"
            "</infNFe></NFe></nfeProc>",
            encoding="utf-8",
        )
        empresa = Empresa(cnpj="50.951.160/0001-94", nome_loja="Loja XML")
        fornecedor = Fornecedor(cnpj="12345678000199", nome="Fornecedor XML")
        db.add_all([empresa, fornecedor])
        db.commit()
        nota = NotaFiscal(
            chave_acesso="TESTE-APRENDIZADO-XML-1",
            numero_nota="2",
            serie="1",
            data_emissao=date(2026, 2, 1),
            valor_total=240.0,
            empresa_id=empresa.id,
            caminho_xml=str(xml),
        )
        nota_sem_xml = NotaFiscal(
            chave_acesso="TESTE-APRENDIZADO-XML-2",
            numero_nota="3",
            serie="1",
            data_emissao=date(2026, 2, 2),
            valor_total=10.0,
            empresa_id=empresa.id,
            caminho_xml=str(pasta / "nao_existe.xml"),
        )
        db.add_all([nota, nota_sem_xml])
        db.commit()
        item = ItemNota(
            nota_id=nota.id,
            fornecedor_id=fornecedor.id,
            ean="7891234567890",
            descricao_produto="REFRIGERANTE COLA 24 UN",
            quantidade=2,
            valor_unitario=120,
            valor_total=240,
            ncm="22021000",
        )
        item_sem_xml = ItemNota(
            nota_id=nota_sem_xml.id,
            fornecedor_id=fornecedor.id,
            ean="SEM_EAN",
            descricao_produto="ITEM SEM XML",
            quantidade=1,
            valor_unitario=10,
            valor_total=10,
        )
        db.add_all([item, item_sem_xml])
        db.commit()

        resumo = baprendizado.sincronizar_aprendizado(db)
        assert resumo["itens"] == 2, resumo
        assert resumo["itens_codigo_recuperado"] == 1, resumo
        assert resumo["xmls_lidos"] == 1, resumo
        assert resumo["xmls_nao_encontrados"] == 1, resumo
        assert resumo["mapeamentos_criados"] == 1, resumo
        assert resumo["itens_com_embalagem"] == 1, resumo
        assert resumo["sem_codigo_fornecedor"] == 1, resumo

        db.refresh(item)
        assert item.c_prod == "SKU-9", item.c_prod
        assert item.unidade_compra == "CX", item.unidade_compra
        assert item.qtd_por_embalagem == 24.0, item.qtd_por_embalagem
        assert item.quantidade_base == 2.0, item.quantidade_base
        assert item.valor_unitario_base == 120.0, item.valor_unitario_base
        aprendido = baprendizado.consultar_mapeamento(db, "12345678000199", "SKU-9")
        assert aprendido is not None, "o cProd recuperado não alimentou a memória"
        assert aprendido.descricao_padrao == "REFRIGERANTE COLA 24 UN", aprendido.descricao_padrao
        assert aprendido.qtd_por_embalagem == 24.0, aprendido.qtd_por_embalagem

        db.refresh(item_sem_xml)
        assert item_sem_xml.c_prod is None, item_sem_xml.c_prod
    finally:
        db.close()
        engine.dispose()


@teste
def test_api_aprendizado_produtos_exige_admin_e_corrige():
    """Rotas de aprendizado: 401 sem sessão e correção manual via PUT (auth + 2FA)."""
    sem_sessao = requests.get(BASE_API + "/api/aprendizado-produtos", timeout=60)
    assert sem_sessao.status_code == 401, sem_sessao.status_code
    sem_sessao = requests.post(BASE_API + "/api/aprendizado-produtos/reprocessar", timeout=60)
    assert sem_sessao.status_code == 401, sem_sessao.status_code

    cnpj_teste = "99999999000199"
    c_prod_teste = "TESTE-APRENDIZADO-" + uuid.uuid4().hex[:8].upper()
    try:
        with conta_admin_teste("Admin Aprendizado") as (_usuario, token):
            dados = _api("/api/aprendizado-produtos", {"limite": 5}, token)
            for chave in (
                "total",
                "limite",
                "itens",
                "manuais",
                "itens_com_embalagem",
                "itens_sem_codigo_fornecedor",
            ):
                assert chave in dados, (chave, dados)
            assert dados["limite"] == 5, dados["limite"]
            assert len(dados["itens"]) <= 5, len(dados["itens"])

            correcao = requests.put(
                BASE_API + "/api/aprendizado-produtos",
                json={
                    "cnpj_fornecedor": cnpj_teste,
                    "c_prod": c_prod_teste,
                    "ean_padrao": "7890000000019",
                    "descricao_padrao": "PRODUTO TESTE APRENDIZADO",
                    "ncm_gravado": "22021000",
                    "qtd_por_embalagem": 24,
                    "unidade_base": "UN",
                },
                headers=_cabecalho_admin(token),
                timeout=120,
            )
            assert correcao.status_code == 200, correcao.text[:400]
            corpo = correcao.json()
            assert corpo["status"] == "sucesso", corpo
            mapeamento = corpo["mapeamento"]
            assert mapeamento["origem"] == "manual", mapeamento
            assert mapeamento["cnpj_fornecedor"] == cnpj_teste, mapeamento
            assert mapeamento["c_prod"] == c_prod_teste, mapeamento
            assert mapeamento["qtd_por_embalagem"] == 24.0, mapeamento
            assert mapeamento["unidade_base"] == "UN", mapeamento
            assert "itens_atualizados" in corpo, corpo

            buscado = _api("/api/aprendizado-produtos", {"busca": c_prod_teste}, token)
            assert buscado["total"] == 1, buscado
            assert buscado["itens"][0]["descricao_padrao"] == "PRODUTO TESTE APRENDIZADO", buscado

            # Fator inválido é recusado (não grava embalagem menor que 1 unidade).
            invalido = requests.put(
                BASE_API + "/api/aprendizado-produtos",
                json={"cnpj_fornecedor": cnpj_teste, "c_prod": c_prod_teste, "qtd_por_embalagem": 0},
                headers=_cabecalho_admin(token),
                timeout=60,
            )
            assert invalido.status_code == 400, invalido.text[:300]
    finally:
        db = SessionLocal()
        try:
            db.query(MapeamentoFornecedorProduto).filter(
                MapeamentoFornecedorProduto.c_prod.like("TESTE-APRENDIZADO-%")
            ).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()


@teste
def test_apptest_aba_aprendizado_embalagens():
    """AppTest: a aba 🧠 Aprendizado da IA (Marcas) mostra tabela com Marca e sem de-para manual."""
    with app_test_logado(admin=True) as app:
        app.radio(key="menu_principal").set_value("🔍 Auditoria (Admin)").run()
        assert not app.exception, f"Auditoria levantou exceção: {app.exception}"

        rotulos = [aba.label for aba in app.tabs]
        assert "🧠 Aprendizado da IA (Marcas)" in rotulos, rotulos

        cabecalhos = [elemento.value for elemento in app.header]
        assert any("Aprendizado da IA" in texto for texto in cabecalhos), cabecalhos
        campos = {campo.key for campo in app.text_input}
        assert "aprendizado_busca" in campos, campos
        botoes = [botao.label for botao in app.button]
        assert any("Reprocessar base" in (rotulo or "") for rotulo in botoes), botoes
        # As métricas e a tabela vêm de GET /api/aprendizado-produtos.
        rotulos_metricas = [metrica.label for metrica in app.metric]
        assert "De-Para aprendidos" in rotulos_metricas, rotulos_metricas
        assert "Itens com embalagem" in rotulos_metricas, rotulos_metricas
        if app.dataframe:
            tabela = app.dataframe[0].value
            assert "Marca" in tabela.columns, list(tabela.columns)
            assert (tabela["Marca"].astype(str).str.strip() != "").all(), tabela[
                "Marca"
            ].tolist()
        else:
            # Base ainda sem de-para: a aba orienta a rodar a importação/reprocessar.
            informes = " ".join(elemento.value for elemento in app.info)
            assert "Nenhum de-para encontrado" in informes, informes

        # A correção manual saiu desta aba: ela é feita na Auditoria de Divergências.
        subtitulos = " ".join(elemento.value for elemento in app.subheader)
        assert "Corrigir um De-Para" not in subtitulos, subtitulos
        assert "aprendizado_escolhido" not in [
            campo.key for campo in app.selectbox
        ], [campo.key for campo in app.selectbox]


# ──────────────────────────────────────────────────────────────────────────────
# Etapa 2: consenso com IA (DeepSeek) e exportação da base completa
# ──────────────────────────────────────────────────────────────────────────────
@teste
def test_ia_consenso_validar_marcas_e_resolver_ambiguidade():
    """Parser do consenso: cerca ```json, booleanos textuais e lista vazia sem HTTP.

    A chamada HTTP é substituída (`ia_consenso._chamar_deepseek`), então o teste roda
    offline e sem gastar cota da DeepSeek.
    """
    from unittest.mock import patch

    def fake_marcas(mensagens, temperatura=0.0, max_tokens=900):
        assert mensagens and mensagens[0]["role"] == "system"
        return (
            "```json\n"
            + json.dumps(
                {
                    "resultados": [
                        {
                            "palavra": "YOKI",
                            "e_marca": True,
                            "confianca": 0.97,
                            "justificativa": "Marca de massas.",
                        },
                        {
                            "palavra": "REFINADO",
                            "e_marca": "false",
                            "confianca": 90,
                            "justificativa": "Adjetivo do produto.",
                        },
                    ]
                },
                ensure_ascii=False,
            )
            + "\n```"
        )

    with patch.object(ia_consenso, "_chamar_deepseek", side_effect=fake_marcas):
        resultado = ia_consenso.validar_marcas_com_ia(
            ["YOKI", "REFINADO", "yoki", "  ", ""]
        )
    assert resultado["status"] == "sucesso", resultado
    assert resultado["marcas"] == ["YOKI"], resultado
    assert resultado["nao_marcas"] == ["REFINADO"], resultado
    assert resultado["nao_classificadas"] == [], resultado
    assert [item["palavra"] for item in resultado["resultados"]] == ["YOKI", "REFINADO"]
    assert resultado["resultados"][1]["confianca"] == 0.9, resultado["resultados"]

    def fake_ambiguidade(mensagens, temperatura=0.0, max_tokens=900):
        return (
            'Segue o veredito: {"mesmo_produto": "não", "confianca": 88, '
            '"motivo": "O peso muda o produto.", '
            '"diferencas": ["5KG x 1KG"]}'
        )

    with patch.object(ia_consenso, "_chamar_deepseek", side_effect=fake_ambiguidade):
        veredito = ia_consenso.resolver_ambiguidade_com_ia("ARROZ 5KG", "ARROZ 1KG")
    assert veredito["mesmo_produto"] is False, veredito
    assert veredito["veredito"] == "diferentes", veredito
    assert veredito["confianca"] == 0.88, veredito
    assert veredito["diferencas"] == ["5KG x 1KG"], veredito

    # Lista vazia é resolvida sem chamar a IA (economia de cota).
    with patch.object(
        ia_consenso, "_chamar_deepseek", side_effect=AssertionError("não deveria chamar")
    ):
        vazio = ia_consenso.validar_marcas_com_ia([])
    assert vazio["status"] == "vazio" and vazio["resultados"] == [], vazio

    # Sem DEEPSEEK_API_KEY o erro é controlado (a rota devolve 503 com instrução).
    with patch.object(ia_consenso, "_api_key", return_value=""):
        try:
            ia_consenso._chamar_deepseek([{"role": "user", "content": "teste"}])
        except ia_consenso.IAnaoconfigurada as erro:
            assert "DEEPSEEK_API_KEY" in str(erro), erro
            assert erro.status == 503, erro.status
        else:
            raise AssertionError("sem chave a IA deveria levantar IAnaoconfigurada")


@teste
def test_regra_volume_separa_chopp_20l_30l():
    """A chave inteligente separa volumes/pesos diferentes, mesmo com o mesmo EAN.

    O caso do Chopp é o teste de aceitação da regra: 20L e 30L são produtos
    diferentes. A regra também é reaplicada DEPOIS das regras de-para, então
    nem uma regra "juntar" consegue fundir os dois grupos.
    """
    assert bmain.extrair_volume("CHOPP 20L") == "20L"
    assert bmain.extrair_volume("CHOPP 30L") == "30L"
    assert bmain.extrair_volume("ARROZ 5KG") == "5KG"
    assert bmain.extrair_volume("ARROZ 1,5KG") == "1.5KG"
    assert bmain.extrair_volume("PACOTE 100G") == "100G"
    assert bmain.extrair_volume("REFRIGERANTE 12X350ML") == "4.2L"
    assert bmain.extrair_volume("CHOPP ARTESANAL") is None

    ean = "7891234567890"
    descricao_20 = "CHOPP ARTESANAL 20L"
    descricao_30 = "CHOPP ARTESANAL 30L"
    chave_20 = bmain._chave_produto_relatorio(ean, descricao_20)
    chave_30 = bmain._chave_produto_relatorio(ean, descricao_30)
    assert chave_20 != chave_30, (chave_20, chave_30)
    assert chave_20.endswith("_VOL_20L"), chave_20
    assert chave_30.endswith("_VOL_30L"), chave_30

    # Mesmo que alguém crie uma regra juntando 20L -> 30L, a chave final de
    # cada descrição volta para o sufixo da sua própria medida.
    mapa_forcado = {chave_20: chave_30}
    final_20 = bmain._chave_final_relatorio(chave_20, descricao_20, mapa_forcado, {})
    final_30 = bmain._chave_final_relatorio(chave_30, descricao_30, {}, {})
    assert final_20 != final_30, (final_20, final_30)
    assert final_20.endswith("_VOL_20L"), final_20
    assert final_30.endswith("_VOL_30L"), final_30


@teste
def test_regra_produto_marca_separa_polenta_arrico_el_chef():
    """PRODUTO + MARCA: Arrico e El Chef ficam em grupos separados; base agrupa.

    Também cobre:
    - marca nula e "NÃO IDENTIFICADA" usam o nome base;
    - números de controle (" - 1", " - 2") não criam grupos diferentes;
    - a regra é reaplicada depois de uma regra de-para forçada;
    - o catálogo MASTER lista TODAS as descrições separadas por Produto + Marca.
    """
    ean = "7891234567890"
    descricoes = [
        "POLENTA PALITO ARRICO 1 X 10KG - 1",
        "POLENTA PALITO ARRICO 1 X 10KG - 2",
        "POLENTA PALITO EL CHEF 1 X 10KG - 1",
        "POLENTA PALITO EL CHEF 1 X 10KG - 2",
    ]
    descricao_unica = "BATATA PALITO OUTRA 2KG"
    todas_descricoes = [*descricoes, descricao_unica]
    chave_arrico = bmain._chave_produto_relatorio(ean, descricoes[0], "ARRICO")
    chave_elchef = bmain._chave_produto_relatorio(ean, descricoes[2], "EL CHEF")
    assert chave_arrico != chave_elchef, (chave_arrico, chave_elchef)
    assert "_MARCA_ARRICO" in chave_arrico, chave_arrico
    assert "_MARCA_ELCHEF" in chave_elchef, chave_elchef

    # Mesmo que uma regra "juntar" tente fundir Arrico no grupo do El Chef, a
    # marca da própria descrição é reaplicada no final.
    final_arrico = bmain._chave_final_relatorio(
        chave_arrico, descricoes[0], {chave_arrico: chave_elchef}, {}, "ARRICO"
    )
    assert final_arrico != chave_elchef, final_arrico
    assert "_MARCA_ARRICO" in final_arrico, final_arrico

    # Sem marca identificada, o nome base (sem os números de controle) agrupa.
    sem_marca_1 = bmain._chave_produto_relatorio("", "POLENTA PALITO - 1", None)
    sem_marca_2 = bmain._chave_produto_relatorio(
        "", "POLENTA PALITO - 2", "NÃO IDENTIFICADA"
    )
    assert sem_marca_1 == sem_marca_2, (sem_marca_1, sem_marca_2)

    db, engine = _banco_aprendizado_temporario()
    try:
        empresa = Empresa(cnpj="12345678000199", nome_loja="Loja Marca")
        fornecedor = Fornecedor(cnpj="98765432000199", nome="Fornecedor Marca")
        db.add_all([empresa, fornecedor])
        db.commit()
        nota = NotaFiscal(
            chave_acesso="TESTE-MARCA-POLENTA",
            numero_nota="1",
            serie="1",
            data_emissao=date(2026, 1, 10),
            valor_total=40.0,
            empresa_id=empresa.id,
        )
        db.add(nota)
        db.commit()
        db.refresh(nota)
        marcas = ["ARRICO", "ARRICO", "EL CHEF", "EL CHEF"]
        for descricao, marca in zip(descricoes, marcas):
            valor = 10.0 if marca == "ARRICO" else 20.0
            db.add(
                ItemNota(
                    nota_id=nota.id,
                    fornecedor_id=fornecedor.id,
                    ean=ean,
                    descricao_produto=descricao,
                    quantidade=1.0,
                    valor_unitario=valor,
                    valor_total=valor,
                    ncm="19049000",
                    marca=marca,
                )
            )
        # Produto com UMA única descrição: o catálogo MASTER não pode descartá-lo.
        db.add(
            ItemNota(
                nota_id=nota.id,
                fornecedor_id=fornecedor.id,
                ean="7891234567891",
                descricao_produto=descricao_unica,
                quantidade=1.0,
                valor_unitario=8.0,
                valor_total=8.0,
                ncm="19049000",
                marca="OUTRA",
            )
        )
        db.commit()

        mapa_regras, mapa_descricoes = bmain._mapas_regras_relatorio(db)
        grupos = bmain._grupos_produto_relatorio(db, mapa_regras, mapa_descricoes)
        assert len(grupos) == 3, grupos
        assert {grupo.get("marca") for grupo in grupos.values()} == {
            "ARRICO",
            "EL CHEF",
            "OUTRA",
        }

        # Os relatórios usam a mesma chave: as médias de Arrico e El Chef ficam
        # separadas em `_carregar_itens_relatorio`.
        itens = bmain._carregar_itens_relatorio(db)
        chaves_polenta = {
            item["chave"]
            for item in itens
            if item["descricao"].startswith("POLENTA PALITO")
        }
        assert len(chaves_polenta) == 2, chaves_polenta
        assert all(
            "MARCA_ARRICO" in chave or "MARCA_ELCHEF" in chave
            for chave in chaves_polenta
        ), chaves_polenta
        medias = {}
        for item in itens:
            if item["chave"] in chaves_polenta:
                medias.setdefault(item["chave"], []).append(item)
        assert sorted(
            round(bmain._media_preco(grupo), 2) for grupo in medias.values()
        ) == [
            10.0,
            20.0,
        ], medias

        catalogo = bmain._linhas_catalogo_completo(db)
        assert {linha["Produto (Marca)"] for linha in catalogo} == {
            "Polenta Palito (Arrico)",
            "Polenta Palito (El Chef)",
            "Batata Palito (Outra)",
        }, catalogo
        assert {linha["Descrição Original"] for linha in catalogo} == set(todas_descricoes)
        assert len(catalogo) == 5, catalogo

        excel = bmain._gerar_excel_catalogo_completo(catalogo)
        workbook = load_workbook(BytesIO(excel))
        planilha = workbook[bmain.NOME_PLANILHA_CATALOGO]
        assert planilha.max_row == 6, planilha.max_row  # cabeçalho + 5 descrições
        tabela = pd.read_excel(BytesIO(excel), dtype=str, keep_default_na=False)
        assert list(tabela.columns) == bmain.COLUNAS_EXPORTACAO_CATALOGO
        assert set(tabela["Descrição Original"]) == set(todas_descricoes)
    finally:
        db.close()
        engine.dispose()


@teste
def test_marca_inferida_separa_polenta_arrico_el_chef():
    """A marca é inferida do item (sem número de controle) e separa Arrico/El Chef."""
    from backend import marcas as bmarcas

    descricoes = [
        "POLENTA PALITO 12X1 ARRICO CONG CX F=12 - 1",
        "POLENTA PALITO 12X1 ARRICO CONG CX F=12 - 2",
        "POLENTA PALITO 12X1 ARRICO CONG CX F=12 - 3",
        "POLENTA PALITO 12X1 ARRICO CONG CX F=12 - 4",
        "POLENTA PALITO 6X2 EL CHEF CONG - 2",
        "POLENTA PALITO 12 X 1 KG",
        "POLENTA PALITO 10X1 KG",
    ]
    db, engine = _banco_aprendizado_temporario()
    try:
        empresa = Empresa(cnpj="12345678000199", nome_loja="Loja Inferência")
        fornecedor = Fornecedor(cnpj="98765432000199", nome="Fornecedor Inferência")
        db.add_all([empresa, fornecedor])
        db.commit()
        nota = NotaFiscal(
            chave_acesso="TESTE-MARCA-INFERIDA",
            numero_nota="1",
            serie="1",
            data_emissao=date(2026, 1, 10),
            valor_total=70.0,
            empresa_id=empresa.id,
        )
        db.add(nota)
        db.commit()
        db.refresh(nota)
        for descricao in descricoes:
            db.add(
                ItemNota(
                    nota_id=nota.id,
                    fornecedor_id=fornecedor.id,
                    ean="SEM_EAN",
                    descricao_produto=descricao,
                    quantidade=1.0,
                    valor_unitario=10.0,
                    valor_total=10.0,
                    ncm="19019090",
                )
            )
        db.commit()

        resumo = bmarcas.recalcular_marcas(db)
        assert resumo["com_marca"] >= 5, resumo
        marcas_gravadas = {
            item.descricao_produto: item.marca
            for item in db.query(ItemNota).all()
        }
        assert {
            marca for descricao, marca in marcas_gravadas.items() if "ARRICO" in descricao
        } == {"ARRICO"}, marcas_gravadas
        assert {
            marca for descricao, marca in marcas_gravadas.items() if "EL CHEF" in descricao
        } == {"CHEF"}, marcas_gravadas

        mapa_regras, mapa_descricoes = bmain._mapas_regras_relatorio(db)
        grupos = bmain._grupos_produto_relatorio(db, mapa_regras, mapa_descricoes)
        grupos_polenta = {
            chave: grupo
            for chave, grupo in grupos.items()
            if any("POLENTA PALITO" in d for d in grupo["descricoes"])
        }
        marcas_grupos = {grupo.get("marca") for grupo in grupos_polenta.values()}
        assert {"ARRICO", "CHEF"} <= marcas_grupos, grupos_polenta

        catalogo = bmain._linhas_catalogo_completo(db)
        rotulos = {linha["Produto (Marca)"] for linha in catalogo}
        assert "Polenta Palito (Arrico)" in rotulos, rotulos
        # O token inferido é CHEF, mas o rótulo preserva o "EL" da descrição.
        assert "Polenta Palito (El Chef)" in rotulos, rotulos
    finally:
        db.close()
        engine.dispose()


@teste
def test_ia_prompt_trata_marca_como_criterio_de_separacao():
    """O prompt da IA manda separar marcas diferentes, produto e volume."""
    from unittest.mock import patch

    capturado = {}

    def fake(mensagens, temperatura=0.0, max_tokens=900):
        capturado["prompt"] = mensagens[-1]["content"]
        return json.dumps(
            {
                "mesmo_grupo": True,
                "manter": "POLENTA PALITO ARRICO 1 X 10KG",
                "sair": [],
                "motivo": "Mesmo produto, mesma marca e mesmo volume.",
                "confianca": 0.95,
            },
            ensure_ascii=False,
        )

    with patch.object(ia_consenso, "_chamar_deepseek", side_effect=fake):
        resultado = ia_consenso.sugerir_correcao_grupo(
            [
                "POLENTA PALITO ARRICO 1 X 10KG",
                "POLENTA PALITO ARRICO PACOTE 1 X 10",
            ],
            "POLENTA PALITO ARRICO 1 X 10KG",
        )

    prompt = capturado["prompt"]
    assert "É CRITÉRIO DE SEPARAÇÃO" in prompt, prompt
    assert "ARRICO x EL CHEF" in prompt, prompt
    assert "COCA-COLA x PEPSI" in prompt, prompt
    assert "QUEIJO MINAS CAMANDUCAIA x QUEIJO MINAS IPANEMA" in prompt, prompt
    assert "NUNCA diga que marca não é critério de separação" in prompt, prompt
    assert "marca diferente, porém, é motivo suficiente para separar" in prompt, prompt
    assert "POLENTA x BATATA" in prompt, prompt
    assert "CHOPP 20L x CHOPP 30L" in prompt, prompt
    assert resultado["mesmo_grupo"] is True, resultado
    assert resultado["sugestoes"] == [], resultado


@teste
def test_ia_remove_numeros_de_controle_e_deduplica_descricoes():
    """A IA recebe a descrição sem "- 1"/"- 2" e cada produto aparece uma única vez.

    Antes, as quatro variações de "POLENTA ... - 1..4" iam para o prompt e a IA
    devolvia quatro sugestões equivalentes (os cards repetidos da tela).
    """
    from unittest.mock import patch

    capturado = {}

    def fake(mensagens, temperatura=0.0, max_tokens=900):
        capturado["prompt"] = mensagens[-1]["content"]
        return json.dumps(
            {
                "mesmo_grupo": False,
                "manter": "POLENTA PALITO ARRICO CONG CX F=12",
                "sair": ["POLENTA PALITO EL CHEF CONG CX"],
                "motivo": "Marca diferente: Arrico x El Chef.",
                "confianca": 0.99,
            },
            ensure_ascii=False,
        )

    descricoes = [
        "POLENTA PALITO ARRICO CONG CX F=12 - 1",
        "POLENTA PALITO ARRICO CONG CX F=12 - 2",
        "POLENTA PALITO ARRICO CONG CX F=12 - 3",
        "POLENTA PALITO ARRICO CONG CX F=12 - 4",
        "POLENTA PALITO EL CHEF CONG CX",
    ]
    with patch.object(ia_consenso, "_chamar_deepseek", side_effect=fake):
        resultado = ia_consenso.sugerir_correcao_grupo(
            descricoes, "POLENTA PALITO ARRICO CONG CX F=12 - 1"
        )

    prompt = capturado["prompt"]
    assert "POLENTA PALITO ARRICO CONG CX F=12 - 1" not in prompt, prompt
    assert "POLENTA PALITO ARRICO CONG CX F=12 - 4" not in prompt, prompt
    assert resultado["descricoes"] == [
        "POLENTA PALITO ARRICO CONG CX F=12",
        "POLENTA PALITO EL CHEF CONG CX",
    ], resultado
    assert [s["item_sugerido_sair"] for s in resultado["sugestoes"]] == [
        "POLENTA PALITO EL CHEF CONG CX"
    ], resultado
    assert ia_consenso.remover_numero_controle("POLENTA - 12") == "POLENTA"


@teste
def test_ia_sugestao_em_grupo_ignora_descricao_inventada():
    """Parser da sugestão em grupo: só aceita descrições que existem no grupo."""
    from unittest.mock import patch

    def fake(mensagens, temperatura=0.0, max_tokens=900):
        assert mensagens and mensagens[0]["role"] == "system"
        return json.dumps(
            {
                "mesmo_grupo": False,
                "manter": "CHOPP ARTESANAL 20L",
                "sair": ["CHOPP ARTESANAL 30L", "DESCRIÇÃO INVENTADA"],
                "motivo": "O volume muda o produto.",
                "confianca": 0.98,
            },
            ensure_ascii=False,
        )

    with patch.object(ia_consenso, "_chamar_deepseek", side_effect=fake):
        resultado = ia_consenso.sugerir_correcao_grupo(
            ["CHOPP ARTESANAL 20L", "CHOPP ARTESANAL 30L"],
            "CHOPP ARTESANAL 20L",
        )
    assert resultado["status"] == "sucesso", resultado
    assert resultado["mesmo_grupo"] is False, resultado
    assert resultado["manter"] == "CHOPP ARTESANAL 20L", resultado
    assert [s["item_sugerido_sair"] for s in resultado["sugestoes"]] == [
        "CHOPP ARTESANAL 30L"
    ], resultado
    assert "volume" in resultado["motivo"].lower(), resultado


@teste
def test_auditar_ia_deduplica_equivalentes_e_aplicar_move_todas_variacoes():
    """Quatro "POLENTA - 1..4" geram UMA sugestão e o Aplicar move as quatro.

    Repete a rodada para garantir que a sugestão pendente existente é atualizada em
    vez de duplicada. As regras de-para são gravadas para cada variação real do grupo,
    então nenhuma delas volta como sugestão na próxima auditoria.
    """
    from datetime import date as _date

    db, engine = _banco_aprendizado_temporario()
    try:
        empresa = Empresa(cnpj="12345678000199", nome_loja="Loja Dedupe IA")
        fornecedor = Fornecedor(cnpj="98765432000199", nome="Fornecedor Dedupe IA")
        db.add_all([empresa, fornecedor])
        db.commit()
        nota = NotaFiscal(
            chave_acesso="TESTE-IA-DEDUPE-POLENTA",
            data_emissao=_date(2026, 1, 10),
            empresa_id=empresa.id,
        )
        db.add(nota)
        db.commit()
        arrico = [
            f"POLENTA PALITO ARRICO CONG CX F=12 - {numero}"
            for numero in range(1, 5)
        ]
        el_chef = "POLENTA PALITO EL CHEF CONG CX"
        for descricao in [*arrico, el_chef]:
            db.add(
                ItemNota(
                    nota_id=nota.id,
                    fornecedor_id=fornecedor.id,
                    ean="7891234567890",
                    descricao_produto=descricao,
                    quantidade=1.0,
                    valor_unitario=10.0,
                    valor_total=10.0,
                    ncm="19049000",
                )
            )
        db.commit()

        def fake(descricoes, padrao=None):
            return {
                "sugestoes": [
                    {
                        "item_sugerido_sair": descricao,
                        "motivo": "Marca diferente: Arrico x El Chef.",
                    }
                    for descricao in arrico
                ]
            }

        original = bmain.sugerir_correcao_com_ia
        bmain.sugerir_correcao_com_ia = fake
        try:
            primeira = bmain._auditar_base_completa(db)
            segunda = bmain._auditar_base_completa(db)
        finally:
            bmain.sugerir_correcao_com_ia = original

        assert primeira["sugestoes_criadas"] == 1, primeira
        assert segunda["sugestoes_criadas"] == 0, segunda
        assert segunda["sugestoes_atualizadas"] == 1, segunda
        pendentes = db.query(SugestaoCorrecaoIA).all()
        assert len(pendentes) == 1, pendentes
        sugestao = pendentes[0]
        assert bmain._equivalencia_descricao(
            sugestao.item_sugerido_sair
        ) == bmain._equivalencia_descricao(arrico[0])

        aplicado = bmain._aplicar_sugestao_ia(db, sugestao)
        assert aplicado["status"] == "sucesso", aplicado
        assert "4 descrições equivalentes movidas" in aplicado["mensagem"], aplicado
        regras = db.query(RegraAgrupamento).all()
        assert len(regras) == len(arrico), regras
        assert all(regra.tipo == "mover" for regra in regras), regras

        mapa_regras, mapa_descricoes = bmain._mapas_regras_relatorio(db)
        grupos_depois = bmain._grupos_produto_relatorio(
            db, mapa_regras, mapa_descricoes
        )
        grupos_arrico = [
            grupo
            for grupo in grupos_depois.values()
            if any("ARRICO" in descricao for descricao in grupo["descricoes"])
        ]
        assert len(grupos_arrico) == 1, grupos_arrico
        assert set(grupos_arrico[0]["descricoes"]) == set(arrico), grupos_arrico
    finally:
        db.close()
        engine.dispose()


@teste
def test_listar_sugestoes_ia_esconde_duplicadas_e_desatualizadas():
    """A listagem mostra um card por descrição e ignora chaves de grupo antigas."""
    from datetime import date as _date

    db, engine = _banco_aprendizado_temporario()
    try:
        empresa = Empresa(cnpj="12345678000199", nome_loja="Loja Lista IA")
        fornecedor = Fornecedor(cnpj="98765432000199", nome="Fornecedor Lista IA")
        db.add_all([empresa, fornecedor])
        db.commit()
        nota = NotaFiscal(
            chave_acesso="TESTE-IA-LISTA-DEDUPE",
            data_emissao=_date(2026, 1, 10),
            empresa_id=empresa.id,
        )
        db.add(nota)
        db.commit()
        zero = "COCA COLA ZERO LT 350 ML C/12"
        normal = "COCA COLA NORMAL LT 350 ML C/12"
        for descricao in (zero, normal):
            db.add(
                ItemNota(
                    nota_id=nota.id,
                    fornecedor_id=fornecedor.id,
                    ean="7894900701326",
                    descricao_produto=descricao,
                    quantidade=1.0,
                    valor_unitario=5.0,
                    valor_total=5.0,
                    ncm="22021000",
                )
            )
        db.commit()

        mapa_regras, mapa_descricoes = bmain._mapas_regras_relatorio(db)
        grupos = bmain._grupos_produto_relatorio(db, mapa_regras, mapa_descricoes)
        chave_atual = next(
            chave for chave, grupo in grupos.items() if zero in grupo["descricoes"]
        )
        db.add_all(
            [
                SugestaoCorrecaoIA(
                    grupo_origem=chave_atual,
                    item_sugerido_sair=zero,
                    motivo_ia="Sabor/variedade diferente.",
                    status_aprovacao="pendente",
                ),
                SugestaoCorrecaoIA(
                    grupo_origem="SEM_EAN_COCA_COLA_ANTIGA",
                    item_sugerido_sair=zero,
                    motivo_ia="Sugestão duplicada da chave antiga.",
                    status_aprovacao="pendente",
                ),
                SugestaoCorrecaoIA(
                    grupo_origem="SEM_EAN_QUEIJO_ANTIGA",
                    item_sugerido_sair="QUEIJO MINAS FRESCAL CAMANDUCAIA",
                    motivo_ia="Sugestão sem grupo atual.",
                    status_aprovacao="pendente",
                ),
            ]
        )
        db.commit()

        resultado = bmain.listar_sugestoes_ia(
            status_aprovacao="pendente", admin=None, db=db
        )
        assert resultado["total"] == 1, resultado
        assert resultado["sugestoes"][0]["grupo_origem"] == chave_atual, resultado
        assert resultado["sugestoes"][0]["item_sugerido_sair"] == zero, resultado

        # A rodada de IA também limpa a base: sobra uma pendente e as antigas viram
        # "ignorada" (não voltam como card nem são recriadas).
        ignoradas = bmain._consolidar_sugestoes_pendentes(db, grupos)
        assert ignoradas == 2, ignoradas
        assert (
            db.query(SugestaoCorrecaoIA)
            .filter(SugestaoCorrecaoIA.status_aprovacao == "pendente")
            .count()
            == 1
        )
    finally:
        db.close()
        engine.dispose()


@teste
def test_sugestao_ia_so_altera_base_no_aplicar():
    """Auditar grava sugestão pendente; Aplicar cria a regra e move só um item."""
    from datetime import date as _date

    db, engine = _banco_aprendizado_temporario()
    try:
        empresa = Empresa(cnpj="12345678000199", nome_loja="Loja IA")
        fornecedor = Fornecedor(cnpj="98765432000199", nome="Fornecedor IA")
        db.add_all([empresa, fornecedor])
        db.commit()
        nota = NotaFiscal(
            chave_acesso="TESTE-IA-SUGESTAO-1",
            data_emissao=_date(2026, 1, 10),
            empresa_id=empresa.id,
        )
        db.add(nota)
        db.commit()
        descricoes = [
            "REFRIGERANTE COCA COLA ZERO",
            "REFRIGERANTE COCA COLA NORMAL",
        ]
        for descricao in descricoes:
            db.add(
                ItemNota(
                    nota_id=nota.id,
                    fornecedor_id=fornecedor.id,
                    ean="7891234567890",
                    descricao_produto=descricao,
                    quantidade=1.0,
                    valor_unitario=5.0,
                    valor_total=5.0,
                    ncm="22021000",
                )
            )
        db.commit()

        mapa_regras, mapa_descricoes = bmain._mapas_regras_relatorio(db)
        grupos = bmain._grupos_produto_relatorio(db, mapa_regras, mapa_descricoes)
        assert len(grupos) == 1, grupos
        grupo_origem = next(iter(grupos))
        assert set(grupos[grupo_origem]["descricoes"]) == set(descricoes)

        original = bmain.sugerir_correcao_com_ia
        bmain.sugerir_correcao_com_ia = lambda descricoes, padrao=None: {
            "sugestoes": [
                {
                    "item_sugerido_sair": "REFRIGERANTE COCA COLA NORMAL",
                    "motivo": "Variedade diferente.",
                }
            ]
        }
        try:
            resultado = bmain._auditar_base_completa(db)
        finally:
            bmain.sugerir_correcao_com_ia = original

        assert resultado["sugestoes_criadas"] == 1, resultado
        assert db.query(SugestaoCorrecaoIA).count() == 1
        # Antes do Aplicar, nenhuma regra nem aprovação foi criada.
        assert db.query(RegraAgrupamento).count() == 0
        assert db.query(ItemAuditado).count() == 0

        sugestao = db.query(SugestaoCorrecaoIA).first()
        aplicado = bmain._aplicar_sugestao_ia(db, sugestao)
        assert aplicado["status"] == "sucesso", aplicado
        assert sugestao.status_aprovacao == "aprovada", aplicado
        assert db.query(RegraAgrupamento).count() == 1
        regra = db.query(RegraAgrupamento).first()
        assert regra.tipo == "mover", regra.tipo
        assert "REFRIGERANTE COCA COLA NORMAL" in regra.chave_origem
        assert regra.chave_destino.startswith("NOVO_"), regra.chave_destino

        mapa_regras, mapa_descricoes = bmain._mapas_regras_relatorio(db)
        grupos_depois = bmain._grupos_produto_relatorio(db, mapa_regras, mapa_descricoes)
        grupos_com_normal = [
            chave
            for chave, grupo in grupos_depois.items()
            if "REFRIGERANTE COCA COLA NORMAL" in grupo["descricoes"]
        ]
        assert len(grupos_com_normal) == 1, grupos_depois
        assert grupos_com_normal[0].startswith("NOVO_"), grupos_com_normal
        assert len(grupos_depois[grupo_origem]["descricoes"]) == 1, grupos_depois
        assert db.query(ItemNota).count() == 2  # nenhuma compra foi apagada
    finally:
        db.close()
        engine.dispose()


@teste
def test_sugestao_ia_aplicar_em_grupo_existente():
    """Aplicar com `chave_destino` move para o grupo escolhido (sem criar NOVO_)."""
    from datetime import date as _date

    db, engine = _banco_aprendizado_temporario()
    try:
        empresa = Empresa(cnpj="12345678000199", nome_loja="Loja IA")
        fornecedor = Fornecedor(cnpj="98765432000199", nome="Fornecedor IA")
        db.add_all([empresa, fornecedor])
        db.commit()
        nota = NotaFiscal(
            chave_acesso="TESTE-IA-DESTINO-EXISTENTE",
            data_emissao=_date(2026, 1, 10),
            empresa_id=empresa.id,
        )
        db.add(nota)
        db.commit()
        itens = [
            ("REFRIGERANTE COCA COLA ZERO", "7891234567890"),
            ("REFRIGERANTE COCA COLA NORMAL", "7891234567890"),
            ("REFRIGERANTE PEPSI", "7891234567891"),
        ]
        for descricao, ean in itens:
            db.add(
                ItemNota(
                    nota_id=nota.id,
                    fornecedor_id=fornecedor.id,
                    ean=ean,
                    descricao_produto=descricao,
                    quantidade=1.0,
                    valor_unitario=5.0,
                    valor_total=5.0,
                    ncm="22021000",
                )
            )
        db.commit()

        mapa_regras, mapa_descricoes = bmain._mapas_regras_relatorio(db)
        grupos = bmain._grupos_produto_relatorio(db, mapa_regras, mapa_descricoes)
        assert len(grupos) == 2, grupos
        origem = next(
            chave
            for chave, grupo in grupos.items()
            if "REFRIGERANTE COCA COLA NORMAL" in grupo["descricoes"]
        )
        destino = next(
            chave
            for chave, grupo in grupos.items()
            if "REFRIGERANTE PEPSI" in grupo["descricoes"]
        )
        sugestao = SugestaoCorrecaoIA(
            grupo_origem=origem,
            item_sugerido_sair="REFRIGERANTE COCA COLA NORMAL",
            motivo_ia="Produto diferente.",
            status_aprovacao="pendente",
        )
        db.add(sugestao)
        db.commit()

        aplicado = bmain._aplicar_sugestao_ia(db, sugestao, chave_destino=destino)
        assert aplicado["status"] == "sucesso", aplicado
        assert "NOVO_" not in aplicado["mensagem"], aplicado["mensagem"]

        regra = db.query(RegraAgrupamento).first()
        assert regra is not None and regra.tipo == "mover", regra
        assert regra.chave_destino == destino, regra.chave_destino
        assert "REFRIGERANTE COCA COLA NORMAL" in regra.chave_origem

        mapa_regras, mapa_descricoes = bmain._mapas_regras_relatorio(db)
        grupos_depois = bmain._grupos_produto_relatorio(db, mapa_regras, mapa_descricoes)
        assert any(
            "REFRIGERANTE COCA COLA NORMAL" in grupo["descricoes"]
            for chave, grupo in grupos_depois.items()
            if chave == destino
        ), grupos_depois
        assert not any(chave.startswith("NOVO_") for chave in grupos_depois), grupos_depois
        assert "REFRIGERANTE COCA COLA NORMAL" not in grupos_depois[origem]["descricoes"]
        assert db.query(ItemNota).count() == 3  # nenhuma compra foi apagada
    finally:
        db.close()
        engine.dispose()


@teste
def test_api_ia_rotas_exigem_2fa_e_orientam_sem_chave():
    """As rotas de IA são de admin+2FA e, sem chave, respondem 503 (não 500).

    Com a chave configurada o teste NÃO chama a DeepSeek de verdade (evita custo/rede
    externa): valida apenas a barreira de autorização e a validação de payload.
    """
    with conta_admin_teste("Admin IA") as (_usuario, token):
        sem_sessao = requests.post(
            BASE_API + "/api/auditoria/ia/resolver-ambiguidade",
            json={"texto1": "ARROZ 5KG", "texto2": "ARROZ 1KG"},
            timeout=30,
        )
        assert sem_sessao.status_code == 401, sem_sessao.text[:300]

        # Auditoria em massa e sugestões também são admin + 2FA.
        sem_sessao_sugestoes = requests.get(BASE_API + "/api/ia/sugestoes", timeout=30)
        assert sem_sessao_sugestoes.status_code == 401, sem_sessao_sugestoes.text[:300]
        sem_sessao_auditar = requests.post(
            BASE_API + "/api/ia/auditar-base-completa",
            json={"limite": 1},
            timeout=30,
        )
        assert sem_sessao_auditar.status_code == 401, sem_sessao_auditar.text[:300]

        payload_vazio = requests.post(
            BASE_API + "/api/auditoria/ia/resolver-ambiguidade",
            json={"texto1": "", "texto2": ""},
            headers=_cabecalho_admin(token),
            timeout=30,
        )
        assert payload_vazio.status_code == 422, payload_vazio.text[:300]

        lista_vazia = requests.post(
            BASE_API + "/api/auditoria/ia/validar-marcas",
            json={"palavras": []},
            headers=_cabecalho_admin(token),
            timeout=30,
        )
        assert lista_vazia.status_code == 422, lista_vazia.text[:300]

        if not ia_consenso.configurada():
            sem_chave = requests.post(
                BASE_API + "/api/auditoria/ia/resolver-ambiguidade",
                json={"texto1": "ARROZ 5KG", "texto2": "ARROZ 1KG"},
                headers=_cabecalho_admin(token),
                timeout=60,
            )
            assert sem_chave.status_code == 503, sem_chave.text[:300]
            assert "DEEPSEEK_API_KEY" in sem_chave.text, sem_chave.text[:300]


@teste
def test_api_exportar_grupos_completos_gera_excel():
    """GET /api/relatorios/exportar-grupos-completos devolve um .xlsx válido.

    Confere a barreira de admin+2FA, o nome/conteúdo do arquivo e as colunas pedidas.
    Todos os grupos exportados têm 2+ descrições e o total bate com a varredura
    completa do backend (`_linhas_grupos_com_variacoes`).
    """
    with conta_admin_teste("Admin Exportacao") as (_usuario, token):
        sem_sessao = requests.get(
            BASE_API + "/api/relatorios/exportar-grupos-completos", timeout=30
        )
        assert sem_sessao.status_code == 401, sem_sessao.text[:300]

        resposta = requests.get(
            BASE_API + "/api/relatorios/exportar-grupos-completos",
            headers=_cabecalho_admin(token),
            timeout=300,
        )
        assert resposta.status_code == 200, resposta.text[:300]
        assert "spreadsheetml.sheet" in resposta.headers.get("Content-Type", ""), (
            resposta.headers
        )
        assert ".xlsx" in resposta.headers.get("Content-Disposition", ""), (
            resposta.headers
        )
        assert resposta.content[:2] == b"PK", "o Excel deve começar com a assinatura ZIP"

        total = int(resposta.headers["X-Total-Grupos"])
        workbook = load_workbook(BytesIO(resposta.content))
        planilha = workbook[bmain.NOME_PLANILHA_GRUPOS]
        assert planilha.max_row == total + 1, (planilha.max_row, total)
        assert planilha.freeze_panes == "A2", planilha.freeze_panes
        assert planilha.auto_filter.ref, "o cabeçalho deve ter filtro automático"

        dataframe = pd.read_excel(
            BytesIO(resposta.content), dtype=str, keep_default_na=False
        )
        assert list(dataframe.columns) == bmain.COLUNAS_EXPORTACAO_GRUPOS, list(
            dataframe.columns
        )
        assert len(dataframe) == total, (len(dataframe), total)
        assert (dataframe["Produto Principal"].str.strip() != "").all()
        assert (dataframe["EAN"].str.strip() != "").all()
        assert (dataframe["NCM"].str.strip() != "").all()
        if not dataframe.empty:
            variacoes = dataframe["Lista de Descrições"].str.split("; ")
            assert (variacoes.str.len() >= 2).all(), dataframe.head().to_dict("records")
            # NCM é texto no arquivo: zeros à esquerda não podem desaparecer.
            com_zero = dataframe[dataframe["NCM"].str.startswith("0")]
            assert (com_zero["NCM"].str.len() == 8).all(), com_zero["NCM"].tolist()

        db = SessionLocal()
        try:
            esperado = bmain._linhas_grupos_com_variacoes(db)
        finally:
            db.close()
        assert total == len(esperado), (total, len(esperado))
        colunas = [
            "Produto Principal",
            "EAN",
            "NCM",
            "Lista de Descrições",
        ]
        assert [tuple(linha[coluna] for coluna in colunas) for linha in esperado] == list(
            dataframe[colunas].itertuples(index=False, name=None)
        ), "o Excel precisa refletir a varredura completa do backend"


@teste
def test_api_exportar_catalogo_completo_gera_excel():
    """GET /api/relatorios/exportar-base-completa: catálogo MASTER (admin + 2FA)."""
    with conta_admin_teste("Admin Catalogo") as (_usuario, token):
        sem_sessao = requests.get(
            BASE_API + "/api/relatorios/exportar-base-completa", timeout=30
        )
        assert sem_sessao.status_code == 401, sem_sessao.text[:300]

        resposta = requests.get(
            BASE_API + "/api/relatorios/exportar-base-completa",
            headers=_cabecalho_admin(token),
            timeout=300,
        )
        assert resposta.status_code == 200, resposta.text[:300]
        assert "spreadsheetml.sheet" in resposta.headers.get("Content-Type", "")
        assert ".xlsx" in resposta.headers.get("Content-Disposition", "")
        assert resposta.content[:2] == b"PK", "o Excel deve começar com a assinatura ZIP"

        workbook = load_workbook(BytesIO(resposta.content))
        planilha = workbook[bmain.NOME_PLANILHA_CATALOGO]
        assert planilha.freeze_panes == "A2", planilha.freeze_panes
        assert planilha.auto_filter.ref, "o cabeçalho deve ter filtro automático"

        tabela = pd.read_excel(
            BytesIO(resposta.content), dtype=str, keep_default_na=False
        )
        assert list(tabela.columns) == bmain.COLUNAS_EXPORTACAO_CATALOGO, list(
            tabela.columns
        )
        assert len(tabela) == int(resposta.headers["X-Total-Descricoes"])
        if not tabela.empty:
            assert (tabela["Produto (Marca)"].str.strip() != "").all()
            assert (tabela["Marca"].str.strip() != "").all(), "coluna Marca vazia"
            assert (tabela["Descrição Original"].str.strip() != "").all()

        db = SessionLocal()
        try:
            esperado = bmain._linhas_catalogo_completo(db)
        finally:
            db.close()
        assert len(tabela) == len(esperado), (len(tabela), len(esperado))
        assert list(
            tabela[
                ["Produto (Marca)", "EAN", "NCM", "Descrição Original"]
            ].itertuples(index=False, name=None)
        ) == [
            (
                linha["Produto (Marca)"],
                linha["EAN"],
                linha["NCM"],
                linha["Descrição Original"],
            )
            for linha in esperado
        ]


@teste
def test_apptest_exportacao_base_completa():
    """AppTest: aba 📥 Exportar Base gera o arquivo e mostra o botão de download."""
    with app_test_logado(admin=True) as app:
        app.radio(key="menu_principal").set_value("🔍 Auditoria (Admin)").run()
        assert not app.exception, f"Auditoria levantou exceção: {app.exception}"

        rotulos = [aba.label for aba in app.tabs]
        assert "📥 Exportar Base" in rotulos, rotulos
        botoes = [botao.label for botao in app.button]
        assert any(
            "Preparar arquivo da base completa" in (rotulo or "") for rotulo in botoes
        ), botoes

        app.button(key="btn_preparar_exportacao_grupos").click().run()
        assert not app.exception, (
            f"Preparação da exportação levantou exceção: {app.exception}"
        )

        exportacao = app.session_state["_auditoria_exportacao_grupos"]
        assert exportacao["conteudo"][:2] == b"PK", exportacao["conteudo"][:20]
        assert exportacao["nome"].endswith(".xlsx"), exportacao["nome"]
        assert int(exportacao["total"]) >= 0, exportacao["total"]

        mensagens = " ".join(elemento.value for elemento in app.success)
        assert "Arquivo pronto" in mensagens, mensagens

        rotulos_download = [botao.label for botao in app.download_button]
        assert any(
            "Exportar Base Completa para Excel" in (rotulo or "")
            for rotulo in rotulos_download
        ), rotulos_download

        # Catálogo MASTER (Produto + Marca): botão próprio e download com o label pedido.
        botoes = [botao.label for botao in app.button]
        assert any(
            "Preparar catálogo completo" in (rotulo or "") for rotulo in botoes
        ), botoes
        app.button(key="btn_preparar_exportacao_catalogo").click().run()
        assert not app.exception, (
            f"Preparação do catálogo levantou exceção: {app.exception}"
        )
        catalogo = app.session_state["_auditoria_exportacao_catalogo"]
        assert catalogo["conteudo"][:2] == b"PK", catalogo["conteudo"][:20]
        assert catalogo["nome"].endswith(".xlsx"), catalogo["nome"]
        assert int(catalogo["total_descricoes"]) >= 0, catalogo["total_descricoes"]

        rotulos_download = [botao.label for botao in app.download_button]
        assert any(
            "Exportar Catálogo Completo (Excel)" in (rotulo or "")
            for rotulo in rotulos_download
        ), rotulos_download


# ──────────────────────────────────────────────────────────────────────────────
# Memória de cache da IA de marcas (tabela marcas_ia)
# ──────────────────────────────────────────────────────────────────────────────
@teste
def test_memoria_marcas_ia_nao_repete_consulta():
    """Descrição já analisada volta do banco; só a inédita chama a DeepSeek.

    O teste substitui `ia_consenso._chamar_deepseek` (sem rede/cota) e confere os
    contadores usados pela tela: cache hit, consulta à IA, marca encontrada e "sem marca".
    """
    from unittest.mock import patch

    from backend.models import MarcaIA

    db, engine = _banco_aprendizado_temporario()
    chamadas = []

    def fake(mensagens, temperatura=0.0, max_tokens=900):
        prompt = mensagens[-1]["content"]
        chamadas.append(prompt)
        # Confere a descrição do item: o prompt cita YOKI nos exemplos de marcas válidas.
        if "PALHA DE MANDIOCA FINA YOKI" in prompt:
            return json.dumps({"marca": "YOKI"}, ensure_ascii=False)
        return json.dumps({"marca": "SEM MARCA"}, ensure_ascii=False)

    estatisticas = {}
    try:
        with patch.object(ia_consenso, "_chamar_deepseek", side_effect=fake):
            primeira = ia_consenso.obter_marca_inteligente(
                "PALHA DE MANDIOCA FINA YOKI - 1",
                db=db,
                estatisticas=estatisticas,
            )
            segunda = ia_consenso.obter_marca_inteligente(
                "PALHA DE MANDIOCA FINA YOKI - 2",
                db=db,
                estatisticas=estatisticas,
            )
            sem_marca = ia_consenso.obter_marca_inteligente(
                "ALFACE CRESPA", db=db, estatisticas=estatisticas
            )
            sem_marca_repetida = ia_consenso.obter_marca_inteligente(
                "ALFACE CRESPA", db=db, estatisticas=estatisticas
            )

        assert primeira == "YOKI", primeira
        assert segunda == "YOKI", segunda
        assert sem_marca == ia_consenso.MARCA_SEM_MARCA, sem_marca
        assert sem_marca_repetida == ia_consenso.MARCA_SEM_MARCA, sem_marca_repetida
        # A segunda YOKI e a segunda ALFACE saíram do cache: só 2 chamadas de verdade.
        assert len(chamadas) == 2, chamadas
        assert estatisticas == {
            "cache_misses": 2,
            "consultas_ia": 2,
            "marcas_encontradas": 2,
            "cache_hits": 2,
            "sem_marca": 2,
        }, estatisticas

        registros = db.query(MarcaIA).order_by(MarcaIA.id).all()
        assert [registro.descricao_original for registro in registros] == [
            "PALHA DE MANDIOCA FINA YOKI",
            "ALFACE CRESPA",
        ], [registro.descricao_original for registro in registros]
        assert [registro.marca_extraida for registro in registros] == [
            "YOKI",
            ia_consenso.MARCA_SEM_MARCA,
        ]
        assert all(registro.data_consulta for registro in registros), registros

        # Cache miss com a IA desligada devolve None (não confunde com "SEM MARCA") e não
        # grava nada: o recálculo em massa pode cair na heurística local.
        with patch.object(
            ia_consenso,
            "_chamar_deepseek",
            side_effect=AssertionError("não deveria chamar a IA"),
        ):
            assert (
                ia_consenso.obter_marca_inteligente(
                    "PRODUTO NOVO QUALQUER", db=db, usar_ia=False
                )
                is None
            )
        assert db.query(MarcaIA).count() == 2
    finally:
        db.close()
        engine.dispose()


@teste
def test_memoria_marcas_ia_rejeita_generico_e_usa_candidata():
    """A candidata entra no prompt, mas REFOR/TRANSP nunca são gravados como marca."""
    from unittest.mock import patch

    from backend.models import MarcaIA

    db, engine = _banco_aprendizado_temporario()
    prompts = []

    def fake(mensagens, temperatura=0.0, max_tokens=900):
        prompt = mensagens[-1]["content"]
        prompts.append(prompt)
        # Mesmo que a IA confirme o candidato genérico, a barreira final o rejeita.
        if "R14 REFOR" in prompt:
            return json.dumps({"marca": "REFOR"}, ensure_ascii=False)
        return json.dumps({"marca": "TRANSP"}, ensure_ascii=False)

    try:
        with patch.object(ia_consenso, "_chamar_deepseek", side_effect=fake):
            resultado = ia_consenso.obter_marca_inteligente(
                "SACO PRETO LIXO 200 - R14 REFOR BL 5KG",
                db=db,
                marca_candidata="REFOR",
            )
            repetida = ia_consenso.obter_marca_inteligente(
                "SACO PRETO LIXO 200 - R14 REFOR BL 5KG",
                db=db,
                marca_candidata="REFOR",
            )
            transporte = ia_consenso.obter_marca_inteligente(
                "CAIXA PAPELAO ONDA B 30X20X20 TRANSP 20UN",
                db=db,
                marca_candidata="TRANSP",
            )

        assert resultado == ia_consenso.MARCA_SEM_MARCA, resultado
        assert repetida == ia_consenso.MARCA_SEM_MARCA, repetida
        assert transporte == ia_consenso.MARCA_SEM_MARCA, transporte
        assert len(prompts) == 2, prompts
        assert 'marca candidata "REFOR"' in prompts[0], prompts[0]
        assert 'marca candidata "TRANSP"' in prompts[1], prompts[1]
        assert "NÃO são marcas" in prompts[0], prompts[0]
        registros = db.query(MarcaIA).order_by(MarcaIA.id).all()
        assert [registro.marca_extraida for registro in registros] == [
            ia_consenso.MARCA_SEM_MARCA,
            ia_consenso.MARCA_SEM_MARCA,
        ], [registro.marca_extraida for registro in registros]
    finally:
        db.close()
        engine.dispose()


@teste
def test_memoria_marcas_ia_completa_abreviacao_fta():
    """Abreviação conhecida (FTA = Fanta) é expandida antes de gravar no cache."""
    from unittest.mock import patch

    from backend.models import MarcaIA

    db, engine = _banco_aprendizado_temporario()
    prompts = []

    def fake(mensagens, temperatura=0.0, max_tokens=900):
        prompts.append(mensagens[-1]["content"])
        return json.dumps({"marca": "FTA"}, ensure_ascii=False)

    try:
        with patch.object(ia_consenso, "_chamar_deepseek", side_effect=fake):
            resultado = ia_consenso.obter_marca_inteligente(
                "FTA LAR LT 350ML 12 FL", db=db
            )
            repetida = ia_consenso.obter_marca_inteligente(
                "FTA LAR LT 350ML 12 FL", db=db
            )

        assert resultado == "FANTA", resultado
        assert repetida == "FANTA", repetida
        assert len(prompts) == 1, prompts
        registro = db.query(MarcaIA).one()
        assert registro.marca_extraida == "FANTA", registro.marca_extraida
    finally:
        db.close()
        engine.dispose()


@teste
def test_memoria_marcas_ia_prompt_completo_e_limpeza_do_cache():
    """Prompt recebe a descrição completa e o cache descarta termos genéricos."""
    from unittest.mock import patch

    from backend.models import MarcaIA

    prompt = ia_consenso._prompt_marca_inteligente(
        "CARNE RESFRIADA DE BOVINO SEM OSSO MAMINHA DA ALCATRA"
    )
    assert "CARNE RESFRIADA DE BOVINO SEM OSSO MAMINHA DA ALCATRA" in prompt, prompt
    assert "DESCRIÇÃO COMPLETA" in prompt, prompt
    assert "NÃO são marcas" in prompt, prompt
    assert "cortes de carne" in prompt and "TETRAPAK" in prompt, prompt
    assert "SEM MARCA" in prompt, prompt
    assert "Produto Sem Nome (Marca)" in prompt, prompt
    assert "parênteses" in prompt, prompt
    # Marcas reais que a IA vinha devolvendo como "SEM MARCA".
    for marca in (
        "SADIA",
        "SEARA",
        "PERDIGÃO",
        "TABASCO",
        "ARRIFANA",
        "VILLA PIVA",
        "IPANEMA",
        "CEPERA",
    ):
        assert marca in prompt, (marca, prompt)
    assert "SUCO VILLA PIVA" in prompt and "MOSTARDA CEPERA" in prompt, prompt

    db, engine = _banco_aprendizado_temporario()
    try:
        db.add_all(
            [
                MarcaIA(descricao_original="ITEM OSSO", marca_extraida="OSSO"),
                MarcaIA(descricao_original="ITEM TRANSP", marca_extraida="TRANSP"),
                MarcaIA(descricao_original="ITEM TETRA", marca_extraida="TETRAPAK"),
                MarcaIA(descricao_original="ITEM YOKI", marca_extraida="YOKI"),
                MarcaIA(
                    descricao_original="ITEM SEM MARCA",
                    marca_extraida=ia_consenso.MARCA_SEM_MARCA,
                ),
            ]
        )
        db.commit()
        assert ia_consenso.limpar_memoria_marcas_invalidas(db) == 3
        restantes = {
            registro.descricao_original: registro.marca_extraida
            for registro in db.query(MarcaIA).all()
        }
        assert restantes == {
            "ITEM YOKI": "YOKI",
            "ITEM SEM MARCA": ia_consenso.MARCA_SEM_MARCA,
        }, restantes

        # Cache envenenado (RESF) é apagado e a IA é chamada de novo para a mesma descrição.
        db.add(MarcaIA(descricao_original="LA DE ACO ASSOLAN 45G", marca_extraida="RESF"))
        db.commit()
        with patch.object(
            ia_consenso,
            "_chamar_deepseek",
            return_value=json.dumps({"marca": "ASSOLAN"}, ensure_ascii=False),
        ) as chamada:
            marca = ia_consenso.obter_marca_inteligente("LA DE ACO ASSOLAN 45G", db=db)
        assert marca == "ASSOLAN", marca
        assert chamada.call_count == 1, chamada.call_count
        assert (
            db.query(MarcaIA)
            .filter(MarcaIA.descricao_original == "LA DE ACO ASSOLAN 45G")
            .one()
            .marca_extraida
            == "ASSOLAN"
        )

        # "Produto Sem Nome (Marca)" é resolvido sem consultar a IA.
        with patch.object(
            ia_consenso,
            "_chamar_deepseek",
            side_effect=AssertionError("não deveria consultar a IA"),
        ):
            assert (
                ia_consenso.obter_marca_inteligente(
                    "PRODUTO SEM NOME (HEINEKEN)", db=db
                )
                == "HEINEKEN"
            )

        # Cache antigo gravou "SEM MARCA" para marcas reais: a trava apaga o
        # registro envenenado e devolve a marca certa sem gastar IA.
        db.add_all(
            [
                MarcaIA(descricao_original="SUCO VILLA PIVA", marca_extraida="SEM MARCA"),
                MarcaIA(descricao_original="MOSTARDA CEPERA", marca_extraida="SEM MARCA"),
            ]
        )
        db.commit()
        with patch.object(
            ia_consenso,
            "_chamar_deepseek",
            side_effect=AssertionError("marca conhecida não deveria gastar IA"),
        ):
            assert (
                ia_consenso.obter_marca_inteligente("SUCO VILLA PIVA", db=db)
                == "VILLA PIVA"
            )
            assert (
                ia_consenso.obter_marca_inteligente("MOSTARDA CEPERA", db=db)
                == "CEPERA"
            )
        cache_corrigido = {
            registro.descricao_original: registro.marca_extraida
            for registro in db.query(MarcaIA).all()
        }
        assert cache_corrigido["SUCO VILLA PIVA"] == "VILLA PIVA", cache_corrigido
        assert cache_corrigido["MOSTARDA CEPERA"] == "CEPERA", cache_corrigido
    finally:
        db.close()
        engine.dispose()


@teste
def test_recalcular_marcas_reaproveita_memoria_da_ia():
    """`recalcular_marcas(usar_ia=True)` grava a marca e a segunda rodada é 100% cache."""
    from unittest.mock import patch

    from backend import marcas as bmarcas
    from backend.models import MarcaIA

    def fake(mensagens, temperatura=0.0, max_tokens=900):
        prompt = mensagens[-1]["content"]
        # Confere a descrição do item, não os exemplos do prompt (que também citam ARRICO).
        if "POLENTA PALITO ARRICO" in prompt:
            return json.dumps({"marca": "ARRICO"}, ensure_ascii=False)
        if "POLENTA PALITO EL CHEF" in prompt:
            return json.dumps({"marca": "EL CHEF"}, ensure_ascii=False)
        return json.dumps({"marca": "SEM MARCA"}, ensure_ascii=False)

    descricoes = [
        "POLENTA PALITO ARRICO CONG CX - 1",
        "POLENTA PALITO ARRICO CONG CX - 2",
        "POLENTA PALITO EL CHEF CONG CX",
        "ALFACE CRESPA",
    ]
    db, engine = _banco_aprendizado_temporario()
    try:
        empresa = Empresa(cnpj="12345678000199", nome_loja="Loja Memória IA")
        fornecedor = Fornecedor(cnpj="98765432000199", nome="Fornecedor Memória IA")
        db.add_all([empresa, fornecedor])
        db.commit()
        nota = NotaFiscal(
            chave_acesso="TESTE-MEMORIA-MARCAS-IA",
            numero_nota="1",
            serie="1",
            data_emissao=date(2026, 1, 10),
            valor_total=40.0,
            empresa_id=empresa.id,
        )
        db.add(nota)
        db.commit()
        db.refresh(nota)
        for descricao in descricoes:
            db.add(
                ItemNota(
                    nota_id=nota.id,
                    fornecedor_id=fornecedor.id,
                    ean="SEM_EAN",
                    descricao_produto=descricao,
                    quantidade=1.0,
                    valor_unitario=10.0,
                    valor_total=10.0,
                    ncm="19019090",
                )
            )
        db.commit()

        with patch.object(ia_consenso, "_chamar_deepseek", side_effect=fake):
            resumo = bmarcas.recalcular_marcas(db, usar_ia=True)
        assert resumo["descricoes_unicas"] == 3, resumo
        assert resumo["consultas_ia"] == 3, resumo
        assert resumo["cache_hits"] == 0, resumo
        assert resumo["sem_marca"] == 1, resumo
        assert resumo["com_marca"] == 3, resumo
        assert resumo["erro_ia"] is None, resumo
        marcas_gravadas = {
            item.descricao_produto: item.marca for item in db.query(ItemNota).all()
        }
        assert {
            marca for descricao, marca in marcas_gravadas.items() if "ARRICO" in descricao
        } == {"ARRICO"}, marcas_gravadas
        assert {
            marca for descricao, marca in marcas_gravadas.items() if "CHEF" in descricao
        } == {"EL CHEF"}, marcas_gravadas
        assert marcas_gravadas["ALFACE CRESPA"] is None, marcas_gravadas
        assert db.query(MarcaIA).count() == 3

        # Segunda rodada: nenhuma chamada nova (o fake falha se for usado).
        with patch.object(
            ia_consenso,
            "_chamar_deepseek",
            side_effect=AssertionError("a memória deveria responder tudo"),
        ):
            resumo_cache = bmarcas.recalcular_marcas(db, usar_ia=True)
        assert resumo_cache["consultas_ia"] == 0, resumo_cache
        assert resumo_cache["cache_hits"] == 3, resumo_cache
        assert resumo_cache["com_marca"] == 3, resumo_cache

        # `apenas_cache=True` (importador/backfill) também não chama a IA.
        with patch.object(
            ia_consenso,
            "_chamar_deepseek",
            side_effect=AssertionError("apenas_cache não pode chamar a IA"),
        ):
            resumo_apenas = bmarcas.recalcular_marcas(db, apenas_cache=True)
        assert resumo_apenas["cache_hits"] == 3, resumo_apenas
        assert resumo_apenas["consultas_ia"] == 0, resumo_apenas
        assert resumo_apenas["com_marca"] == 3, resumo_apenas
    finally:
        db.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
