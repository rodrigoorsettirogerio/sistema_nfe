"""Teste unitário do fluxo automático certificado → download → importação.

Não usa internet nem a ACBr real: o download é substituído por uma função que
grava um XML na pasta temporária e o banco roda em SQLite temporário. O objetivo
é garantir que a tarefa de background:

- respeita a janela configurada no primeiro download (padrão 7 dias) e o NSU
  salvo depois;
- importa o XML baixado para a loja correta;
- não apaga as notas existentes;
- ignora uma chave já importada quando roda novamente.

Como rodar (sem backend ligado):
    python tests/test_acbr_auto_import.py
"""

import os
import shutil
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

# Fixa a janela antes de importar o backend: o `.env` usa override=False e não
# sobrescreve esta variável no processo do teste.
os.environ.setdefault("DIAS_PRIMEIRO_DOWNLOAD", "7")
DIAS_PRIMEIRO_DOWNLOAD = int(os.environ["DIAS_PRIMEIRO_DOWNLOAD"])

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import import_xmls
from backend import database as bdatabase
from backend import main as backend_main
from backend import models

CNPJ = "07269638000171"
CHAVE = "352611" + CNPJ + "55001000000011000000010"
FORNECEDOR = "11111111000191"


def _xml_nota():
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe" versao="4.00">
  <NFe>
    <infNFe Id="NFe{CHAVE}" versao="4.00">
      <ide>
        <cUF>35</cUF>
        <nNF>123</nNF>
        <serie>1</serie>
        <dhEmi>{date.today().isoformat()}T10:00:00-03:00</dhEmi>
      </ide>
      <emit>
        <CNPJ>{FORNECEDOR}</CNPJ>
        <xNome>FORNECEDOR AUTOMATICO LTDA</xNome>
      </emit>
      <dest>
        <CNPJ>{CNPJ}</CNPJ>
        <xNome>LOJA AUTOMATICA LTDA</xNome>
      </dest>
      <det nItem="1">
        <prod>
          <cProd>P001</cProd>
          <cEAN>7891234567890</cEAN>
          <xProd>ARROZ TESTE 5KG</xProd>
          <NCM>10063021</NCM>
          <CFOP>5102</CFOP>
          <uCom>UN</uCom>
          <qCom>1.0000</qCom>
          <vUnCom>10.0000</vUnCom>
          <vProd>10.00</vProd>
        </prod>
      </det>
      <total><ICMSTot><vNF>10.00</vNF></ICMSTot></total>
    </infNFe>
  </NFe>
  <protNFe versao="4.00"><infProt><cStat>100</cStat></infProt></protNFe>
</nfeProc>"""



def main():
    pasta = tempfile.mkdtemp(prefix="acbr_auto_import_")
    caminho_db = os.path.join(pasta, "teste.db")
    engine = create_engine(
        "sqlite:///" + caminho_db.replace("\\", "/"),
        connect_args={"check_same_thread": False},
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    models.Base.metadata.create_all(bind=engine)

    db = TestingSession()
    usuario = models.Usuario(
        nome="Conta Automática",
        email=f"auto.{os.getpid()}@teste.local",
        senha_hash="nao-usado",
    )
    empresa = models.Empresa(
        cnpj=CNPJ,
        razao_social="LOJA AUTOMATICA LTDA",
        nome_loja="Loja Automática",
        municipio="SAO PAULO",
        usuario_id=None,
    )
    db.add(usuario)
    db.commit()
    db.refresh(usuario)
    empresa.usuario_id = usuario.id
    db.add(empresa)
    db.commit()
    db.refresh(empresa)
    empresa_id = empresa.id
    db.close()

    requisicoes = []

    def _pasta(cnpj):
        destino = os.path.join(pasta, "xmls", cnpj)
        os.makedirs(destino, exist_ok=True)
        return destino

    def _download_simulado(requisicao, usuario, db):
        requisicoes.append(requisicao)
        destino = _pasta(requisicao.cnpj)
        with open(os.path.join(destino, f"{CHAVE}.xml"), "wb") as arquivo:
            arquivo.write(_xml_nota().encode("utf-8"))
        loja = db.query(models.Empresa).filter(models.Empresa.cnpj == CNPJ).one()
        loja.ultimo_nsu_acbr = "123"
        db.commit()
        return {
            "documentos_baixados": 1,
            "ultimo_nsu_acbr": "123",
            "primeiro_download": requisicao.data_inicial > date(2000, 1, 1),
        }

    originais = {
        "SessionLocal_main": backend_main.SessionLocal,
        "SessionLocal_import": import_xmls.SessionLocal,
        "baixar": backend_main.baixar_xmls_da_acbr,
        "pasta": backend_main._pasta_xmls_acbr,
        "init_db": bdatabase.init_db,
        "marcas": import_xmls.recalcular_marcas,
    }
    backend_main.SessionLocal = TestingSession
    import_xmls.SessionLocal = TestingSession
    backend_main.baixar_xmls_da_acbr = _download_simulado
    backend_main._pasta_xmls_acbr = _pasta
    bdatabase.init_db = lambda: None
    import_xmls.recalcular_marcas = lambda db, apenas_cache=True: {
        "com_marca": 0,
        "ncms_com_marca": 0,
        "atualizados": 0,
        "cache_hits": 0,
    }
    try:
        primeiro = backend_main._baixar_e_importar_acbr_em_background(
            CNPJ, "homologacao"
        )
        assert primeiro["sucesso"] is True, primeiro
        assert primeiro["importacao"]["importadas"] == 1, primeiro
        assert primeiro["importacao"]["ignoradas"] == 0, primeiro
        assert len(requisicoes) == 1
        assert requisicoes[0].data_inicial == date.today() - timedelta(
            days=DIAS_PRIMEIRO_DOWNLOAD
        ), requisicoes[0].data_inicial

        # Mesmo que o front peça um histórico maior, o primeiro download fica
        # limitado à janela configurada (padrão 7 dias).
        periodo = backend_main._periodo_consulta_acbr(
            date.today() - timedelta(days=60),
            date.today() + timedelta(days=1),
            True,
        )
        assert periodo == (
            date.today() - timedelta(days=DIAS_PRIMEIRO_DOWNLOAD),
            date.today() + timedelta(days=1),
        ), periodo

        db = TestingSession()
        nota = db.query(models.NotaFiscal).filter_by(chave_acesso=CHAVE).one()
        assert nota.empresa_id == empresa_id, nota.empresa_id
        assert nota.itens, "o item da nota precisa ser importado"
        assert nota.itens[0].descricao_produto == "ARROZ TESTE 5KG"
        db.close()

        # Segunda execução: a chave já existe, então nada é duplicado e o
        # download passa a usar o NSU salvo (período local aberto).
        segundo = backend_main._baixar_e_importar_acbr_em_background(
            CNPJ, "homologacao"
        )
        assert segundo["sucesso"] is True, segundo
        assert segundo["importacao"]["importadas"] == 0, segundo
        assert segundo["importacao"]["ignoradas"] == 1, segundo
        assert requisicoes[1].data_inicial == date(2000, 1, 1), (
            requisicoes[1].data_inicial
        )

        db = TestingSession()
        assert db.query(models.NotaFiscal).count() == 1
        assert db.query(models.ItemNota).count() == 1
        db.close()
    finally:
        backend_main.SessionLocal = originais["SessionLocal_main"]
        import_xmls.SessionLocal = originais["SessionLocal_import"]
        backend_main.baixar_xmls_da_acbr = originais["baixar"]
        backend_main._pasta_xmls_acbr = originais["pasta"]
        bdatabase.init_db = originais["init_db"]
        import_xmls.recalcular_marcas = originais["marcas"]
        engine.dispose()
        shutil.rmtree(pasta, ignore_errors=True)

    print("✅ test_acbr_auto_import: download + importação incremental passaram")


if __name__ == "__main__":
    main()

