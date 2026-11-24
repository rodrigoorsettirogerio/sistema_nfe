"""Testes das rotas Omie da fase 3a e do agendamento diário.

Prova, SEM chamar a API do Omie e SEM tocar na base real (SQLite temporário):

1. ``POST /api/omie/credenciais`` grava app_key/app_secret CIFRADOS
   (``fernet:v1:...``), devolve só os 4 últimos caracteres e recusa loja de
   outra conta (403), payload vazio (400) e CNPJ inexistente (404);
2. ``GET /api/omie/status/{cnpj}`` devolve ``omie_sync_em``, as contagens da
   última sincronização e se há credencial — sem nunca expor o segredo;
   credencial ilegível (chave do Fernet trocada) vira ``erro_credenciais``;
3. ``POST /api/omie/sincronizar`` usa a rotina da fase 2 com a janela padrão,
   respeita datas/etapas/simulação, grava o resumo no banco e traduz os erros
   (credencial ausente/ilegível/período = 400, recusa do Omie = 502);
4. o agendador roda UMA vez por dia por loja com credencial, com pausa entre
   lojas, não repete a tentativa no mesmo dia e trata o HTTP 500 "Não existem
   registros" como "sem dados" (não é erro fatal);
5. o cliente Omie converte esse 500 em lista vazia nas listagens.

Como rodar (na raiz do projeto, sem backend ligado):
    python tests/test_omie_rotas.py
"""

import json
import os
import shutil
import sys
import tempfile
import traceback
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from backend import agendador
from backend import cripto_credenciais as bcripto
from backend import main as backend_main
from backend import omie_sync as bsync
from backend.models import Base, Empresa, Usuario
from backend.omie_client import ErroOmie, OmieClient, OmieSemRegistros, sem_registros

CNPJ_A = "11222333000181"
CNPJ_B = "50951160000194"
CNPJ_SEM_CADASTRO = "99888777000166"
CHAVE_CRIPTO = "segredo-de-teste-omie-rotas-0123456789"
APP_KEY = "APPKEY-ABCD1234"
APP_SECRET = "SEGREDO-SUPER-9876"

DIRETORIO = tempfile.mkdtemp(prefix="omie_rotas_teste_")
_MOTORES = []


def _motor(nome):
    """Engine SQLite temporária com TODAS as tabelas (mesmo esquema do banco)."""
    caminho = Path(DIRETORIO, nome).as_posix()
    motor = create_engine(f"sqlite:///{caminho}")
    Base.metadata.create_all(bind=motor)
    _MOTORES.append(motor)
    return motor


def _banco(nome):
    """Sessão no banco temporário (o teste é dono do arquivo)."""
    return sessionmaker(autocommit=False, autoflush=False, bind=_motor(nome))()


def _criar_contas(db):
    """Dono, outra conta e admin com uma loja cada — devolve (db, ...)."""
    dono = Usuario(
        nome="Dono", email="dono@teste.com", senha_hash="x", email_verificado=True
    )
    outra = Usuario(
        nome="Outra", email="outra@teste.com", senha_hash="x", email_verificado=True
    )
    admin = Usuario(
        nome="Admin",
        email="admin@teste.com",
        senha_hash="x",
        email_verificado=True,
        is_admin=True,
    )
    db.add_all([dono, outra, admin])
    db.commit()
    loja = Empresa(
        cnpj=CNPJ_A,
        razao_social="LOJA TESTE LTDA",
        nome_loja="Loja Teste",
        usuario_id=dono.id,
    )
    loja_outra = Empresa(
        cnpj=CNPJ_B,
        razao_social="OUTRA LOJA LTDA",
        nome_loja="Outra Loja",
        usuario_id=outra.id,
    )
    db.add_all([loja, loja_outra])
    db.commit()
    db.refresh(loja)
    db.refresh(loja_outra)
    return db, dono, outra, admin, loja, loja_outra


def _erro_http(funcao, **kwargs):
    """Chama a rota esperando HTTPException; devolve (status, detalhe)."""
    try:
        funcao(**kwargs)
    except HTTPException as erro:
        return erro.status_code, str(erro.detail)
    raise AssertionError(f"{funcao.__name__} deveria ter levantado HTTPException.")


def _resumo_fake(empresa, novas=2, itens=5, simulado=False):
    """Retorno no formato de ``omie_sync.sincronizar_recebimentos``."""
    if not simulado:
        empresa.omie_sync_em = "2026-11-24T09:00:00+00:00"
    return {
        "empresa_id": empresa.id,
        "empresa": empresa.nome_loja,
        "cnpj": empresa.cnpj,
        "periodo": ("2026-10-26", "2026-11-24"),
        "etapas": {"60": {"rotulo": "Recebido", "recebimentos": 3}},
        "recebimentos": 3,
        "novas": novas,
        "duplicadas": 1,
        "canceladas": 0,
        "ignoradas": 0,
        "itens": itens,
        "bonificados": 0,
        "notas_sem_itens": 0,
        "fornecedores_novos": 1,
        "fornecedores_atualizados": 0,
        "mapeamentos_criados": 0,
        "mapeamentos_usados": 0,
        "notas_novas": [],
        "erros": [],
        "origem_credenciais": "empresa",
        "simulado": simulado,
        "sincronizado_em": empresa.omie_sync_em,
    }


def test_credenciais_grava_cifrado_e_devolve_so_a_mascara():
    """A rota cifra no banco e devolve apenas os 4 últimos caracteres."""
    db, dono, _outra, _admin, loja, _loja_outra = _criar_contas(_banco("cred.db"))
    resposta = backend_main.salvar_credenciais_omie(
        dados=backend_main.CredenciaisOmieRequest(
            cnpj="11.222.333/0001-81", app_key=APP_KEY, app_secret=APP_SECRET
        ),
        usuario=dono,
        db=db,
    )
    texto = json.dumps(resposta)
    assert APP_SECRET not in texto, resposta
    assert APP_KEY not in texto, resposta
    assert resposta["app_key"].startswith("*") and resposta["app_key"].endswith("1234")
    assert resposta["app_secret"].endswith("9876"), resposta
    assert resposta["configurado"] is True, resposta
    # Gravar a credencial também ativa a fonte Omie da loja (seletor da tela).
    assert resposta["fonte_notas"] == "omie", resposta

    db.expire_all()
    salva = db.get(Empresa, loja.id)
    assert salva.omie_app_key.startswith(bcripto.PREFIXO), salva.omie_app_key
    assert salva.omie_app_secret.startswith(bcripto.PREFIXO), salva.omie_app_secret
    assert salva.fonte_notas == "omie", salva.fonte_notas
    assert APP_SECRET not in (salva.omie_app_key + salva.omie_app_secret)
    assert bcripto.descriptografar(salva.omie_app_secret) == APP_SECRET

    situacao = backend_main._situacao_credenciais_omie(salva)
    assert situacao["configurado"] is True, situacao
    assert situacao["origem"] == "empresa", situacao
    assert situacao["app_secret_configurado"] is True, situacao
    assert APP_SECRET not in json.dumps(situacao), situacao


def test_credenciais_respeitam_dono_payload_e_cnpj():
    """403 para loja de outra conta, 400 payload/CNPJ e 404 loja inexistente."""
    db, dono, outra, admin, loja, loja_outra = _criar_contas(_banco("perm.db"))
    dados = backend_main.CredenciaisOmieRequest(
        cnpj=CNPJ_A, app_key=APP_KEY, app_secret=APP_SECRET
    )
    codigo, detalhe = _erro_http(
        backend_main.salvar_credenciais_omie, dados=dados, usuario=outra, db=db
    )
    assert codigo == 403, (codigo, detalhe)
    assert "conta" in detalhe.lower(), detalhe

    vazio = backend_main.CredenciaisOmieRequest(
        cnpj=CNPJ_A, app_key="", app_secret=APP_SECRET
    )
    codigo, detalhe = _erro_http(
        backend_main.salvar_credenciais_omie, dados=vazio, usuario=dono, db=db
    )
    assert codigo == 400, (codigo, detalhe)
    assert "App Key" in detalhe, detalhe

    sem_cnpj = backend_main.CredenciaisOmieRequest(
        cnpj="123", app_key=APP_KEY, app_secret=APP_SECRET
    )
    codigo, _detalhe = _erro_http(
        backend_main.salvar_credenciais_omie, dados=sem_cnpj, usuario=dono, db=db
    )
    assert codigo == 400, codigo

    inexistente = backend_main.CredenciaisOmieRequest(
        cnpj=CNPJ_SEM_CADASTRO, app_key=APP_KEY, app_secret=APP_SECRET
    )
    codigo, _detalhe = _erro_http(
        backend_main.salvar_credenciais_omie, dados=inexistente, usuario=dono, db=db
    )
    assert codigo == 404, codigo

    # Admin (suporte) grava na loja de outra conta; o dono da loja continua sendo outro.
    resposta = backend_main.salvar_credenciais_omie(
        dados=backend_main.CredenciaisOmieRequest(
            cnpj=CNPJ_B, app_key=APP_KEY, app_secret=APP_SECRET
        ),
        usuario=admin,
        db=db,
    )
    assert resposta["cnpj"] == CNPJ_B, resposta
    db.expire_all()
    assert loja.id != loja_outra.id
    assert db.get(Empresa, loja_outra.id).usuario_id == outra.id
    assert backend_main.status_omie(cnpj=CNPJ_B, usuario=admin, db=db)[
        "loja_configurada"
    ] is True


def test_status_devolve_contagens_e_nunca_o_segredo():
    """Status: omie_sync_em + contagens + indicadores, sem app_secret."""
    db, dono, outra, _admin, loja, loja_outra = _criar_contas(_banco("status.db"))
    backend_main.salvar_credenciais_omie(
        dados=backend_main.CredenciaisOmieRequest(
            cnpj=CNPJ_A, app_key=APP_KEY, app_secret=APP_SECRET
        ),
        usuario=dono,
        db=db,
    )
    loja.omie_sync_em = "2026-11-24T06:00:00+00:00"
    db.commit()
    resumo = _resumo_fake(loja)
    resumo["sincronizado_em"] = "2026-11-24T06:00:00+00:00"
    loja.omie_sync_em = resumo["sincronizado_em"]
    db.commit()
    backend_main._registrar_sincronizacao_omie(db, loja.cnpj, resumo)

    status = backend_main.status_omie(cnpj=CNPJ_A, usuario=dono, db=db)
    assert status["omie_sync_em"] == "2026-11-24T06:00:00+00:00", status
    assert status["fonte_notas"] == "omie", status
    assert status["contagens"]["novas"] == 2, status
    assert status["contagens"]["itens"] == 5, status
    assert status["ultima_sincronizacao"]["periodo"] == [
        "2026-10-26",
        "2026-11-24",
    ], status
    assert status["origem_credenciais"] == "empresa", status
    assert status["app_secret_configurado"] is True, status
    assert APP_SECRET not in json.dumps(status), status
    assert APP_KEY not in json.dumps(status), status

    # Loja sem credencial e sem histórico (o .env está vazio no teste).
    vazio = backend_main.status_omie(cnpj=CNPJ_B, usuario=outra, db=db)
    assert vazio["configurado"] is False, vazio
    assert vazio["origem_credenciais"] is None, vazio
    assert vazio["ultima_sincronizacao"] is None, vazio
    assert vazio["contagens"] is None, vazio
    assert vazio["omie_sync_em"] is None, vazio

    # Credencial ilegível (chave do Fernet trocada) não derruba o status.
    quebrada = db.get(Empresa, loja_outra.id)
    quebrada.omie_app_key = bcripto.PREFIXO + "token-invalido"
    quebrada.omie_app_secret = bcripto.PREFIXO + "token-invalido"
    db.commit()
    resposta = backend_main.status_omie(cnpj=CNPJ_B, usuario=outra, db=db)
    assert resposta["loja_configurada"] is False, resposta
    assert "Regrave" in str(resposta["erro_credenciais"]), resposta
    assert "token-invalido" not in json.dumps(resposta), resposta



def test_sincronizar_usa_padrao_e_persiste_o_resumo():
    """Janela padrão (30 dias), resumo gravado e omie_sync_em atualizado."""
    db, dono, _outra, _admin, loja, _loja_outra = _criar_contas(_banco("sync.db"))
    backend_main.salvar_credenciais_omie(
        dados=backend_main.CredenciaisOmieRequest(
            cnpj=CNPJ_A, app_key=APP_KEY, app_secret=APP_SECRET
        ),
        usuario=dono,
        db=db,
    )
    chamadas = []

    def sync_falsa(sessao, empresa, **kwargs):
        chamadas.append({"empresa_id": empresa.id, **kwargs})
        return _resumo_fake(empresa)

    with patch.object(backend_main.omie_sync, "sincronizar_recebimentos", sync_falsa):
        resposta = backend_main.sincronizar_omie(
            dados=backend_main.SincronizacaoOmieRequest(cnpj="11.222.333/0001-81"),
            usuario=dono,
            db=db,
        )
    assert chamadas == [
        {"empresa_id": loja.id, "dias": bsync.DIAS_PADRAO, "confirmar": True}
    ], chamadas
    assert resposta["sucesso"] is True, resposta
    assert resposta["contagens"]["novas"] == 2, resposta
    assert resposta["contagens"]["itens"] == 5, resposta
    assert resposta["ultima_sincronizacao"]["contagens"]["novas"] == 2, resposta
    assert APP_SECRET not in json.dumps(resposta), resposta

    db.expire_all()
    assert db.get(Empresa, loja.id).omie_sync_em == "2026-11-24T09:00:00+00:00"
    status = backend_main.status_omie(cnpj=CNPJ_A, usuario=dono, db=db)
    assert status["contagens"]["novas"] == 2, status
    assert status["omie_sync_em"] == "2026-11-24T09:00:00+00:00", status


def test_sincronizar_datas_etapas_e_simulacao():
    """Datas explícitas viram período; simular roda com confirmar=False."""
    db, dono, _outra, _admin, loja, _loja_outra = _criar_contas(_banco("sync2.db"))
    backend_main.salvar_credenciais_omie(
        dados=backend_main.CredenciaisOmieRequest(
            cnpj=CNPJ_A, app_key=APP_KEY, app_secret=APP_SECRET
        ),
        usuario=dono,
        db=db,
    )
    chamadas = []

    def sync_falsa(sessao, empresa, **kwargs):
        chamadas.append(kwargs)
        return _resumo_fake(
            empresa, simulado=bool(kwargs.get("confirmar") is False)
        )

    pedido = backend_main.SincronizacaoOmieRequest(
        cnpj=CNPJ_A,
        data_inicio=date(2026, 11, 1),
        data_fim=date(2026, 11, 10),
        etapas=["60", "80"],
    )
    with patch.object(backend_main.omie_sync, "sincronizar_recebimentos", sync_falsa):
        resposta = backend_main.sincronizar_omie(dados=pedido, usuario=dono, db=db)
    assert chamadas[-1] == {
        "dias": None,
        "confirmar": True,
        "data_inicio": date(2026, 11, 1),
        "data_fim": date(2026, 11, 10),
        "etapas": ["60", "80"],
    }, chamadas[-1]
    assert resposta["simulado"] is False, resposta

    # Simulação: confirmar=False e NADA de resumo/omie_sync_em.
    resumo_antes = backend_main._ultima_sincronizacao_omie(db, loja.cnpj)
    with patch.object(backend_main.omie_sync, "sincronizar_recebimentos", sync_falsa):
        simulado = backend_main.sincronizar_omie(
            dados=backend_main.SincronizacaoOmieRequest(cnpj=CNPJ_A, simular=True),
            usuario=dono,
            db=db,
        )
    assert chamadas[-1]["confirmar"] is False, chamadas[-1]
    assert chamadas[-1]["dias"] == bsync.DIAS_PADRAO, chamadas[-1]
    assert simulado["simulado"] is True, simulado
    assert backend_main._ultima_sincronizacao_omie(db, loja.cnpj) == resumo_antes
    db.expire_all()
    # A simulação não avançou o marcador da loja.
    assert db.get(Empresa, loja.id).omie_sync_em == "2026-11-24T09:00:00+00:00"


def test_sincronizar_periodo_meio_aberto_responde_400():
    """Só uma das datas é recusada antes de chamar a API."""
    db, dono, _outra, _admin, _loja, _loja_outra = _criar_contas(_banco("sync3.db"))
    pedido = backend_main.SincronizacaoOmieRequest(
        cnpj=CNPJ_A, data_inicio=date(2026, 11, 1)
    )
    codigo, detalhe = _erro_http(
        backend_main.sincronizar_omie, dados=pedido, usuario=dono, db=db
    )
    assert codigo == 400, (codigo, detalhe)
    assert "duas datas" in detalhe, detalhe


def test_sincronizar_traduz_os_erros():
    """Credencial/período = 400; recusa do Omie = 502 (sem segredo no detalhe)."""
    db, dono, _outra, _admin, loja, _loja_outra = _criar_contas(_banco("erros.db"))
    pedido = backend_main.SincronizacaoOmieRequest(cnpj=CNPJ_A)

    # Loja sem credencial e .env vazio: a própria fase 2 levanta OmieNaoConfigurado.
    codigo, detalhe = _erro_http(
        backend_main.sincronizar_omie, dados=pedido, usuario=dono, db=db
    )
    assert codigo == 400, (codigo, detalhe)
    assert "credencia" in detalhe.lower(), detalhe

    cenarios = (
        (bsync.ErroSincronizacao("período inválido"), 400, "período inválido"),
        (bcripto.ErroCredenciais("Regrave as credenciais"), 400, "Regrave"),
        (
            ErroOmie("API do Omie recusou ListarRecebimentos (HTTP 500)", status=500),
            502,
            "API do Omie",
        ),
    )
    for erro, esperado, trecho in cenarios:

        def sync_que_falha(*_args, _erro=erro, **_kwargs):
            raise _erro

        with patch.object(
            backend_main.omie_sync, "sincronizar_recebimentos", sync_que_falha
        ):
            codigo, detalhe = _erro_http(
                backend_main.sincronizar_omie, dados=pedido, usuario=dono, db=db
            )
        assert codigo == esperado, (erro, codigo, detalhe)
        assert trecho in detalhe, (erro, detalhe)
        assert APP_SECRET not in detalhe, detalhe
    db.expire_all()
    assert db.get(Empresa, loja.id).omie_sync_em is None



def _preparar_agendador(nome):
    """Engine + lojas com/sem credencial para o ciclo Omie do agendador."""
    motor = _motor(nome)
    db = sessionmaker(autocommit=False, autoflush=False, bind=motor)()
    _db, _dono, _outra, _admin, loja_a, loja_b = _criar_contas(db)
    bsync.definir_credenciais(db, loja_a, "APP-KEY-A", "APP-SECRET-A")
    bsync.definir_credenciais(db, loja_b, "APP-KEY-B", "APP-SECRET-B")
    return motor, db, loja_a, loja_b


def _agora_do_agendador():
    """Instante dentro da hora configurada (24/11/2026, fuso de Brasília)."""
    return datetime(
        2026, 11, 24, agendador.hora_omie_configurada(), 5, tzinfo=agendador.FUSO_BRASILIA
    )


def test_agendador_roda_uma_vez_por_loja_com_credencial():
    """Só lojas com credencial; a já sincronizada hoje é pulada."""
    motor, db, loja_a, loja_b = _preparar_agendador("agendador.db")
    agora = _agora_do_agendador()
    loja_a.omie_sync_em = agora.astimezone(timezone.utc).isoformat(timespec="seconds")
    db.commit()
    chamadas = []

    def callback(sessao, empresa):
        chamadas.append(empresa.cnpj)
        return {"novas": 1, "itens": 4, "erros": []}

    with patch.object(agendador, "SessionLocal", sessionmaker(bind=motor)):
        resultados = agendador.executar_ciclo_omie(
            agora=agora, sincronizar=callback, pausa=0, tentativas={}
        )
    assert chamadas == [loja_b.cnpj], chamadas
    assert len(resultados) == 2, resultados
    assert resultados[0]["motivo"] == "já sincronizada hoje", resultados[0]
    assert resultados[1]["sincronizado"] is True, resultados[1]
    assert resultados[1]["novas"] == 1 and resultados[1]["itens"] == 4, resultados[1]


def test_agendador_nao_repete_tentativa_no_mesmo_dia_e_respeita_hora():
    """Fora do horário não chama; mesma loja não é tentada duas vezes no dia."""
    motor, db, loja_a, loja_b = _preparar_agendador("agendador2.db")
    agora = _agora_do_agendador()
    chamadas = []

    def callback(_sessao, empresa):
        chamadas.append(empresa.cnpj)
        return {"novas": 0, "itens": 0, "erros": []}

    with patch.object(agendador, "SessionLocal", sessionmaker(bind=motor)):
        fora = agendador.executar_ciclo_omie(
            agora=agora.replace(hour=(agora.hour + 1) % 24),
            sincronizar=callback,
            pausa=0,
            tentativas={},
        )
        assert fora == [], fora

        tentativas = {}
        primeira = agendador.executar_ciclo_omie(
            agora=agora, sincronizar=callback, pausa=0, tentativas=tentativas
        )
        segunda = agendador.executar_ciclo_omie(
            agora=agora, sincronizar=callback, pausa=0, tentativas=tentativas
        )
    assert chamadas == [loja_a.cnpj, loja_b.cnpj], chamadas
    assert [item["sincronizado"] for item in primeira] == [True, True], primeira
    assert all(item["motivo"] == "já tentada hoje" for item in segunda), segunda
    assert loja_a.id != loja_b.id


def test_agendador_trata_500_sem_registros_como_sem_dados():
    """HTTP 500 "Não existem registros" não é erro: a loja fica sem dados."""
    motor, _db, _loja_a, _loja_b = _preparar_agendador("agendador3.db")
    agora = _agora_do_agendador()
    mensagem = (
        "API do Omie recusou ListarRecebimentos (HTTP 500): "
        "ERROR: Não existem registros para a página [1]!"
    )

    def callback(_sessao, _empresa):
        raise OmieSemRegistros(mensagem, status=500)

    with patch.object(agendador, "SessionLocal", sessionmaker(bind=motor)):
        resultados = agendador.executar_ciclo_omie(
            agora=agora, sincronizar=callback, pausa=0, tentativas={}
        )
    assert len(resultados) == 2, resultados
    for item in resultados:
        assert item["sincronizado"] is True, item
        assert item["sem_dados"] is True, item
        assert "erro" not in item, item
        assert "Não existem registros" in item["motivo"], item

    # Um 500 de verdade (não é o "sem registros") continua sendo erro.
    def callback_falha(_sessao, _empresa):
        raise ErroOmie("API do Omie recusou (HTTP 500): falha inesperada", status=500)

    with patch.object(agendador, "SessionLocal", sessionmaker(bind=motor)):
        resultados = agendador.executar_ciclo_omie(
            agora=agora, sincronizar=callback_falha, pausa=0, tentativas={}
        )
    assert all(item["sincronizado"] is False for item in resultados), resultados
    assert all(item.get("erro") for item in resultados), resultados



def test_cliente_traduz_500_sem_registros_em_lista_vazia():
    """O cliente devolve [] (não exceção) quando a etapa/período está vazia."""
    cliente = OmieClient("app-key", "app-secret")

    def pagina_vazia(*_args, **_kwargs):
        raise OmieSemRegistros("HTTP 500: Não existem registros", status=500)

    with patch.object(OmieClient, "_pagina_recebimentos", pagina_vazia):
        assert cliente.listar_recebimentos() == []
        assert cliente.listar_todos_recebimentos() == []
    with patch.object(OmieClient, "_pagina_documentos", pagina_vazia):
        assert cliente.listar_documentos_entrada("01/11/2026", "10/11/2026") == []
        assert (
            cliente.listar_todos_documentos_entrada("01/11/2026", "10/11/2026") == []
        )

    assert sem_registros(OmieSemRegistros("x", status=500)) is True
    assert sem_registros(ErroOmie("x", status=425)) is False
    assert sem_registros(ErroOmie("x", status=500)) is False


def test_chamar_classifica_o_500_sem_registros():
    """``_chamar`` usa OmieSemRegistros só quando o corpo cita a mensagem."""

    class RespostaFalsa:
        def __init__(self, texto):
            self.ok = False
            self.status_code = 500
            self.text = texto

        def json(self):
            return {"faultstring": self.text}

    cliente = OmieClient("app-key", "app-secret")
    with patch(
        "backend.omie_client.requests.post",
        lambda *a, **k: RespostaFalsa(
            "ERROR: Não existem registros para a página [1]!"
        ),
    ):
        try:
            cliente._chamar("http://omie.teste", "ListarRecebimentos", {})
            raise AssertionError("deveria levantar OmieSemRegistros")
        except OmieSemRegistros as erro:
            assert erro.status == 500, erro.status
            assert sem_registros(erro) is True

    with patch(
        "backend.omie_client.requests.post",
        lambda *a, **k: RespostaFalsa("ERROR: token inválido"),
    ):
        try:
            cliente._chamar("http://omie.teste", "ListarRecebimentos", {})
            raise AssertionError("deveria levantar ErroOmie")
        except OmieSemRegistros:
            raise AssertionError("token inválido não é 'sem registros'")
        except ErroOmie as erro:
            assert sem_registros(erro) is False


def main() -> int:
    ambiente = {
        nome: os.environ.get(nome)
        for nome in (
            "OMIE_ENC_KEY",
            "OMIE_APP_KEY",
            "OMIE_APP_SECRET",
            "NFE_OMIE_HORA",
            "NFE_OMIE_AGENDADOR",
        )
    }
    os.environ["OMIE_ENC_KEY"] = CHAVE_CRIPTO
    # Nenhuma credencial do .env: o teste controla o que cada loja tem.
    os.environ["OMIE_APP_KEY"] = ""
    os.environ["OMIE_APP_SECRET"] = ""
    os.environ.pop("NFE_OMIE_HORA", None)

    testes = (
        test_credenciais_grava_cifrado_e_devolve_so_a_mascara,
        test_credenciais_respeitam_dono_payload_e_cnpj,
        test_status_devolve_contagens_e_nunca_o_segredo,
        test_sincronizar_usa_padrao_e_persiste_o_resumo,
        test_sincronizar_datas_etapas_e_simulacao,
        test_sincronizar_periodo_meio_aberto_responde_400,
        test_sincronizar_traduz_os_erros,
        test_agendador_roda_uma_vez_por_loja_com_credencial,
        test_agendador_nao_repete_tentativa_no_mesmo_dia_e_respeita_hora,
        test_agendador_trata_500_sem_registros_como_sem_dados,
        test_cliente_traduz_500_sem_registros_em_lista_vazia,
        test_chamar_classifica_o_500_sem_registros,
    )
    falhas = 0
    try:
        for teste in testes:
            try:
                teste()
                print(f"OK  {teste.__name__}")
            except Exception:
                falhas += 1
                print(f"FALHOU  {teste.__name__}")
                traceback.print_exc()
    finally:
        for nome, valor in ambiente.items():
            if valor is None:
                os.environ.pop(nome, None)
            else:
                os.environ[nome] = valor
        for motor in _MOTORES:
            motor.dispose()
        shutil.rmtree(DIRETORIO, ignore_errors=True)
    if falhas:
        print(f"\n{falhas} teste(s) falharam.")
        return 1
    print(f"\n{len(testes)} testes passaram.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

