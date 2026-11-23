"""Testes do endereço usado no cadastro de empresa na ACBr.

Cobre o mapeamento do payload, o fallback para o endereço já gravado na loja
quando a BrasilAPI está indisponível (ex.: HTTP 429) e a repetição da consulta à
Receita (429/5xx/timeout com espera crescente e `Retry-After`). Não usa internet,
ACBr real nem o banco de produção.

Como rodar (sem backend ligado):
    python tests/test_acbr_endereco.py
"""

import os
import sys
import tempfile
import traceback
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from backend import acbr_client
from backend import consulta_cadastral
from backend import database as bdatabase
from backend import main as backend_main
from backend import models
from backend.models import Empresa

CNPJ = "07269638000171"
CNPJ_RECEITA = "47627864000100"
ENDERECO_LOCAL = {
    "razao_social": "PORTUCHO BAR E LANCHONETE LTDA",
    "nome_fantasia": "Portucho Matriz",
    "logradouro": "RUA PASSAROS E FLORES",
    "numero": "239",
    "complemento": "",
    "bairro": "JARDIM DAS ACACIAS",
    "codigo_municipio_ibge": "3550308",
    "municipio": "SAO PAULO",
    "uf": "SP",
    "cep": "04704000",
}
# Resposta "boa" da BrasilAPI (mesmos nomes de campo do serviço).
RECEITA = {
    "razao_social": "LOJA TESTE LTDA",
    "nome_fantasia": "Loja Teste",
    "municipio": "COTIA",
    "cep": "06708415",
    "uf": "sp",
    "codigo_municipio_ibge": 3513009,
    "bairro": "VILA SANTO ANTONIO",
    "descricao_tipo_de_logradouro": "RUA",
    "logradouro": "JOSE FELIX DE OLIVEIRA",
    "numero": "991",
    "email": "loja@teste.local",
    "ddd_telefone_1": "11999998888",
}


class _RespostaFalsa:
    """Resposta mínima no formato usado por `requests`."""

    def __init__(self, status_code, corpo):
        self.status_code = status_code
        self.ok = 200 <= status_code < 300
        self._corpo = corpo
        self.text = str(corpo)

    def json(self):
        if isinstance(self._corpo, dict):
            return self._corpo
        raise ValueError("corpo não é JSON")


def _empresa_com_endereco() -> Empresa:
    return Empresa(
        cnpj=CNPJ,
        razao_social=ENDERECO_LOCAL["razao_social"],
        nome_loja=ENDERECO_LOCAL["nome_fantasia"],
        municipio=ENDERECO_LOCAL["municipio"],
        logradouro=ENDERECO_LOCAL["logradouro"],
        numero=ENDERECO_LOCAL["numero"],
        complemento=ENDERECO_LOCAL["complemento"],
        bairro=ENDERECO_LOCAL["bairro"],
        codigo_municipio=ENDERECO_LOCAL["codigo_municipio_ibge"],
        uf=ENDERECO_LOCAL["uf"],
        cep=ENDERECO_LOCAL["cep"],
    )


def test_payload_usa_endereco_do_banco() -> None:
    """Sem BrasilAPI/formulário, o endereço persistido completa a ACBr."""
    dados = backend_main._montar_empresa_acbr(
        _empresa_com_endereco(), {}, {}, email_padrao="conta@teste.local"
    )
    endereco = dados["endereco"]
    assert dados["cpf_cnpj"] == CNPJ, dados
    assert endereco["logradouro"] == ENDERECO_LOCAL["logradouro"], endereco
    assert endereco["numero"] == ENDERECO_LOCAL["numero"], endereco
    assert endereco["bairro"] == ENDERECO_LOCAL["bairro"], endereco
    assert endereco["codigo_municipio"] == "3550308", endereco
    assert endereco["cidade"] == ENDERECO_LOCAL["municipio"], endereco
    assert endereco["uf"] == "SP", endereco
    assert endereco["cep"] == "04704000", endereco
    assert dados["email"] == "conta@teste.local", dados


def test_brasilapi_429_usa_dados_locais() -> None:
    """429 da BrasilAPI não bloqueia o cadastro quando há endereço local."""
    cliente = acbr_client.AcbrClient()
    resposta = _RespostaFalsa(429, "Too Many Requests")
    with patch("backend.acbr_client.requests.get", return_value=resposta):
        dados = cliente._dados_cadastro_brasilapi(CNPJ, ENDERECO_LOCAL)

    endereco = dados["endereco"]
    assert dados["cpf_cnpj"] == CNPJ, dados
    assert endereco["logradouro"] == ENDERECO_LOCAL["logradouro"], endereco
    assert endereco["bairro"] == ENDERECO_LOCAL["bairro"], endereco
    assert endereco["codigo_municipio"] == "3550308", endereco
    assert endereco["uf"] == "SP", endereco
    assert endereco["cep"] == "04704000", endereco


def test_resposta_parcial_da_brasilapi_nao_apaga_endereco() -> None:
    """Resposta 200 sem endereço mantém o que já estava salvo no banco."""
    cliente = acbr_client.AcbrClient()
    resposta = _RespostaFalsa(
        200,
        {
            "razao_social": "PORTUCHO BAR E LANCHONETE LTDA",
            "email": "financeiro@portucho.test",
            "logradouro": "",
            "bairro": "",
            "codigo_municipio_ibge": "",
            "uf": "",
            "cep": "",
        },
    )
    with patch("backend.acbr_client.requests.get", return_value=resposta):
        dados = cliente._dados_cadastro_brasilapi(CNPJ, ENDERECO_LOCAL)

    endereco = dados["endereco"]
    assert dados["email"] == "financeiro@portucho.test", dados
    assert endereco["logradouro"] == ENDERECO_LOCAL["logradouro"], endereco
    assert endereco["codigo_municipio"] == "3550308", endereco
    assert endereco["cep"] == "04704000", endereco


def test_sem_endereco_local_429_continua_erro() -> None:
    """Sem fallback completo, o erro original da BrasilAPI é preservado."""
    cliente = acbr_client.AcbrClient()
    resposta = _RespostaFalsa(429, "Too Many Requests")
    with patch("backend.acbr_client.requests.get", return_value=resposta):
        try:
            cliente._dados_cadastro_brasilapi(CNPJ, {})
        except acbr_client.ErroAcbr as erro:
            assert erro.status == 429, erro.status
        else:
            raise AssertionError("Era esperado ErroAcbr para 429 sem fallback")


def test_le_endereco_do_banco_quando_brasilapi_falha() -> None:
    """Sem `dados_empresa`, o cliente lê a tabela `empresas` como fallback."""
    arquivo = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    arquivo.close()
    engine = create_engine(f"sqlite:///{arquivo.name}")
    sessao_factory = sessionmaker(bind=engine)
    models.Base.metadata.create_all(bind=engine)
    sessao = sessao_factory()
    sessao.add(_empresa_com_endereco())
    sessao.commit()
    sessao.close()

    sessao_original = bdatabase.SessionLocal
    bdatabase.SessionLocal = sessao_factory
    try:
        resposta = _RespostaFalsa(429, "Too Many Requests")
        with patch("backend.acbr_client.requests.get", return_value=resposta):
            dados = acbr_client.AcbrClient()._dados_cadastro_brasilapi(CNPJ)
    finally:
        bdatabase.SessionLocal = sessao_original
        engine.dispose()
        os.unlink(arquivo.name)

    endereco = dados["endereco"]
    assert endereco["logradouro"] == ENDERECO_LOCAL["logradouro"], endereco
    assert endereco["bairro"] == ENDERECO_LOCAL["bairro"], endereco
    assert endereco["codigo_municipio"] == "3550308", endereco
    assert endereco["uf"] == "SP", endereco
    assert endereco["cep"] == "04704000", endereco


def test_aplicar_endereco_nao_apaga_campos_existentes() -> None:
    """Campos vazios da resposta não sobrescrevem o endereço já gravado."""
    empresa = _empresa_com_endereco()
    mudou = backend_main._aplicar_endereco_empresa(
        empresa,
        {
            "logradouro": "",
            "bairro": "",
            "codigo_municipio": "",
            "uf": "sp",
            "cep": "",
        },
    )
    assert mudou is False, mudou
    assert empresa.logradouro == ENDERECO_LOCAL["logradouro"], empresa.logradouro
    assert empresa.bairro == ENDERECO_LOCAL["bairro"], empresa.bairro
    assert empresa.codigo_municipio == "3550308", empresa.codigo_municipio
    assert empresa.uf == "SP", empresa.uf
    assert empresa.cep == "04704000", empresa.cep


def test_brasilapi_429_troca_de_provedor() -> None:
    """429 na BrasilAPI passa para o próximo provedor em vez de insistir no mesmo.

    Regressão do cadastro sem endereço: com o limite de requisições (HTTP 429), a
    consulta antiga repetia a BrasilAPI até desistir e a loja era salva sem
    endereço — a ACBr recusava depois com "Complete o cadastro da empresa".
    """
    backend_main._DADOS_CNPJ_CACHE.pop(CNPJ_RECEITA, None)
    consulta_cadastral.limpar_cache()
    respostas = [
        _RespostaFalsa(429, "Too Many Requests"),
        _RespostaFalsa(200, RECEITA),
    ]
    with patch.object(
        backend_main.requests, "get", side_effect=respostas
    ) as consulta, patch.object(backend_main.time, "sleep") as espera, patch.object(
        consulta_cadastral, "ESPERA_ENTRE_PROVEDORES", 0.01
    ):
        dados = backend_main._consultar_cnpj_brasilapi(CNPJ_RECEITA)
        # O cache da consulta evita uma segunda chamada ao provedor.
        assert consulta_cadastral.consultar_cnpj(CNPJ_RECEITA) == dados
        chamadas = consulta.call_count

    assert chamadas == 2, chamadas
    urls = [chamada.args[0] for chamada in consulta.call_args_list]
    assert "brasilapi.com.br" in urls[0], urls
    assert "minhareceita.org" in urls[1], urls
    # Uma pausa curta antes de trocar de provedor (nunca em rajada).
    assert espera.call_count == 1, espera.call_count
    assert dados["fonte"] == "brasilapi", dados
    assert dados["provedor"] == "minhareceita", dados
    assert dados["nome_loja"] == "Loja Teste", dados
    assert dados["cep"] == "06708415", dados
    assert dados["codigo_municipio"] == "3513009", dados
    assert dados["uf"] == "SP", dados
    # A resposta boa fica no cache: o recadastro não volta a consultar a Receita.
    assert backend_main._DADOS_CNPJ_CACHE.get(CNPJ_RECEITA) == dados
    backend_main._DADOS_CNPJ_CACHE.pop(CNPJ_RECEITA, None)
    consulta_cadastral.limpar_cache()


def test_brasilapi_sempre_429_desiste_sem_estourar() -> None:
    """429 em todos os provedores: devolve None (o cadastro segue) e loga os status."""
    backend_main._DADOS_CNPJ_CACHE.pop(CNPJ_RECEITA, None)
    consulta_cadastral.limpar_cache()
    respostas = [_RespostaFalsa(429, "Too Many Requests") for _ in range(3)]
    with patch.object(
        backend_main.requests, "get", side_effect=respostas
    ) as consulta, patch.object(backend_main.time, "sleep") as espera, patch.object(
        consulta_cadastral, "ESPERA_ENTRE_PROVEDORES", 0.01
    ):
        dados = backend_main._consultar_cnpj_brasilapi(CNPJ_RECEITA)

    assert dados is None, dados
    assert consulta.call_count == backend_main.TENTATIVAS_BRASILAPI, consulta.call_count
    assert espera.call_count == backend_main.TENTATIVAS_BRASILAPI - 1, espera.call_count
    # Nenhum provedor respondeu: a falha NÃO entra no cache (tenta de novo).
    assert backend_main._DADOS_CNPJ_CACHE.get(CNPJ_RECEITA) is None
    consulta_cadastral.limpar_cache()


def test_brasilapi_404_nao_repete() -> None:
    """CNPJ inexistente (404) não é instabilidade: uma tentativa e pronto."""
    backend_main._DADOS_CNPJ_CACHE.pop(CNPJ_RECEITA, None)
    consulta_cadastral.limpar_cache()
    with patch.object(
        backend_main.requests, "get", return_value=_RespostaFalsa(404, "{}")
    ) as consulta, patch.object(backend_main.time, "sleep") as espera:
        dados = backend_main._consultar_cnpj_brasilapi(CNPJ_RECEITA)

    assert dados is None, dados
    assert consulta.call_count == 1, consulta.call_count
    assert espera.call_count == 0, espera.call_count
    consulta_cadastral.limpar_cache()


def test_espera_brasilapi_respeita_retry_after() -> None:
    """O `Retry-After` da BrasilAPI manda (com teto de 10s) na espera entre tentativas."""
    resposta = _RespostaFalsa(429, "Too Many Requests")
    resposta.headers = {"Retry-After": "3"}
    assert backend_main._espera_brasilapi(resposta, 1) >= 3.0

    resposta.headers = {"Retry-After": "999"}
    assert backend_main._espera_brasilapi(resposta, 1) == 10.0

    # Sem o cabeçalho, a espera dobra a cada tentativa.
    resposta.headers = {}
    espera_1 = backend_main._espera_brasilapi(resposta, 1)
    espera_2 = backend_main._espera_brasilapi(resposta, 2)
    assert espera_2 >= espera_1, (espera_1, espera_2)


def test_aviso_receita_orienta_endereco_manual() -> None:
    """Consulta indisponível gera o aviso para o cliente preencher o endereço."""
    aviso = backend_main._aviso_receita({"fonte": "indisponivel"})
    assert isinstance(aviso, str), aviso
    assert "endereço" in aviso.lower(), aviso
    assert backend_main._aviso_receita({}) is not None
    assert backend_main._aviso_receita({"fonte": "brasilapi"}) is None


def main() -> int:
    testes = (
        test_payload_usa_endereco_do_banco,
        test_brasilapi_429_usa_dados_locais,
        test_resposta_parcial_da_brasilapi_nao_apaga_endereco,
        test_sem_endereco_local_429_continua_erro,
        test_le_endereco_do_banco_quando_brasilapi_falha,
        test_aplicar_endereco_nao_apaga_campos_existentes,
        test_brasilapi_429_troca_de_provedor,
        test_brasilapi_sempre_429_desiste_sem_estourar,
        test_brasilapi_404_nao_repete,
        test_espera_brasilapi_respeita_retry_after,
        test_aviso_receita_orienta_endereco_manual,
    )
    falhas = 0
    for teste in testes:
        try:
            teste()
            print(f"OK  {teste.__name__}")
        except Exception:
            falhas += 1
            print(f"FALHOU  {teste.__name__}")
            traceback.print_exc()
    if falhas:
        print(f"\n{falhas} teste(s) falharam.")
        return 1
    print(f"\n{len(testes)} testes passaram.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
