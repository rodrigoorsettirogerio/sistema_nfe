"""Testes da confirmação de e-mail no cadastro (código de 6 dígitos).

Cobre a Fase B1 do fluxo de cadastro:

- ``POST /api/auth/registrar`` cria a conta SEM sessão e envia o código por SMTP;
- ``POST /api/auth/login`` responde 403 ``email_nao_verificado`` até confirmar;
- ``POST /api/auth/validar-email`` recusa código errado (conta tentativas), expirado e
  sem código pendente; com o código certo devolve o token e cria a configuração padrão
  de alerta (ativo, canal e-mail, destinatário = e-mail da conta);
- ``POST /api/auth/reenviar-codigo`` respeita 1 código por minuto e 10 por hora;
- falha de SMTP não apaga a conta: a resposta traz ``email_enviado=False`` + aviso.

Não usa internet, SMTP real nem o banco de produção: o banco é um SQLite temporário e
``backend.main.send_email`` é substituído por um dublê que só registra as mensagens.

Como rodar (sem backend ligado):
    python tests/test_verificacao_email.py
"""

import re
import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

from backend import auth
from backend import main as backend_main
from backend.models import Base, CodigoEmail, ConfiguracaoAlerta, Usuario

SENHA = "senha-segura-123"


class CaixaDeSaida:
    """Dublê de `send_email`: guarda (destino, assunto, corpo) e nunca abre socket."""

    def __init__(self):
        self.mensagens = []

    def __call__(self, to_email, subject, body, cc_email=""):
        self.mensagens.append(
            {"para": to_email, "assunto": subject, "corpo": body, "cc": cc_email}
        )
        return True

    def ultimo_codigo(self) -> str:
        """Código de 6 dígitos do último e-mail enviado."""
        corpo = self.mensagens[-1]["corpo"]
        achado = re.search(r"\b(\d{6})\b", corpo)
        assert achado, corpo
        return achado.group(1)


class SMTPComFalha:
    """Dublê que simula SMTP fora do ar (o envio levanta ValueError)."""

    def __call__(self, *args, **kwargs):
        raise ValueError("SMTP_HOST precisa estar configurado no ambiente.")


def _banco_temporario():
    """Sessão SQLAlchemy em um arquivo temporário com as tabelas do ORM."""
    arquivo = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    arquivo.close()
    engine = create_engine(f"sqlite:///{arquivo.name}")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)()


def _cadastrar(db, caixa, nome, email):
    """Registra a conta com o SMTP dublê e devolve a resposta da rota."""
    with patch.object(backend_main, "send_email", caixa):
        return backend_main.registrar_usuario(
            backend_main.AuthCadastroRequest(nome=nome, email=email, senha=SENHA), db
        )



def test_registrar_sem_sessao_e_envia_codigo() -> None:
    """Cadastro cria a conta pendente, envia 1 e-mail e NÃO devolve token."""
    db = _banco_temporario()
    caixa = CaixaDeSaida()
    resposta = _cadastrar(db, caixa, "Maria Teste", "Maria@Teste.com")

    assert resposta["verificacao_pendente"] is True, resposta
    assert resposta["email_enviado"] is True, resposta
    assert resposta["email"] == "maria@teste.com", resposta
    assert "token" not in resposta, resposta
    assert len(caixa.mensagens) == 1, caixa.mensagens
    assert caixa.mensagens[0]["para"] == "maria@teste.com"
    codigo = caixa.ultimo_codigo()
    assert len(codigo) == 6 and codigo.isdigit(), codigo

    usuario = db.query(Usuario).filter(Usuario.email == "maria@teste.com").first()
    assert usuario is not None
    assert bool(usuario.email_verificado) is False
    # O código vai para o banco apenas como hash (nunca em texto).
    registro = db.query(CodigoEmail).filter(CodigoEmail.usuario_id == usuario.id).one()
    assert registro.codigo_hash != codigo
    assert len(registro.codigo_hash) == 64


def test_login_bloqueado_ate_confirmar_email() -> None:
    """Senha certa + e-mail pendente = 403 com `email_nao_verificado` (sem token)."""
    db = _banco_temporario()
    _cadastrar(db, CaixaDeSaida(), "Joao Teste", "joao@teste.com")
    try:
        backend_main.login(
            backend_main.AuthLoginRequest(email="joao@teste.com", senha=SENHA), db
        )
    except HTTPException as erro:
        assert erro.status_code == 403, erro.status_code
        assert isinstance(erro.detail, dict), erro.detail
        assert erro.detail.get("email_nao_verificado") is True
        assert erro.detail.get("email") == "joao@teste.com"
    else:
        raise AssertionError("Login de conta não confirmada deveria responder 403.")


def test_codigo_errado_conta_tentativa_e_estoura_limite() -> None:
    """Código errado responde 400; depois do limite o mesmo código responde 429."""
    db = _banco_temporario()
    caixa = CaixaDeSaida()
    _cadastrar(db, caixa, "Ana Teste", "ana@teste.com")
    correto = caixa.ultimo_codigo()
    errado = "000000" if correto != "000000" else "111111"

    for tentativa in range(1, auth.TENTATIVAS_CODIGO_EMAIL + 1):
        try:
            backend_main.validar_email_do_cadastro(
                backend_main.AuthValidarEmailRequest(
                    email="ana@teste.com", codigo=errado
                ),
                db,
            )
        except HTTPException as erro:
            esperado = 429 if tentativa >= auth.TENTATIVAS_CODIGO_EMAIL else 400
            assert erro.status_code == esperado, (tentativa, erro.status_code)
        else:
            raise AssertionError("Código errado deveria ser recusado.")
    try:
        backend_main.validar_email_do_cadastro(
            backend_main.AuthValidarEmailRequest(email="ana@teste.com", codigo=correto),
            db,
        )
    except HTTPException as erro:
        assert erro.status_code == 429, erro.status_code
    else:
        raise AssertionError("Código queimado não pode confirmar o e-mail.")


def test_codigo_expirado_e_sem_codigo_pendente() -> None:
    """Código vencido responde 400 e o mesmo código não serve depois."""
    db = _banco_temporario()
    caixa = CaixaDeSaida()
    _cadastrar(db, caixa, "Bia Teste", "bia@teste.com")
    codigo = caixa.ultimo_codigo()
    usuario = db.query(Usuario).filter(Usuario.email == "bia@teste.com").one()
    registro = (
        db.query(CodigoEmail)
        .filter(CodigoEmail.usuario_id == usuario.id, CodigoEmail.usado_em.is_(None))
        .one()
    )
    registro.expira_em = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    db.commit()

    try:
        backend_main.validar_email_do_cadastro(
            backend_main.AuthValidarEmailRequest(email="bia@teste.com", codigo=codigo),
            db,
        )
    except HTTPException as erro:
        assert erro.status_code == 400, erro.status_code
        assert "expirou" in str(erro.detail).lower(), erro.detail
    else:
        raise AssertionError("Código expirado deveria ser recusado.")

    try:
        backend_main.validar_email_do_cadastro(
            backend_main.AuthValidarEmailRequest(email="bia@teste.com", codigo=codigo),
            db,
        )
    except HTTPException as erro:
        assert erro.status_code == 400, erro.status_code
        assert "pendente" in str(erro.detail).lower(), erro.detail
    else:
        raise AssertionError("Sem código pendente a rota deveria pedir um novo.")


def test_reenvio_respeita_um_por_minuto() -> None:
    """Reenviar logo depois do cadastro responde 429; passado o minuto, envia outro."""
    db = _banco_temporario()
    caixa = CaixaDeSaida()
    _cadastrar(db, caixa, "Caio Teste", "caio@teste.com")
    usuario = db.query(Usuario).filter(Usuario.email == "caio@teste.com").one()

    try:
        with patch.object(backend_main, "send_email", caixa):
            backend_main.reenviar_codigo_de_confirmacao(
                backend_main.AuthReenviarCodigoRequest(email="caio@teste.com"), db
            )
    except HTTPException as erro:
        assert erro.status_code == 429, erro.status_code
    else:
        raise AssertionError("Reenvio imediato deveria ser bloqueado (429).")

    # Cinco minutos de espera: o registro antigo sai da janela de 60s.
    antigo = (
        db.query(CodigoEmail)
        .filter(CodigoEmail.usuario_id == usuario.id)
        .order_by(CodigoEmail.id.desc())
        .first()
    )
    antigo.criado_em = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    db.commit()

    with patch.object(backend_main, "send_email", caixa):
        resposta = backend_main.reenviar_codigo_de_confirmacao(
            backend_main.AuthReenviarCodigoRequest(email="caio@teste.com"), db
        )
    assert resposta["email_enviado"] is True, resposta
    assert len(caixa.mensagens) == 2, caixa.mensagens
    assert db.query(CodigoEmail).filter(CodigoEmail.usuario_id == usuario.id).count() == 2


def test_confirmar_email_abre_sessao_e_cria_alerta_padrao() -> None:
    """Código certo devolve token, marca o e-mail e cria a configuração de alerta."""
    db = _banco_temporario()
    caixa = CaixaDeSaida()
    _cadastrar(db, caixa, "Duda Teste", "duda@teste.com")
    codigo = caixa.ultimo_codigo()

    resposta = backend_main.validar_email_do_cadastro(
        backend_main.AuthValidarEmailRequest(email="duda@teste.com", codigo=codigo), db
    )
    assert resposta.get("token"), resposta
    assert resposta["usuario"]["email"] == "duda@teste.com"
    assert resposta["usuario"]["email_verificado"] is True

    usuario = db.query(Usuario).filter(Usuario.email == "duda@teste.com").one()
    assert bool(usuario.email_verificado) is True
    assert usuario.email_verificado_em
    config = (
        db.query(ConfiguracaoAlerta)
        .filter(ConfiguracaoAlerta.usuario_id == usuario.id)
        .one()
    )
    assert bool(config.ativo) is True
    assert config.canal == "email"
    assert config.email == "duda@teste.com"

    # Confirmar de novo é idempotente (não gera erro nem código novo).
    repetido = backend_main.validar_email_do_cadastro(
        backend_main.AuthValidarEmailRequest(email="duda@teste.com", codigo="000000"), db
    )
    assert repetido.get("token"), repetido

    # E agora o login normal funciona.
    sessao = backend_main.login(
        backend_main.AuthLoginRequest(email="duda@teste.com", senha=SENHA), db
    )
    assert sessao.get("token"), sessao


def test_falha_smtp_nao_apaga_a_conta() -> None:
    """SMTP fora do ar: conta criada, resposta avisa e o cliente pode reenviar."""
    db = _banco_temporario()
    resposta = _cadastrar(db, SMTPComFalha(), "Eva Teste", "eva@teste.com")
    assert resposta["email_enviado"] is False, resposta
    assert resposta["aviso"], resposta
    usuario = db.query(Usuario).filter(Usuario.email == "eva@teste.com").first()
    assert usuario is not None
    assert bool(usuario.email_verificado) is False
    # O código foi gerado e gravado mesmo sem o e-mail sair.
    assert db.query(CodigoEmail).filter(CodigoEmail.usuario_id == usuario.id).count() == 1


def test_hash_do_codigo_nao_revela_o_codigo() -> None:
    """O hash usa o segredo da aplicação e a comparação é em tempo constante."""
    codigo = auth.gerar_codigo_email()
    assert len(codigo) == 6 and codigo.isdigit(), codigo
    hash_1 = auth.hash_codigo_email(codigo)
    assert codigo not in hash_1
    assert len(hash_1) == 64
    assert auth.codigo_email_valido(codigo, hash_1) is True
    outro = "000000" if codigo != "000000" else "111111"
    assert auth.codigo_email_valido(outro, hash_1) is False


def main() -> int:
    testes = (
        test_registrar_sem_sessao_e_envia_codigo,
        test_login_bloqueado_ate_confirmar_email,
        test_codigo_errado_conta_tentativa_e_estoura_limite,
        test_codigo_expirado_e_sem_codigo_pendente,
        test_reenvio_respeita_um_por_minuto,
        test_confirmar_email_abre_sessao_e_cria_alerta_padrao,
        test_falha_smtp_nao_apaga_a_conta,
        test_hash_do_codigo_nao_revela_o_codigo,
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

