"""Teste end-to-end SIMULADO das rotas de integração com a ACBr API.

O teste sobe uma ACBr fake local (OAuth, Empresa, Certificado e Distribuição
NF-e) e uma instância real do FastAPI apontando para ela. Nenhuma chamada sai
para a internet e nenhuma credencial real é usada.

Cobre:

- ``POST /api/acbr/empresas``: valida o CNPJ da conta, envia empresa +
  certificado (base64) e configura a Distribuição NF-e;
- ``POST /api/acbr/baixar-xmls``: pede ``dist-nsu``, pagina os documentos
  (com ``$inlinecount=true``, booleano exigido pela ACBr), baixa somente NF-e
  completa e grava no disco apenas o XML cuja ``dhEmi`` está dentro do período.
  A ACBr não recebe campos de data (o schema não os aceita): o primeiro download
  envia ``dist_nsu=0`` e o NSU devolvido fica salvo para as consultas seguintes;
- ``GET /api/acbr/status/{cnpj}``: devolve o NSU salvo na loja e o último NSU
  disponível na ACBr, lendo o cursor do banco antes da consulta e persistindo o
  ``ultNSU`` retornado. Se o banco estiver atrasado, o cursor exigido pela ACBr
  é gravado e a consulta é repetida uma vez.

Como rodar (não precisa de backend já ligado):
    python tests/test_acbr_rotas.py
"""

import base64
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import requests


def _porta_livre():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


PORTA_ACBR = _porta_livre()
PORTA_API = _porta_livre()
BASE_ACBR = f"http://127.0.0.1:{PORTA_ACBR}"
BASE_API = f"http://127.0.0.1:{PORTA_API}"
PASTA_XML = tempfile.mkdtemp(prefix="acbr_xmls_teste_")

# O backend lê estas variáveis no import: precisam existir ANTES do processo.
AMBIENTE = os.environ.copy()
AMBIENTE.update(
    {
        "ACBR_CLIENT_ID": "cliente-mock",
        "ACBR_CLIENT_SECRET": "segredo-mock",
        "ACBR_AMBIENTE": "sandbox",
        "ACBR_AUTH_URL": BASE_ACBR + "/token",
        "ACBR_API_URL": BASE_ACBR,
        "ACBR_SCOPES": "empresa distribuicao-nfe",
        "ACBR_TIMEOUT": "10",
        "ACBR_XML_DIR": PASTA_XML,
        "DIAS_PRIMEIRO_DOWNLOAD": "7",
        # Este teste exercita as rotas manualmente, na ordem em que ele controla
        # o NSU. A automação pós-certificado tem cobertura própria e desligá-la
        # evita que o background consuma a primeira distribuição em paralelo.
        "ACBR_AUTO_IMPORTAR_APOS_CERTIFICADO": "0",
        # Cadastro exige confirmação de e-mail: sem este flag a API tentaria o Gmail
        # real para enviar o código a um endereço de teste (o teste confirma direto
        # no banco e entra pelo login normal).
        "NFE_SMTP_DESATIVADO": "1",
    }
)
DIAS_PRIMEIRO_DOWNLOAD = int(AMBIENTE["DIAS_PRIMEIRO_DOWNLOAD"])


def _cnpj_valido_aleatorio():
    """CNPJ com dígitos verificadores corretos (o backend valida o CNPJ da conta)."""
    base = "99" + "".join(str(random.randint(0, 9)) for _ in range(6)) + "0001"

    def digito(parcial):
        pesos = list(range(len(parcial) - 7, 1, -1))
        pesos = pesos + list(range(9, 1, -1))
        soma = sum(int(numero) * peso for numero, peso in zip(parcial, pesos))
        resto = soma % 11
        return "0" if resto < 2 else str(11 - resto)

    primeiro = digito(base)
    return base + primeiro + digito(base + primeiro)


CNPJ = _cnpj_valido_aleatorio()
CHAVE_DENTRO = "352601" + CNPJ + "55001" + "000000123" + "1" + "00000001" + "0"
CHAVE_FORA = "352601" + CNPJ + "55001" + "000000124" + "1" + "00000001" + "0"
HOJE = date.today()
FORNECEDOR_MOCK = "11111111000191"

MOCK = {
    "token_requests": 0,
    "empresa_criada": None,
    "empresa_atualizada": None,
    "certificado_put": None,
    "config": None,
    "config_put": 0,
    "distribuicao": None,
    "distribuicoes": [],
    # Cursor que a ACBr espera receber em `dist_nsu` na próxima chamada.
    "nsu_atual": 0,
    "xml_baixados": [],
}


def _xml_nota(chave, data_iso):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe" versao="4.00">
  <NFe>
    <infNFe Id="NFe{chave}" versao="4.00">
      <ide>
        <cUF>35</cUF>
        <nNF>123</nNF>
        <serie>1</serie>
        <dhEmi>{data_iso}T10:00:00-03:00</dhEmi>
      </ide>
      <emit>
        <CNPJ>{FORNECEDOR_MOCK}</CNPJ>
        <xNome>FORNECEDOR MOCK LTDA</xNome>
      </emit>
    </infNFe>
  </NFe>
  <protNFe versao="4.00"><infProt><cStat>100</cStat></infProt></protNFe>
</nfeProc>""".encode("utf-8")


class AcbrFake(BaseHTTPRequestHandler):
    """Servidor HTTP mínimo que imita os endpoints usados pela integração."""

    server_version = "AcbrFake/1.0"

    def log_message(self, formato, *args):  # silencia o log do servidor
        return

    def _responder(self, status, corpo=None, bytes_xml=None):
        if bytes_xml is not None:
            dados = bytes_xml
            tipo = "application/xml; charset=utf-8"
        else:
            dados = json.dumps(
                corpo if corpo is not None else {}, ensure_ascii=False
            ).encode("utf-8")
            tipo = "application/json; charset=utf-8"
        self.send_response(status)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)

    def _corpo(self):
        tamanho = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(tamanho) if tamanho else b""

    def _autorizado(self):
        return self.headers.get("Authorization") == "Bearer token-mock"

    def do_POST(self):
        caminho = urlparse(self.path).path
        if caminho == "/token":
            corpo = parse_qs(self._corpo().decode("utf-8"))
            assert corpo.get("grant_type") == ["client_credentials"], corpo
            assert corpo.get("client_id") == ["cliente-mock"], corpo
            assert corpo.get("client_secret") == ["segredo-mock"], corpo
            assert "empresa" in corpo.get("scope", [""])[0], corpo
            MOCK["token_requests"] += 1
            self._responder(
                200,
                {
                    "access_token": "token-mock",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
            return
        if not self._autorizado():
            self._responder(401, {"detail": "token inválido"})
            return
        if caminho == "/empresas":
            dados = json.loads(self._corpo().decode("utf-8"))
            assert dados["cpf_cnpj"] == CNPJ, dados
            assert dados["endereco"]["codigo_municipio"] == "3550308", dados
            assert dados["email"] == "financeiro@empresa-mock.test", dados
            MOCK["empresa_criada"] = dados
            self._responder(200, {**dados, "id": "empresa-mock"})
            return
        if caminho == "/distribuicao/nfe":
            dados = json.loads(self._corpo().decode("utf-8"))
            assert dados["tipo_consulta"] == "dist-nsu", dados
            assert dados["cpf_cnpj"] == CNPJ, dados
            # A ACBr real rejeita a consulta quando dist_nsu não é exatamente o
            # ultNSU devolvido na chamada anterior. O fake reproduz essa regra
            # para o teste pegar o regresso de enviar 0 em toda consulta.
            if dados["dist_nsu"] != MOCK["nsu_atual"]:
                self._responder(
                    400,
                    {
                        "detail": (
                            "O campo 'dist_nsu' deve ser igual a "
                            f"{MOCK['nsu_atual']}"
                        )
                    },
                )
                return
            MOCK["distribuicao"] = dados
            MOCK["distribuicoes"].append(dados)
            # Primeiro lote (0) devolve 123; as chamadas seguintes avançam um
            # NSU por vez, inclusive a consulta de status.
            proximo_nsu = 123 if MOCK["nsu_atual"] == 0 else MOCK["nsu_atual"] + 1
            MOCK["nsu_atual"] = proximo_nsu
            self._responder(
                200,
                {
                    "id": "distribuicao-mock",
                    "status": "concluido",
                    "ambiente": dados["ambiente"],
                    "tipo_consulta": "dist-nsu",
                    "codigo_status": 138,
                    "motivo_status": "Documento localizado",
                    "data_hora_resposta": HOJE.isoformat(),
                    "ultNSU": proximo_nsu,
                    "maxNSU": 125,
                    "ultimo_nsu": proximo_nsu,
                    "max_nsu": 125,
                },
            )
            return
        self._responder(404, {"detail": "endpoint fake não implementado"})

    def do_GET(self):
        partes = urlparse(self.path)
        caminho = partes.path
        if not self._autorizado():
            self._responder(401, {"detail": "token inválido"})
            return
        if caminho == f"/empresas/{CNPJ}":
            if MOCK["empresa_criada"]:
                self._responder(200, MOCK["empresa_criada"])
            else:
                self._responder(404, {"detail": "Empresa não encontrada"})
            return
        if caminho == f"/empresas/{CNPJ}/distnfe":
            if MOCK["config"]:
                self._responder(200, MOCK["config"])
            else:
                self._responder(404, {"detail": "Configuração não encontrada"})
            return
        if caminho == "/distribuicao/nfe/documentos":
            consulta = parse_qs(partes.query)
            assert consulta.get("cpf_cnpj") == [CNPJ], consulta
            assert consulta.get("ambiente") == ["homologacao"], consulta
            # A ACBr API declara `$inlinecount` como BOOLEAN; a string OData
            # "allpages" faz o backend responder "Cannot deserialize value...".
            inlinecount = (consulta.get("$inlinecount") or [""])[0]
            assert inlinecount.lower() == "true", consulta
            assert inlinecount != "allpages", consulta
            skip = int((consulta.get("$skip") or ["0"])[0])
            if skip:
                self._responder(200, {"@count": 3, "data": []})
                return
            documentos = [
                {
                    "id": "doc-1",
                    "tipo_documento": "nota",
                    "resumo": False,
                    "chave_acesso": CHAVE_DENTRO,
                    "emitente_nome_razao_social": "FORNECEDOR MOCK LTDA",
                },
                {
                    "id": "doc-2",
                    "tipo_documento": "nota",
                    "resumo": False,
                    "chave_acesso": CHAVE_FORA,
                    "emitente_nome_razao_social": "FORNECEDOR MOCK LTDA",
                },
                {
                    "id": "doc-3",
                    "tipo_documento": "nota",
                    "resumo": True,
                    "chave_acesso": CHAVE_DENTRO,
                },
            ]
            self._responder(200, {"@count": 3, "data": documentos})
            return
        if caminho.startswith("/distribuicao/nfe/documentos/") and caminho.endswith(
            "/xml"
        ):
            documento_id = caminho.split("/")[-2]
            MOCK["xml_baixados"].append(documento_id)
            if documento_id == "doc-1":
                self._responder(
                    200, bytes_xml=_xml_nota(CHAVE_DENTRO, HOJE.isoformat())
                )
            elif documento_id == "doc-2":
                self._responder(
                    200,
                    bytes_xml=_xml_nota(
                        CHAVE_FORA, (HOJE - timedelta(days=900)).isoformat()
                    ),
                )
            else:
                self._responder(404, {"detail": "XML indisponível"})
            return
        self._responder(404, {"detail": "endpoint fake não implementado"})


    def do_PUT(self):
        caminho = urlparse(self.path).path
        if not self._autorizado():
            self._responder(401, {"detail": "token inválido"})
            return
        if caminho == f"/empresas/{CNPJ}/certificado":
            dados = json.loads(self._corpo().decode("utf-8"))
            MOCK["certificado_put"] = dados
            self._responder(
                200,
                {
                    "id": "certificado-mock",
                    "cpf_cnpj": CNPJ,
                    "nome_razao_social": "EMPRESA MOCK LTDA",
                    "serial_number": "123456789",
                    "not_valid_after": "2027-12-31T23:59:59Z",
                },
            )
            return
        if caminho == f"/empresas/{CNPJ}/distnfe":
            dados = json.loads(self._corpo().decode("utf-8"))
            MOCK["config"] = dados
            MOCK["config_put"] += 1
            self._responder(200, dados)
            return
        if caminho == f"/empresas/{CNPJ}":
            dados = json.loads(self._corpo().decode("utf-8"))
            MOCK["empresa_atualizada"] = dados
            self._responder(200, {**dados, "id": "empresa-mock"})
            return
        self._responder(404, {"detail": "endpoint fake não implementado"})


def _gerar_pfx(cnpj, senha):
    """Certificado A1 de teste, igual ao usado pelos testes do projeto."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID

    chave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nome = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, f"EMPRESA MOCK:{cnpj}"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Empresa Mock"),
            x509.NameAttribute(NameOID.COUNTRY_NAME, "BR"),
        ]
    )
    agora = datetime.now(timezone.utc)
    certificado = (
        x509.CertificateBuilder()
        .subject_name(nome)
        .issuer_name(nome)
        .public_key(chave.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(agora - timedelta(days=1))
        .not_valid_after(agora + timedelta(days=365))
        .sign(chave, hashes.SHA256())
    )
    return pkcs12.serialize_key_and_certificates(
        name=b"certificado-mock",
        key=chave,
        cert=certificado,
        cas=None,
        encryption_algorithm=serialization.BestAvailableEncryption(
            senha.encode("utf-8")
        ),
    )



def _esperar_api(limite=40):
    for _ in range(limite * 4):
        try:
            if requests.get(BASE_API + "/", timeout=1).status_code == 200:
                return
        except requests.RequestException:
            pass
        time.sleep(0.25)
    raise RuntimeError("O backend de teste não subiu.")


def _testar_nsu_do_banco_prevalece():
    """`consultar_ultimo_nsu` lê o cursor da tabela empresas antes do POST."""
    from unittest.mock import patch

    import backend.database as bdatabase
    from backend import acbr_client
    from backend.models import Empresa

    bdatabase.init_db()
    cnpj_teste = _cnpj_valido_aleatorio()
    db = bdatabase.SessionLocal()
    db.query(Empresa).filter(Empresa.cnpj == cnpj_teste).delete()
    db.add(
        Empresa(
            cnpj=cnpj_teste,
            razao_social="EMPRESA UNIT NSU LTDA",
            nome_loja="Loja Unit NSU",
            ultimo_nsu_acbr="28165",
        )
    )
    db.commit()

    cliente = acbr_client.AcbrClient(
        client_id="cliente-mock",
        client_secret="segredo-mock",
        api_url=BASE_ACBR,
        auth_url=BASE_ACBR + "/token",
    )
    enviados = []

    def fake_requisicao(metodo, caminho, **kwargs):
        enviados.append(dict(kwargs["json"]))
        return {"ultNSU": 28165}

    try:
        with patch.object(
            acbr_client, "_salvar_nsu_empresa_local", return_value=True
        ), patch.object(cliente, "consultar_empresa", return_value={}), patch.object(
            cliente, "_requisicao", side_effect=fake_requisicao
        ):
            resultado = cliente.consultar_ultimo_nsu(cnpj_teste, dist_nsu=0)

        assert resultado == "28165", resultado
        corpo = enviados[-1]
        assert corpo["dist_nsu"] == 28165, corpo

        # Loja com NSU "0" no banco é primeiro download: envia 0, não o
        # valor antigo que o chamador passou.
        empresa = db.query(Empresa).filter(Empresa.cnpj == cnpj_teste).one()
        empresa.ultimo_nsu_acbr = "0"
        db.commit()
        enviados.clear()
        with patch.object(
            acbr_client, "_salvar_nsu_empresa_local", return_value=True
        ), patch.object(cliente, "consultar_empresa", return_value={}), patch.object(
            cliente, "_requisicao", side_effect=fake_requisicao
        ):
            cliente.consultar_ultimo_nsu(cnpj_teste, dist_nsu=28165)

        assert enviados[-1]["dist_nsu"] == 0, enviados[-1]
    finally:
        db.query(Empresa).filter(Empresa.cnpj == cnpj_teste).delete()
        db.commit()
        db.close()


def _testar_payload_distribuicao_sem_datas_e_nsu_do_banco():
    """O POST /distribuicao/nfe usa o NSU do banco e não envia datas."""
    from unittest.mock import patch

    import backend.database as bdatabase
    from backend import acbr_client
    from backend.models import Empresa

    bdatabase.init_db()
    cnpj_teste = _cnpj_valido_aleatorio()
    db = bdatabase.SessionLocal()
    db.query(Empresa).filter(Empresa.cnpj == cnpj_teste).delete()
    db.add(
        Empresa(
            cnpj=cnpj_teste,
            razao_social="EMPRESA UNIT PAYLOAD LTDA",
            nome_loja="Loja Unit Payload",
            ultimo_nsu_acbr="28165",
        )
    )
    db.commit()

    cliente = acbr_client.AcbrClient(
        client_id="cliente-mock",
        client_secret="segredo-mock",
        api_url=BASE_ACBR,
        auth_url=BASE_ACBR + "/token",
    )
    enviados = []

    def fake_requisicao(metodo, caminho, **kwargs):
        enviados.append(dict(kwargs["json"]))
        return {"ultNSU": 28165}

    try:
        with patch.object(cliente, "_requisicao", side_effect=fake_requisicao):
            # O valor passado pelo chamador é ignorado: vale o banco.
            cliente.distribuir_nfe(cnpj_teste, dist_nsu=0)

        corpo = enviados[-1]
        assert corpo["dist_nsu"] == 28165, corpo
        for campo_data in ("data_inicial", "data_final", "dataInicio", "dataFim"):
            assert campo_data not in corpo, corpo
        assert set(corpo) <= {
            "cpf_cnpj",
            "ambiente",
            "uf_autor",
            "tipo_consulta",
            "dist_nsu",
            "ignorar_tempo_espera",
        }, corpo
    finally:
        db.query(Empresa).filter(Empresa.cnpj == cnpj_teste).delete()
        db.commit()
        db.close()


def _testar_sincronizacao_no_distribuir_nfe():
    """O download também corrige e repete o POST quando o cursor atrasa."""
    from unittest.mock import patch

    import backend.database as bdatabase
    from backend import acbr_client
    from backend.models import Empresa

    bdatabase.init_db()
    cnpj_teste = _cnpj_valido_aleatorio()
    db = bdatabase.SessionLocal()
    db.query(Empresa).filter(Empresa.cnpj == cnpj_teste).delete()
    db.add(
        Empresa(
            cnpj=cnpj_teste,
            razao_social="EMPRESA UNIT SYNC DIST LTDA",
            nome_loja="Loja Unit Sync Distribuicao",
            ultimo_nsu_acbr="0",
        )
    )
    db.commit()

    cliente = acbr_client.AcbrClient(
        client_id="cliente-mock",
        client_secret="segredo-mock",
        api_url=BASE_ACBR,
        auth_url=BASE_ACBR + "/token",
    )
    enviados = []

    def fake_requisicao(metodo, caminho, **kwargs):
        dist_nsu = kwargs["json"]["dist_nsu"]
        enviados.append(dist_nsu)
        if dist_nsu == 0:
            raise acbr_client.ErroAcbr(
                "ACBr API recusou POST /distribuicao/nfe (HTTP 400)",
                status=400,
                corpo={
                    "code": "ValidationFailed",
                    "message": "O campo 'dist_nsu' deve ser igual a 28165.",
                },
            )
        return {"ultNSU": 28165}

    try:
        with patch.object(cliente, "_requisicao", side_effect=fake_requisicao):
            cliente.distribuir_nfe(cnpj_teste, dist_nsu=28165)

        assert enviados == [0, 28165], enviados
        db.expire_all()
        empresa = db.query(Empresa).filter(Empresa.cnpj == cnpj_teste).one()
        assert empresa.ultimo_nsu_acbr == "28165", empresa.ultimo_nsu_acbr
    finally:
        db.query(Empresa).filter(Empresa.cnpj == cnpj_teste).delete()
        db.commit()
        db.close()


def _testar_sincronizacao_do_nsu_esperado():
    """Se o banco atrasar, o cursor informado pela ACBr é salvo e o POST repete."""
    from unittest.mock import patch

    import backend.database as bdatabase
    from backend import acbr_client
    from backend.models import Empresa

    bdatabase.init_db()
    cnpj_teste = _cnpj_valido_aleatorio()
    db = bdatabase.SessionLocal()
    db.query(Empresa).filter(Empresa.cnpj == cnpj_teste).delete()
    db.add(
        Empresa(
            cnpj=cnpj_teste,
            razao_social="EMPRESA UNIT SYNC LTDA",
            nome_loja="Loja Unit Sync",
            ultimo_nsu_acbr="0",
        )
    )
    db.commit()

    cliente = acbr_client.AcbrClient(
        client_id="cliente-mock",
        client_secret="segredo-mock",
        api_url=BASE_ACBR,
        auth_url=BASE_ACBR + "/token",
    )
    enviados = []

    def fake_requisicao(metodo, caminho, **kwargs):
        dist_nsu = kwargs["json"]["dist_nsu"]
        enviados.append(dist_nsu)
        if dist_nsu == 0:
            raise acbr_client.ErroAcbr(
                "ACBr API recusou POST /distribuicao/nfe (HTTP 400)",
                status=400,
                corpo={
                    "code": "ValidationFailed",
                    "message": "O campo 'dist_nsu' deve ser igual a 28165.",
                },
            )
        return {"ultNSU": 28165}

    try:
        with patch.object(cliente, "consultar_empresa", return_value={}), patch.object(
            cliente, "_requisicao", side_effect=fake_requisicao
        ):
            resultado = cliente.consultar_ultimo_nsu(cnpj_teste)

        assert resultado == "28165", resultado
        assert enviados == [0, 28165], enviados
        db.expire_all()
        empresa = db.query(Empresa).filter(Empresa.cnpj == cnpj_teste).one()
        assert empresa.ultimo_nsu_acbr == "28165", empresa.ultimo_nsu_acbr
    finally:
        db.query(Empresa).filter(Empresa.cnpj == cnpj_teste).delete()
        db.commit()
        db.close()


def main():
    import backend.database as bdatabase
    from backend.models import CodigoEmail, Empresa, Usuario

    _testar_nsu_do_banco_prevalece()
    _testar_payload_distribuicao_sem_datas_e_nsu_do_banco()
    _testar_sincronizacao_no_distribuir_nfe()
    _testar_sincronizacao_do_nsu_esperado()
    servidor_mock = ThreadingHTTPServer(("127.0.0.1", PORTA_ACBR), AcbrFake)
    servidor_mock.daemon_threads = True
    threading.Thread(target=servidor_mock.serve_forever, daemon=True).start()

    log_path = os.path.join(PASTA_XML, "backend.log")
    log = open(log_path, "w", encoding="utf-8")
    processo = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "backend.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(PORTA_API),
            "--log-level",
            "warning",
        ],
        cwd=str(RAIZ),
        env=AMBIENTE,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    usuario_id = None
    empresa_id = None
    db = None
    try:
        _esperar_api()

        # Sem sessão, as duas rotas são protegidas.
        sem_sessao = requests.post(
            BASE_API + "/api/acbr/baixar-xmls",
            json={
                "cnpj": CNPJ,
                "data_inicial": HOJE.isoformat(),
                "data_final": HOJE.isoformat(),
            },
            timeout=15,
        )
        assert sem_sessao.status_code == 401, sem_sessao.text[:300]

        email = f"acbr.mock.{uuid.uuid4().hex[:10]}@teste.local"
        registro = requests.post(
            BASE_API + "/api/auth/registrar",
            json={"nome": "Conta ACBr Mock", "email": email, "senha": "Senha@12345"},
            timeout=30,
        )
        assert registro.status_code == 200, registro.text[:300]
        pendente = registro.json()
        # Cadastro NÃO abre sessão: a conta nasce aguardando o código de confirmação.
        assert pendente.get("verificacao_pendente") is True, pendente
        assert "token" not in pendente, pendente

        # O teste confirma o e-mail direto no banco (SMTP desativado) e entra pelo
        # login normal, que é o caminho que o cliente usa depois de digitar o código.
        db = bdatabase.SessionLocal()
        usuario = db.query(Usuario).filter(Usuario.email == email).one()
        usuario.email_verificado = True
        usuario.email_verificado_em = datetime.now(timezone.utc).isoformat(
            timespec="seconds"
        )
        db.commit()
        db.refresh(usuario)
        usuario_id = usuario.id
        login = requests.post(
            BASE_API + "/api/auth/login",
            json={"email": email, "senha": "Senha@12345"},
            timeout=30,
        )
        assert login.status_code == 200, login.text[:300]
        token = login.json()["token"]
        cabecalho = {"Authorization": f"Bearer {token}"}

        empresa = Empresa(
            cnpj=CNPJ,
            razao_social="EMPRESA MOCK LTDA",
            nome_loja="Loja Mock",
            municipio="SAO PAULO",
            usuario_id=usuario_id,
        )
        db.add(empresa)
        db.commit()
        db.refresh(empresa)
        empresa_id = empresa.id

        senha_certificado = "Cert@12345"
        pfx = _gerar_pfx(CNPJ, senha_certificado)
        formulario = {
            "cnpj": CNPJ,
            "senha_certificado": senha_certificado,
            "cep": "01311-200",
            "logradouro": "RUA MOCK",
            "numero": "100",
            "complemento": "SALA 1",
            "bairro": "BELA VISTA",
            "codigo_municipio": "3550308",
            "cidade": "SAO PAULO",
            "uf": "sp",
            "email": "financeiro@empresa-mock.test",
            "telefone": "1130000000",
        }
        criada = requests.post(
            BASE_API + "/api/acbr/empresas",
            data=formulario,
            files={
                "certificado": (
                    "empresa-mock.pfx",
                    pfx,
                    "application/x-pkcs12",
                )
            },
            headers=cabecalho,
            timeout=60,
        )
        assert criada.status_code == 200, criada.text[:500]
        corpo_empresa = criada.json()
        assert corpo_empresa["sucesso"] is True, corpo_empresa
        assert corpo_empresa["criada_na_acbr"] is True, corpo_empresa
        assert corpo_empresa["background_iniciado"] is False, corpo_empresa
        assert corpo_empresa["empresa"]["cnpj"] == CNPJ, corpo_empresa
        assert corpo_empresa["distribuicao_configurada"] is True, corpo_empresa
        assert corpo_empresa["certificado_acbr"]["serial_number"] == "123456789"
        assert "password" not in json.dumps(corpo_empresa), corpo_empresa

        assert MOCK["empresa_criada"] is not None
        assert MOCK["empresa_criada"]["endereco"]["cep"] == "01311200"
        assert MOCK["empresa_criada"]["endereco"]["uf"] == "SP"
        assert MOCK["empresa_criada"]["endereco"]["codigo_municipio"] == "3550308"
        assert MOCK["certificado_put"]["certificado"] == base64.b64encode(pfx).decode(
            "ascii"
        ), MOCK["certificado_put"].keys()
        assert MOCK["certificado_put"]["password"] == senha_certificado, MOCK[
            "certificado_put"
        ].keys()
        assert MOCK["config"]["ambiente"] == "homologacao"


        baixados = requests.post(
            BASE_API + "/api/acbr/baixar-xmls",
            json={
                "cnpj": CNPJ,
                "data_inicial": (HOJE - timedelta(days=1)).isoformat(),
                "data_final": (HOJE + timedelta(days=1)).isoformat(),
                "limite_documentos": 10,
            },
            headers=cabecalho,
            timeout=90,
        )
        assert baixados.status_code == 200, baixados.text[:500]
        corpo_xmls = baixados.json()
        assert corpo_xmls["sucesso"] is True, corpo_xmls
        assert corpo_xmls["documentos_encontrados"] == 3, corpo_xmls
        assert corpo_xmls["documentos_baixados"] == 1, corpo_xmls
        assert corpo_xmls["documentos_ignorados"] == 2, corpo_xmls
        assert corpo_xmls["ambiente"] == "homologacao", corpo_xmls
        assert corpo_xmls["distribuicao"]["status"] == "concluido", corpo_xmls
        assert corpo_xmls["primeiro_download"] is True, corpo_xmls
        assert corpo_xmls["ultimo_nsu_acbr"] == "123", corpo_xmls
        assert corpo_xmls["novo_nsu"] == "123", corpo_xmls
        assert corpo_xmls["periodo"] == {
            "data_inicial": (HOJE - timedelta(days=DIAS_PRIMEIRO_DOWNLOAD)).isoformat(),
            "data_final": (HOJE + timedelta(days=1)).isoformat(),
        }, corpo_xmls
        # Primeiro download: a ACBr recebe dist_nsu=0 e NÃO recebe campos de
        # data (o schema DistribuicaoNfePedido v3.1.18 não os aceita; o período
        # é aplicado localmente depois do download).
        primeira_consulta = MOCK["distribuicoes"][0]
        assert primeira_consulta["dist_nsu"] == 0, primeira_consulta
        for campo_data in ("data_inicial", "data_final", "dataInicio", "dataFim"):
            assert campo_data not in primeira_consulta, primeira_consulta
        db.expire_all()
        assert (
            db.query(Empresa).filter(Empresa.id == empresa_id).one().ultimo_nsu_acbr
            == "123"
        ), "o NSU devolvido pela ACBr deve ser persistido"

        item = corpo_xmls["xmls"][0]
        assert item["chave_acesso"] == CHAVE_DENTRO, item
        assert item["data_emissao"] == HOJE.isoformat(), item
        assert item["emitente"] == "FORNECEDOR MOCK LTDA", item
        arquivo = Path(item["arquivo"])
        if not arquivo.is_absolute():
            arquivo = RAIZ / arquivo
        assert arquivo.is_file(), arquivo
        assert CHAVE_DENTRO.encode("ascii") in arquivo.read_bytes()
        assert not (
            Path(PASTA_XML) / CNPJ / f"{CHAVE_FORA}.xml"
        ).exists(), "XML fora do período não pode ser gravado"

        # O XML resumido (doc-3) nem chega a ser baixado; o doc-2 é baixado,
        # mas descartado pelo filtro de período.
        assert MOCK["xml_baixados"] == ["doc-1", "doc-2"], MOCK["xml_baixados"]
        assert MOCK["token_requests"] == 1, MOCK["token_requests"]
        assert MOCK["config_put"] == 1, MOCK["config_put"]
        assert MOCK["distribuicao"]["cpf_cnpj"] == CNPJ, MOCK["distribuicao"]
        motivos = " | ".join(item["motivo"] for item in corpo_xmls["ignorados"])
        assert "fora do período" in motivos, motivos
        assert "resumido" in motivos, motivos

        # Segunda consulta: usa o NSU salvo (123), NÃO reenvia as datas e grava o
        # novo NSU devolvido pela ACBr (124).
        segunda = requests.post(
            BASE_API + "/api/acbr/baixar-xmls",
            json={
                "cnpj": CNPJ,
                "data_inicial": (HOJE - timedelta(days=1)).isoformat(),
                "data_final": (HOJE + timedelta(days=1)).isoformat(),
                "limite_documentos": 10,
            },
            headers=cabecalho,
            timeout=90,
        )
        assert segunda.status_code == 200, segunda.text[:500]
        corpo_segunda = segunda.json()
        assert corpo_segunda["primeiro_download"] is False, corpo_segunda
        assert corpo_segunda["ultimo_nsu_acbr"] == "124", corpo_segunda
        assert corpo_segunda["novo_nsu"] == "124", corpo_segunda
        assert corpo_segunda["periodo"] == {
            "data_inicial": (HOJE - timedelta(days=1)).isoformat(),
            "data_final": (HOJE + timedelta(days=1)).isoformat(),
        }, corpo_segunda
        segunda_consulta = MOCK["distribuicoes"][1]
        assert segunda_consulta["dist_nsu"] == 123, segunda_consulta
        for campo_data in ("data_inicial", "data_final", "dataInicio", "dataFim"):
            assert campo_data not in segunda_consulta, segunda_consulta
        db.expire_all()
        assert (
            db.query(Empresa).filter(Empresa.id == empresa_id).one().ultimo_nsu_acbr
            == "124"
        ), "o NSU da segunda consulta deve substituir o anterior"

        # Diagnóstico: a consulta parte do NSU já salvo (124) e a ACBr devolve
        # o cursor novo (125). A rota deve persistir esse ultNSU para que a
        # próxima chamada envie exatamente o valor que a API espera.
        status = requests.get(
            BASE_API + f"/api/acbr/status/{CNPJ}",
            headers=cabecalho,
            timeout=60,
        )
        assert status.status_code == 200, status.text[:500]
        corpo_status = status.json()
        assert corpo_status["sucesso"] is True, corpo_status
        assert corpo_status["ultimo_nsu_anterior"] == "124", corpo_status
        assert corpo_status["ultimo_nsu_local"] == "125", corpo_status
        assert corpo_status["ultimo_nsu_acbr"] == "125", corpo_status
        assert corpo_status["pendentes"] == 1, corpo_status
        assert corpo_status["certificado"], corpo_status
        assert corpo_status["mensagem"] == (
            "Seu último NSU: 125 | Último NSU na ACBr: 125"
        ), corpo_status
        status_consulta = MOCK["distribuicoes"][2]
        assert status_consulta["dist_nsu"] == 124, status_consulta
        for campo_data in ("data_inicial", "data_final", "dataInicio", "dataFim"):
            assert campo_data not in status_consulta, status_consulta
        db.expire_all()
        assert (
            db.query(Empresa).filter(Empresa.id == empresa_id).one().ultimo_nsu_acbr
            == "125"
        ), "o ultNSU retornado pela consulta de status deve ser persistido"
    except Exception:
        log.flush()
        try:
            with open(log_path, "r", encoding="utf-8") as arquivo_log:
                print("\n--- log do backend de teste ---")
                print(arquivo_log.read()[-5000:])
        except OSError:
            pass
        raise
    finally:
        if db is not None:
            db.close()
        if usuario_id or empresa_id:
            limpeza = bdatabase.SessionLocal()
            try:
                if empresa_id is not None:
                    limpeza.query(Empresa).filter(Empresa.id == empresa_id).delete()
                if usuario_id is not None:
                    # O cadastro grava o código de confirmação: a conta de teste sai
                    # junto com os códigos (sem deixar órfãos no banco real).
                    limpeza.query(CodigoEmail).filter(
                        CodigoEmail.usuario_id == usuario_id
                    ).delete()
                    limpeza.query(Usuario).filter(Usuario.id == usuario_id).delete()
                limpeza.commit()
            finally:
                limpeza.close()
        processo.terminate()
        try:
            processo.wait(timeout=10)
        except subprocess.TimeoutExpired:
            processo.kill()
            processo.wait(timeout=5)
        log.close()
        servidor_mock.shutdown()
        servidor_mock.server_close()
        shutil.rmtree(PASTA_XML, ignore_errors=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
    print("✅ test_acbr_rotas: empresa/certificado e download de XMLs (mock) passaram")
    raise SystemExit(0)

