"""Testes do isolamento entre contas nas rotas usadas pela tela "Minhas Lojas".

Cobre os dois vazamentos corrigidos na Fase A:

- ``GET /api/lojas``: exigia nenhuma autenticação e devolvia ``db.query(Empresa).all()``
  (lojas de TODAS as contas). Agora a dependência é ``auth.usuario_atual`` (401 sem
  token) e a consulta filtra ``Empresa.usuario_id``;
- ``GET /api/empresas/consultar-cnpj``: usado pelo autopreenchimento do cadastro. CNPJ
  de OUTRA conta não é exposto (nem consulta a Receita) e CNPJ da própria conta
  devolve o endereço da Receita + o endereço já gravado como fallback.

Não usa internet, BrasilAPI real nem o banco de produção: banco SQLite temporário e
``_consultar_cnpj_brasilapi`` substituído por dublês.

Como rodar (sem backend ligado):
    python tests/test_multitenancy_lojas.py
"""

import sys
import tempfile
import traceback
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from backend import auth
from backend import main as backend_main
from backend.models import Base, Empresa, Usuario

CNPJ_CONTA_A = "07269638000171"
CNPJ_CONTA_B = "50951160000194"
ENDERECO_RECEITA = {
    "razao_social": "PORTUCHO BAR E LANCHONETE LTDA",
    "nome_loja": "Portucho Matriz",
    "municipio": "SAO PAULO",
    "cep": "04101000",
    "uf": "SP",
    "codigo_municipio": "3550308",
    "bairro": "SAUDE",
    "logradouro": "RUA PASSAROS E FLORES",
    "numero": "239",
    "complemento": "",
    "email": "",
    "telefone": "",
    "fonte": "brasilapi",
}


def _banco_com_duas_contas():
    """Banco temporário com 1 loja por conta e devolve (sessão, usuario_a, usuario_b)."""
    arquivo = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    arquivo.close()
    engine = create_engine(f"sqlite:///{arquivo.name}")
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(autocommit=False, autoflush=False, bind=engine)()

    usuario_a = Usuario(
        nome="Conta A", email="a@teste.com", senha_hash="x", email_verificado=True
    )
    usuario_b = Usuario(
        nome="Conta B", email="b@teste.com", senha_hash="x", email_verificado=True
    )
    db.add_all([usuario_a, usuario_b])
    db.commit()
    db.add_all(
        [
            Empresa(
                cnpj=CNPJ_CONTA_A,
                razao_social="EMPRESA A",
                nome_loja="Loja A",
                usuario_id=usuario_a.id,
            ),
            Empresa(
                cnpj=CNPJ_CONTA_B,
                razao_social="EMPRESA B",
                nome_loja="Loja B",
                usuario_id=usuario_b.id,
            ),
        ]
    )
    db.commit()
    return db, usuario_a, usuario_b


def test_lojas_sem_token_responde_401() -> None:
    """Sem cabeçalho Authorization a dependência recusa antes de qualquer consulta."""
    db, _usuario_a, _usuario_b = _banco_com_duas_contas()
    try:
        auth.usuario_atual(credenciais=None, db=db)
    except HTTPException as erro:
        assert erro.status_code == 401, erro.status_code
        assert "login" in str(erro.detail).lower(), erro.detail
    else:
        raise AssertionError("Rota protegida não pode responder sem token.")


def test_lojas_devolve_somente_a_conta_logada() -> None:
    """Cada conta enxerga a própria loja — nunca a lista global."""
    db, usuario_a, usuario_b = _banco_com_duas_contas()
    lojas_a = backend_main.listar_lojas(usuario=usuario_a, db=db)
    lojas_b = backend_main.listar_lojas(usuario=usuario_b, db=db)

    assert [loja["cnpj"] for loja in lojas_a] == [CNPJ_CONTA_A], lojas_a
    assert [loja["cnpj"] for loja in lojas_b] == [CNPJ_CONTA_B], lojas_b
    assert all(loja["cnpj"] != CNPJ_CONTA_B for loja in lojas_a)
    # Nada além de dados cadastrais públicos sai na resposta.
    assert set(lojas_a[0]) == {"id", "cnpj", "nome_loja", "razao_social"}, lojas_a[0]


def test_consultar_cnpj_de_outra_conta_nao_expoe_dados() -> None:
    """CNPJ de outra conta responde `de_outra_conta` sem consultar a Receita."""
    db, usuario_a, _usuario_b = _banco_com_duas_contas()
    chamadas = []

    def brasilapi_falsa(cnpj):
        chamadas.append(cnpj)
        return dict(ENDERECO_RECEITA)

    with patch.object(backend_main, "_consultar_cnpj_brasilapi", brasilapi_falsa):
        dados = backend_main.consultar_cnpj_da_loja(
            cnpj=CNPJ_CONTA_B, usuario=usuario_a, db=db
        )
    assert dados["de_outra_conta"] is True, dados
    assert dados["endereco"] == {}, dados
    assert "outra conta" in str(dados["aviso"]).lower(), dados
    assert chamadas == [], chamadas


def _erro_http(funcao, **kwargs):
    """Chama a rota esperando HTTPException; devolve (status, detalhe)."""
    try:
        funcao(**kwargs)
    except HTTPException as erro:
        return erro.status_code, str(erro.detail)
    raise AssertionError(f"{funcao.__name__} deveria ter levantado HTTPException.")


def _criar_admin(db) -> Usuario:
    """Conta de suporte (`is_admin=True`): a visão de todas as lojas do rodrigo."""
    admin = Usuario(
        nome="Admin Suporte",
        email="admin@teste.com",
        senha_hash="x",
        email_verificado=True,
        is_admin=True,
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return admin


def test_admin_ve_todas_as_lojas_com_o_email_do_dono() -> None:
    """Admin enxerga todas as lojas com `dono_email`; conta comum só a própria."""
    db, usuario_a, usuario_b = _banco_com_duas_contas()
    admin = _criar_admin(db)

    lojas_admin = backend_main.listar_empresas_vinculadas(usuario=admin, db=db)
    assert {loja["cnpj"] for loja in lojas_admin} == {CNPJ_CONTA_A, CNPJ_CONTA_B}
    donos = {loja["cnpj"]: loja["dono_email"] for loja in lojas_admin}
    assert donos[CNPJ_CONTA_A] == usuario_a.email, donos
    assert donos[CNPJ_CONTA_B] == usuario_b.email, donos
    # O dono da loja NÃO muda só porque o admin a está vendo.
    assert {
        loja["cnpj"]: loja["usuario_id"] for loja in lojas_admin
    } == {CNPJ_CONTA_A: usuario_a.id, CNPJ_CONTA_B: usuario_b.id}

    # Conta comum: continua vendo só a própria loja e sem o e-mail de terceiros.
    lojas_a = backend_main.listar_empresas_vinculadas(usuario=usuario_a, db=db)
    assert [loja["cnpj"] for loja in lojas_a] == [CNPJ_CONTA_A], lojas_a
    assert all("dono_email" not in loja for loja in lojas_a), lojas_a
    # A rota do "Testar Conexão" segue restrita ao dono (não vira lista global).
    assert backend_main.listar_lojas(usuario=admin, db=db) == []


def test_pode_gerenciar_loja_libera_admin_e_bloqueia_conta_comum() -> None:
    """A checagem do backend libera o admin em qualquer loja, e só o dono nas demais."""
    db, usuario_a, usuario_b = _banco_com_duas_contas()
    admin = _criar_admin(db)
    loja_a = db.query(Empresa).filter(Empresa.cnpj == CNPJ_CONTA_A).one()
    loja_b = db.query(Empresa).filter(Empresa.cnpj == CNPJ_CONTA_B).one()

    assert backend_main._pode_gerenciar_loja(admin, loja_a) is True
    assert backend_main._pode_gerenciar_loja(admin, loja_b) is True
    assert backend_main._pode_gerenciar_loja(usuario_a, loja_a) is True
    assert backend_main._pode_gerenciar_loja(usuario_a, loja_b) is False
    assert backend_main._pode_gerenciar_loja(usuario_b, loja_a) is False
    # Empresa órfã não é "de ninguém": nenhuma conta comum assume sem cadastro.
    orfa = Empresa(cnpj="11222333000181", razao_social="ORFA", nome_loja="Orfa")
    db.add(orfa)
    db.commit()
    assert backend_main._pode_gerenciar_loja(usuario_a, orfa) is False
    assert backend_main._pode_gerenciar_loja(admin, orfa) is True


def test_consultar_cnpj_de_cliente_mostra_o_dono_para_o_admin() -> None:
    """Admin consulta CNPJ de loja de cliente: vê os dados e o e-mail do dono."""
    db, _usuario_a, usuario_b = _banco_com_duas_contas()
    admin = _criar_admin(db)
    chamadas = []

    def brasilapi_falsa(cnpj):
        chamadas.append(cnpj)
        return dict(ENDERECO_RECEITA)

    with patch.object(backend_main, "_consultar_cnpj_brasilapi", brasilapi_falsa):
        dados = backend_main.consultar_cnpj_da_loja(
            cnpj=CNPJ_CONTA_B, usuario=admin, db=db
        )
    assert dados["encontrado"] is True, dados
    assert dados["de_outra_conta"] is True, dados
    assert dados["dono_email"] == usuario_b.email, dados
    assert dados["razao_social"] == ENDERECO_RECEITA["razao_social"], dados
    assert chamadas == [CNPJ_CONTA_B], chamadas


def test_consultar_cnpj_proprio_traz_endereco_da_receita() -> None:
    """Loja da conta: razão social + endereço no formato do payload da ACBr."""
    db, usuario_a, _usuario_b = _banco_com_duas_contas()
    with patch.object(
        backend_main, "_consultar_cnpj_brasilapi", lambda cnpj: dict(ENDERECO_RECEITA)
    ):
        dados = backend_main.consultar_cnpj_da_loja(
            cnpj=CNPJ_CONTA_A, usuario=usuario_a, db=db
        )
    assert dados["encontrado"] is True, dados
    assert dados["da_conta"] is True, dados
    assert dados["fonte"] == "brasilapi", dados
    assert dados["razao_social"] == "PORTUCHO BAR E LANCHONETE LTDA", dados
    assert dados["endereco"]["logradouro"] == "RUA PASSAROS E FLORES", dados
    assert dados["endereco"]["codigo_municipio"] == "3550308", dados
    assert dados["aviso"] is None, dados


def test_consultar_cnpj_sem_receita_usa_endereco_gravado() -> None:
    """Receita fora do ar: o endereço já gravado na loja completa a resposta."""
    db, usuario_a, _usuario_b = _banco_com_duas_contas()
    empresa = db.query(Empresa).filter(Empresa.cnpj == CNPJ_CONTA_A).one()
    empresa.cep = "04101000"
    empresa.logradouro = "RUA SALVA NO BANCO"
    empresa.numero = "100"
    empresa.bairro = "CENTRO"
    empresa.uf = "SP"
    empresa.codigo_municipio = "3550308"
    empresa.municipio = "COTIA"
    db.commit()

    with patch.object(backend_main, "_consultar_cnpj_brasilapi", lambda cnpj: None):
        dados = backend_main.consultar_cnpj_da_loja(
            cnpj=CNPJ_CONTA_A, usuario=usuario_a, db=db
        )
    assert dados["encontrado"] is False, dados
    assert dados["fonte"] == "indisponivel", dados
    assert dados["endereco"]["logradouro"] == "RUA SALVA NO BANCO", dados
    assert dados["endereco"]["cidade"] == "COTIA", dados
    # O placeholder "Loja Exemplo" da consulta falha NÃO pode ir para a tela.
    assert dados["nome_loja"] == "", dados
    assert dados["razao_social"] == "", dados
    assert dados["aviso"], dados


def test_consultar_cnpj_invalido_responde_400() -> None:
    """CNPJ com menos de 14 dígitos é recusado antes de qualquer consulta."""
    db, usuario_a, _usuario_b = _banco_com_duas_contas()
    try:
        backend_main.consultar_cnpj_da_loja(cnpj="123", usuario=usuario_a, db=db)
    except HTTPException as erro:
        assert erro.status_code == 400, erro.status_code
    else:
        raise AssertionError("CNPJ inválido deveria responder 400.")


def test_fonte_notas_derivada_e_json_sem_segredo() -> None:
    """Loja antiga (coluna nula) deriva a fonte e o JSON não expõe credencial."""
    db, _usuario_a, _usuario_b = _banco_com_duas_contas()
    loja = db.query(Empresa).filter(Empresa.cnpj == CNPJ_CONTA_A).one()

    # Sem fonte gravada e sem credencial: fluxo histórico = certificado A1.
    assert backend_main._fonte_notas_da_loja(loja) == "certificado"
    dados = backend_main._empresa_json(loja)
    assert dados["fonte_notas"] == "certificado", dados
    assert dados["cert_concordancia"] is None, dados
    assert dados["omie_configurado"] is False, dados

    # Credencial Omie sem escolha explícita: a leitura passa a indicar Omie.
    loja.omie_app_key = "fernet:v1:chave"
    loja.omie_app_secret = "fernet:v1:segredo"
    db.commit()
    assert backend_main._fonte_notas_da_loja(loja) == "omie"
    dados = backend_main._empresa_json(loja)
    assert dados["omie_configurado"] is True, dados
    # Nem o texto cifrado nem o segredo saem na resposta da tela.
    assert "fernet:v1" not in str(dados), dados

    # A escolha explícita da tela prevalece sobre a derivação.
    loja.fonte_notas = "certificado"
    assert backend_main._fonte_notas_da_loja(loja) == "certificado"


def test_fonte_notas_certificado_exige_concordancia_e_registra() -> None:
    """Certificado só ativa com o aceite e a data/usuário ficam persistidos."""
    db, usuario_a, _usuario_b = _banco_com_duas_contas()
    loja = db.query(Empresa).filter(Empresa.cnpj == CNPJ_CONTA_A).one()

    codigo, detalhe = _erro_http(
        backend_main.definir_fonte_notas_da_loja,
        empresa_id=loja.id,
        dados=backend_main.FonteNotasRequest(fonte="certificado"),
        usuario=usuario_a,
        db=db,
    )
    assert codigo == 400, (codigo, detalhe)
    assert "concord" in detalhe.lower(), detalhe
    assert loja.cert_concordancia_em is None

    resposta = backend_main.definir_fonte_notas_da_loja(
        empresa_id=loja.id,
        dados=backend_main.FonteNotasRequest(
            fonte="certificado", concordancia_sefaz=True
        ),
        usuario=usuario_a,
        db=db,
    )
    assert resposta["sucesso"] is True, resposta
    empresa_json = resposta["empresa"]
    assert empresa_json["fonte_notas"] == "certificado", empresa_json
    concordancia = empresa_json["cert_concordancia"]
    assert concordancia and concordancia["em"], empresa_json
    assert concordancia["por"] == usuario_a.email, concordancia
    # A mensagem avisa que ainda falta enviar o .pfx ao cadastrar a loja.
    assert ".pfx" in resposta["mensagem"], resposta

    db.expire_all()
    persistida = db.get(Empresa, loja.id)
    assert persistida.fonte_notas == "certificado", persistida.fonte_notas
    assert persistida.cert_concordancia_em == concordancia["em"]
    assert persistida.cert_concordancia_por == usuario_a.email


def test_fonte_notas_omie_exige_credencial_e_respeita_dono() -> None:
    """Troca para Omie sem credencial = 400; loja de outra conta = 403."""
    db, usuario_a, usuario_b = _banco_com_duas_contas()
    loja_a = db.query(Empresa).filter(Empresa.cnpj == CNPJ_CONTA_A).one()
    admin = _criar_admin(db)

    codigo, detalhe = _erro_http(
        backend_main.definir_fonte_notas_da_loja,
        empresa_id=loja_a.id,
        dados=backend_main.FonteNotasRequest(fonte="omie"),
        usuario=usuario_a,
        db=db,
    )
    assert codigo == 400, (codigo, detalhe)
    assert "App Key" in detalhe, detalhe

    codigo, _detalhe = _erro_http(
        backend_main.definir_fonte_notas_da_loja,
        empresa_id=loja_a.id,
        dados=backend_main.FonteNotasRequest(fonte="omie"),
        usuario=usuario_b,
        db=db,
    )
    assert codigo == 403, codigo

    loja_a.omie_app_key = "fernet:v1:chave"
    loja_a.omie_app_secret = "fernet:v1:segredo"
    db.commit()
    resposta = backend_main.definir_fonte_notas_da_loja(
        empresa_id=loja_a.id,
        dados=backend_main.FonteNotasRequest(fonte="omie"),
        usuario=usuario_a,
        db=db,
    )
    assert resposta["empresa"]["fonte_notas"] == "omie", resposta

    # Fonte desconhecida é recusada antes de gravar qualquer coisa.
    codigo, _detalhe = _erro_http(
        backend_main.definir_fonte_notas_da_loja,
        empresa_id=loja_a.id,
        dados=backend_main.FonteNotasRequest(fonte="planilha"),
        usuario=usuario_a,
        db=db,
    )
    assert codigo == 400, codigo

    # Admin (suporte) também troca a fonte de uma loja de cliente.
    loja_b = db.query(Empresa).filter(Empresa.cnpj == CNPJ_CONTA_B).one()
    loja_b.omie_app_key = "fernet:v1:chave"
    loja_b.omie_app_secret = "fernet:v1:segredo"
    db.commit()
    resposta_admin = backend_main.definir_fonte_notas_da_loja(
        empresa_id=loja_b.id,
        dados=backend_main.FonteNotasRequest(fonte="omie"),
        usuario=admin,
        db=db,
    )
    assert resposta_admin["empresa"]["usuario_id"] == usuario_b.id, resposta_admin



def test_excluir_certificado_limpa_concordancia() -> None:
    """Apagar o certificado também apaga a concordância (ela vale para ele)."""
    db, usuario_a, _usuario_b = _banco_com_duas_contas()
    loja = db.query(Empresa).filter(Empresa.cnpj == CNPJ_CONTA_A).one()
    loja.certificado_titular = "EMPRESA A:07269638000171"
    loja.certificado_validade = "31/12/2027"
    loja.certificado_enviado_em = "2026-11-24T09:00:00+00:00"
    loja.cert_concordancia_em = "2026-11-24T09:05:00+00:00"
    loja.cert_concordancia_por = usuario_a.email
    db.commit()

    resposta = backend_main.excluir_certificado_da_empresa(
        empresa_id=loja.id, usuario=usuario_a, db=db
    )
    assert resposta["certificado_excluido"] is True, resposta
    empresa_json = resposta["empresa"]
    assert empresa_json["certificado"] is None, empresa_json
    assert empresa_json["cert_concordancia"] is None, empresa_json
    db.expire_all()
    persistida = db.get(Empresa, loja.id)
    assert persistida.cert_concordancia_em is None
    assert persistida.cert_concordancia_por is None


def test_cadastro_com_fonte_omie_dispensa_certificado() -> None:
    """Vincular loja para o Omie não exige .pfx; o certificado continua barrado."""
    import asyncio

    db, usuario_a, _usuario_b = _banco_com_duas_contas()
    cnpj_novo = "33445566000177"
    with patch.object(
        backend_main, "_consultar_cnpj", lambda cnpj: dict(ENDERECO_RECEITA)
    ):
        resposta = asyncio.run(
            backend_main.cadastrar_empresa(
                usuario=usuario_a,
                db=db,
                cnpj=cnpj_novo,
                certificado=None,
                fonte_notas="omie",
            )
        )
    assert resposta["sucesso"] is True, resposta
    empresa_json = resposta["empresa"]
    assert empresa_json["cnpj"] == cnpj_novo, empresa_json
    assert empresa_json["usuario_id"] == usuario_a.id, empresa_json
    assert empresa_json["fonte_notas"] == "omie", empresa_json
    assert empresa_json["certificado"] is None, empresa_json
    assert empresa_json["cert_concordancia"] is None, empresa_json

    # Fonte desconhecida é recusada.
    outra = "77889900000155"
    try:
        asyncio.run(
            backend_main.cadastrar_empresa(
                usuario=usuario_a,
                db=db,
                cnpj=outra,
                certificado=None,
                fonte_notas="planilha",
            )
        )
    except HTTPException as erro:
        assert erro.status_code == 400, erro.status_code
    else:
        raise AssertionError("Fonte inválida deveria responder 400.")

    # Sem o aceite (false explícito) o certificado nem é validado.
    try:
        asyncio.run(
            backend_main.cadastrar_empresa(
                usuario=usuario_a,
                db=db,
                cnpj=outra,
                certificado=None,
                fonte_notas="certificado",
                concordancia_sefaz=False,
            )
        )
    except HTTPException as erro:
        assert erro.status_code == 400, erro.status_code
        assert erro.detail == backend_main.certificados.ERRO_SEM_CONCORDANCIA
    else:
        raise AssertionError("Certificado sem concordância deveria responder 400.")



def main() -> int:
    testes = (
        test_lojas_sem_token_responde_401,
        test_lojas_devolve_somente_a_conta_logada,
        test_admin_ve_todas_as_lojas_com_o_email_do_dono,
        test_pode_gerenciar_loja_libera_admin_e_bloqueia_conta_comum,
        test_consultar_cnpj_de_outra_conta_nao_expoe_dados,
        test_consultar_cnpj_de_cliente_mostra_o_dono_para_o_admin,
        test_consultar_cnpj_proprio_traz_endereco_da_receita,
        test_consultar_cnpj_sem_receita_usa_endereco_gravado,
        test_consultar_cnpj_invalido_responde_400,
        test_fonte_notas_derivada_e_json_sem_segredo,
        test_fonte_notas_certificado_exige_concordancia_e_registra,
        test_fonte_notas_omie_exige_credencial_e_respeita_dono,
        test_excluir_certificado_limpa_concordancia,
        test_cadastro_com_fonte_omie_dispensa_certificado,
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
