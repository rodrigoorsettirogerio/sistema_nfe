import hashlib
import logging
import os
import re
import smtplib
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from io import BytesIO
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from contextlib import asynccontextmanager

import pandas as pd
import requests
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.responses import JSONResponse, StreamingResponse
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import func, or_
from sqlalchemy.orm import Session
from typing import List, Optional
from pydantic import BaseModel, Field
from backend.database import PROJECT_ROOT, SessionLocal, get_db, init_db
from backend import acbr_client, auth, certificados
from backend.email_service import (
    detalhe_erro_smtp,
    recarregar_configuracao_smtp,
    send_email,
)
from backend.models import (
    AlertaNota,
    CodigoEmail,
    ConfiguracaoAlerta,
    ConfiguracaoSistema,
    Empresa,
    Fornecedor,
    ItemAuditado,
    ItemNota,
    MarcaIA,
    MapeamentoFornecedorProduto,
    NotaFiscal,
    RegraAgrupamento,
    SugestaoCorrecaoIA,
    Usuario,
)
# Memória de aprendizado fornecedor+cProd e identidade da embalagem (CX24, UN...).
from backend import aprendizado_produtos as aprendizado
# Consenso com IA (DeepSeek): segunda opinião da Auditoria (marcas e ambiguidades).
from backend import ia_consenso
# Autenticação (passlib + JWT) e dependência do usuário logado.
from backend import auth

# Regras de similaridade da auditoria (espelho do front) usadas para excluir dos
# relatórios os produtos que ainda estão como "Pendente de Aprovação".
from backend import auditoria_relatorio as regras_auditoria
# Extração da marca (NLP leve) e similaridade entre descrições: usadas na consulta
# paralela que alimenta SÓ o tooltip de marcas substitutas dos relatórios.
from backend import marcas as regras_marcas
# Motor de alertas (WhatsApp/e-mail): monta a mensagem a partir dos mesmos itens dos relatórios.
from backend import alertas as motor_alertas
# Consulta de CNPJ/CEP com troca de provedor (BrasilAPI -> minhareceita -> cnpj.ws).
from backend import consulta_cadastral
# Relatório de compras por e-mail (variações x última compra, impacto em R$, inflação).
from backend import relatorios_alerta
# Índice de inflação das compras (cesta anterior como peso, série histórica).
from backend import inflacao
# Excel da memória de cálculo: abre a conta do mês fechado produto a produto.
from backend import memoria_calculo
# Agendador (thread daemon) do envio diário/semanal/mensal: só decide quem/quando,
# o envio em si é o callback `_executar_agendamento` definido neste módulo.
from backend import agendador


@asynccontextmanager
async def _ciclo_de_vida(_app):
    """Liga o agendador com a API e o desliga no shutdown (sem cron externo)."""
    agendador.iniciar(_executar_agendamento)
    try:
        yield
    finally:
        agendador.parar()


app = FastAPI(title="API", version="1.0.0", lifespan=_ciclo_de_vida)
logger = logging.getLogger("sistema_nfe")
# O uvicorn configura apenas os loggers `uvicorn.*`; sem um handler próprio, os
# `logger.info` do cadastro de loja ficariam invisíveis (o root não tem handler).
# O handler dedicado mantém o terminal da API mostrando cada etapa do cadastro.
if not logger.handlers:
    _handler_api = logging.StreamHandler()
    _handler_api.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    )
    logger.addHandler(_handler_api)
if logger.level == logging.NOTSET:
    logger.setLevel(logging.INFO)
logger.propagate = False

# Garante as tabelas e as colunas novas (migrações aditivas: itens_nota.cfop,
# itens_auditados.motivo, empresas.usuario_id, tabela usuarios) antes da primeira
# requisição. É idempotente.
init_db()

@app.get("/")
def home():
    return {"status": "API rodando com sucesso!"}


# ──────────────────────────────────────────────────────────────────────────────
# Autenticação e multitenancy (login por e-mail/senha + empresas por usuário)
# ──────────────────────────────────────────────────────────────────────────────
class AuthCadastroRequest(BaseModel):
    """Corpo de /api/auth/registrar."""
    nome: str
    email: str
    senha: str


class AuthValidarEmailRequest(BaseModel):
    """Corpo de /api/auth/validar-email: e-mail da conta + código de 6 dígitos."""

    email: str
    codigo: str = ""


class AuthReenviarCodigoRequest(BaseModel):
    """Corpo de /api/auth/reenviar-codigo: só o e-mail da conta pendente."""

    email: str


class AuthLoginRequest(BaseModel):
    """Corpo de /api/auth/login."""
    email: str
    senha: str
    # Código de 6 dígitos do autenticador: obrigatório para administrador com 2FA ativo
    # (sem ele o login é recusado — nada de sessão parcial). Conta comum não usa.
    codigo_2fa: Optional[str] = None


class Auth2FASetupRequest(BaseModel):
    """Corpo de /api/auth/2fa/setup.

    `reiniciar=True` gera um segredo novo (troca de celular): o 2FA volta a ficar
    inativo até o administrador confirmar um código do novo aparelho.
    """
    reiniciar: bool = False


class Auth2FAVerifyRequest(BaseModel):
    """Corpo de /api/auth/2fa/verify: código de 6 dígitos do aplicativo autenticador."""
    codigo: str = ""


def _id_do_usuario_logado(usuario: Usuario) -> int:
    """ID da conta autenticada usado como dono de TODA loja criada/vinculada.

    Multitenancy: o dono é sempre o usuário do token (``usuario.id``), nunca um
    valor fixo como ``1`` (admin) — isso fazia lojas novas aparecerem só para o
    administrador. O guard é fail-closed: sem identificador a requisição é
    recusada em vez de gravar ``NULL`` ou um dono errado.
    """
    usuario_id = getattr(usuario, "id", None)
    if usuario_id is None:
        raise HTTPException(
            status_code=401,
            detail="Sessão inválida: usuário autenticado sem identificador. Entre novamente.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return int(usuario_id)


def _pode_gerenciar_loja(usuario: Usuario, empresa: Empresa) -> bool:
    """True quando a conta logada pode ver/alterar o certificado DESTA loja.

    Regra de negócio: a conta comum gerencia só as próprias lojas; o administrador
    (rodrigo) gerencia TODAS — precisa dar suporte (ver, reenviar e excluir
    certificado) sem depender do login do cliente. A checagem fica no backend: o
    front só esconde botões, nunca é a autoridade.
    """
    if bool(getattr(usuario, "is_admin", False)):
        return True
    return getattr(empresa, "usuario_id", None) == getattr(usuario, "id", None)


def _empresa_json(empresa: Empresa, total_notas: Optional[int] = None) -> dict:
    """Empresa no formato usado pelo front (seletores e tela 'Minhas Lojas')."""
    dados = {
        "id": empresa.id,
        "cnpj": empresa.cnpj,
        "nome_loja": empresa.nome_loja,
        "razao_social": empresa.razao_social,
        "municipio": empresa.municipio,
        "usuario_id": empresa.usuario_id,
        # None quando a loja foi vinculada antes da barreira do certificado existir.
        "certificado": certificados.certificado_json(empresa),
        # "0"/vazio = loja ainda sem download da Distribuição NF-e.
        "ultimo_nsu_acbr": str(empresa.ultimo_nsu_acbr or "0"),
    }
    if total_notas is not None:
        dados["total_notas"] = int(total_notas)
    return dados


def _aplicar_certificado(empresa: Empresa, dados: dict) -> None:
    """Grava no cadastro da loja os dados PÚBLICOS do certificado já validado."""
    empresa.certificado_titular = dados.get("titular")
    empresa.certificado_validade = dados.get("validade")
    empresa.certificado_enviado_em = dados.get("enviado_em")


# Campos de endereço exigidos pelo contrato de `POST /empresas` da ACBr.
CAMPOS_ENDERECO_EMPRESA = (
    "logradouro",
    "numero",
    "complemento",
    "bairro",
    "codigo_municipio",
    "uf",
    "cep",
)


def _endereco_empresa(empresa: Empresa) -> dict:
    """Endereço fiscal já gravado na loja (fallback quando a BrasilAPI falha)."""
    return {
        campo: str(getattr(empresa, campo, "") or "").strip()
        for campo in CAMPOS_ENDERECO_EMPRESA
    }


def _aplicar_endereco_empresa(empresa: Empresa, dados: dict) -> bool:
    """Persiste os campos de endereço conhecidos, sem apagar os já gravados.

    A origem pode ser a BrasilAPI (`info`) ou o próprio JSON montado para a ACBr
    (`dados_acbr["endereco"]`). Campos vazios não sobrescrevem o banco: assim uma
    resposta incompleta/429 não destrói o endereço salvo no vínculo da loja.
    """
    dados = dados or {}
    mudou = False
    for campo in CAMPOS_ENDERECO_EMPRESA:
        valor = str(dados.get(campo) or "").strip()
        if not valor:
            continue
        if campo in {"codigo_municipio", "cep"}:
            valor = re.sub(r"\D", "", valor)
        elif campo == "uf":
            valor = valor.upper()
        if getattr(empresa, campo, None) != valor:
            setattr(empresa, campo, valor)
            mudou = True

    # O contrato da ACBr chama o município de `cidade`; no banco ele é `municipio`.
    cidade = str(dados.get("cidade") or dados.get("municipio") or "").strip()
    if cidade and not str(empresa.municipio or "").strip():
        empresa.municipio = cidade
        mudou = True
    return mudou


def _salvar_empresa_no_banco(db: Session, empresa: Empresa, acao: str) -> None:
    """Commit + refresh com log explícito e rollback em caso de falha.

    ``acao`` é o particípio usado na mensagem ("cadastrada"/"atualizada").
    Sem o rollback, uma falha de SQLite deixaria a sessão da requisição suja;
    sem o log, o front recebia um erro genérico e não havia rastro no terminal.
    """
    try:
        db.commit()
        db.refresh(empresa)
    except Exception as erro:
        db.rollback()
        logger.exception(
            "Erro ao salvar empresa %s no banco (ação=%s)",
            getattr(empresa, "cnpj", "?"),
            acao,
        )
        raise HTTPException(
            status_code=500,
            detail=(
                "Erro ao salvar a empresa no banco de dados. "
                "Nenhuma alteração foi confirmada; tente novamente."
            ),
        ) from erro

    logger.info(
        "Empresa %s %s no banco: id=%s usuario_id=%s nome_loja=%r",
        empresa.cnpj,
        acao,
        empresa.id,
        empresa.usuario_id,
        empresa.nome_loja,
    )


def _total_notas(db: Session, empresa_id: int) -> int:
    """Quantas notas fiscais estão ligadas à empresa."""
    return int(
        db.query(func.count(NotaFiscal.id))
        .filter(NotaFiscal.empresa_id == empresa_id)
        .scalar()
        or 0
    )


def _consultar_cnpj(cnpj: str) -> dict:
    """Dados cadastrais do CNPJ na BrasilAPI.

    `fonte` diz de onde veio o dado: "brasilapi" (nome fantasia/razão social reais) ou
    "indisponivel" (offline, CNPJ recém-aberto, limite de requisições da API). O cadastro
    usa essa marca para NUNCA gravar o placeholder "Loja Exemplo" por cima do nome real
    da loja — ver `_atualizar_dados_cadastrais`.
    """
    dados = _consultar_cnpj_brasilapi(cnpj)
    if dados:
        return dados
    return {
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


# Cache em memória (por processo) da consulta à Receita: evita repetir a chamada quando a
# mesma loja é recadastrada. Só a resposta BOA fica no cache — falha tenta de novo.
_DADOS_CNPJ_CACHE = {}


# Quantas consultas são feitas no total antes de desistir: uma por provedor
# (BrasilAPI -> minhareceita -> publica.cnpj.ws). O limite de requisições da
# BrasilAPI (HTTP 429) é passageiro, mas insistir no MESMO serviço deixava o
# cadastro sem endereço (a ACBr recusava com "Complete o cadastro da empresa").
# Agora um 429/5xx passa para o próximo provedor (backend/consulta_cadastral.py).
TENTATIVAS_BRASILAPI = len(consulta_cadastral.PROVEDORES_CNPJ)
# Espera inicial entre as tentativas (dobra a cada rodada). `BRASILAPI_ESPERA=0`
# deixa os testes instantâneos. Também é usada por `_espera_brasilapi`.
ESPERA_BASE_BRASILAPI = float(os.getenv("BRASILAPI_ESPERA", "1") or 0)
# Status que vale a pena repetir/trocar de provedor: limite de requisições e
# instabilidade do serviço. Qualquer outro (404 de CNPJ inexistente) encerra na hora.
STATUS_BRASILAPI_PASSIVEIS_DE_REPETICAO = set(
    consulta_cadastral.STATUS_QUE_TROCAM_DE_PROVEDOR
)


def _espera_brasilapi(resposta, tentativa: int) -> float:
    """Segundos de espera antes da próxima tentativa (respeita `Retry-After`)."""
    espera = ESPERA_BASE_BRASILAPI * (2 ** (tentativa - 1))
    try:
        cabecalho = str((getattr(resposta, "headers", None) or {}).get("Retry-After") or "")
    except Exception:
        cabecalho = ""
    if cabecalho.strip().isdigit():
        espera = max(espera, float(cabecalho.strip()))
    return max(0.0, min(espera, 10.0))


def _consultar_cnpj_brasilapi(cnpj: str):
    """Dados do CNPJ na Receita (dict) ou None quando nenhum provedor respondeu.

    A consulta em si fica em ``backend/consulta_cadastral.py``: BrasilAPI,
    minhareceita.org e publica.cnpj.ws, trocando de provedor em 429/5xx/timeout em
    vez de insistir no mesmo. O cache de memória é mantido AQUI porque o cadastro
    de loja e a tela "Minhas Lojas" compartilham a mesma resposta dentro do
    processo; falha não é cacheada (o próximo clique tenta de novo).

    O retorno mantém o contrato antigo: ``fonte="brasilapi"`` quando o dado veio
    da Receita (de qualquer provedor) e ``indisponivel``/None quando não veio.
    """
    if cnpj in _DADOS_CNPJ_CACHE:
        return _DADOS_CNPJ_CACHE[cnpj]
    dados = consulta_cadastral.consultar_cnpj(cnpj)
    if not dados or not dados.get("nome_loja"):
        logger.warning(
            "Consulta CNPJ %s: nenhum provedor respondeu; o cadastro segue sem os "
            "dados da Receita (endereço pode ser informado na tela Minhas Lojas).",
            cnpj,
        )
        return None
    _DADOS_CNPJ_CACHE[cnpj] = dados
    return dados


def _aviso_receita(info: dict) -> Optional[str]:
    """Aviso para o front quando a Receita não respondeu (429/offline).

    O cadastro continua válido — só o endereço pode faltar. O aviso aparece na
    tela "Minhas Lojas" para o cliente preencher o endereço fiscal e reenviar
    antes de a ACBr recusar por dados incompletos.
    """
    if str((info or {}).get("fonte") or "") == "brasilapi":
        return None
    return (
        "A consulta à Receita Federal (BrasilAPI) não respondeu agora, então a loja "
        "ficou sem os dados de endereço. Se a ACBr recusar o cadastro por endereço "
        "incompleto, preencha o endereço fiscal em 'Minhas Lojas' e salve de novo."
    )


def _atualizar_dados_cadastrais(empresa: Empresa, info: dict) -> bool:
    """Renomeia a loja com o **Nome Fantasia** da BrasilAPI (regra do cadastro).

    O cadastro de CNPJ SEMPRE atualiza `nome_loja` com o nome fantasia da Receita (e a
    razão social/município quando vêm preenchidos) — é isso que mantém "Moça Bar" no lugar
    de um nome antigo depois de uma alteração na Receita. Só a resposta da BrasilAPI pode
    sobrescrever: quando a consulta falha (`fonte="indisponivel"`) nada é gravado, para um
    erro de rede não trocar o nome da loja por "Loja Exemplo".

    Devolve True quando algum campo mudou.
    """
    if str((info or {}).get("fonte") or "") != "brasilapi":
        return False
    mudou = False
    for campo in ("nome_loja", "razao_social", "municipio"):
        valor = str(info.get(campo) or "").strip()
        if valor and getattr(empresa, campo) != valor:
            setattr(empresa, campo, valor)
            mudou = True
    return mudou



def _sessao_json(usuario: Usuario, mensagem: Optional[str] = None) -> dict:
    """Resposta de login/cadastro: dados do usuário + token JWT + validade."""
    token, expira = auth.criar_token(usuario)
    corpo = {
        "sucesso": True,
        "usuario": auth.descricao_usuario(usuario),
        "token": token,
        "tipo_token": "bearer",
        "expira_em": expira.isoformat(),
    }
    if mensagem:
        corpo["mensagem"] = mensagem
    return corpo


ASSUNTO_CODIGO_EMAIL = "Seu código de confirmação — Inteligência de Custos NF-e"
# Um código novo por minuto e no máximo 10 por hora (proteção contra abuso do SMTP).
REENVIO_CODIGO_SEGUNDOS = 60
REENVIO_CODIGO_LIMITE_HORA = 10


def _texto_codigo_email(usuario: Usuario, codigo: str, minutos: int) -> str:
    """Corpo em texto simples do e-mail de confirmação (sem HTML: nada para abrir)."""
    primeiro_nome = (usuario.nome or "").split()[0] if usuario.nome else "cliente"
    return (
        f"Olá, {primeiro_nome}!\n\n"
        "Seu código de confirmação da conta Inteligência de Custos NF-e é:\n\n"
        f"    {codigo}\n\n"
        f"Ele vale por {minutos} minutos e só pode ser usado uma vez.\n"
        "Se você não criou esta conta, ignore esta mensagem.\n\n"
        "Inteligência de Custos NF-e\n"
    )


def _enviar_codigo_confirmacao(db: Session, usuario: Usuario) -> dict:
    """Gera um código novo (invalidando os anteriores) e envia por e-mail.

    Devolve ``{"email_enviado", "expira_em", "aviso"}``. Falha de SMTP NÃO apaga a
    conta: a resposta avisa o cliente e o botão "Reenviar código" tenta de novo. O
    código nunca entra em log nem na resposta HTTP.
    """
    agora = datetime.now(timezone.utc)
    minutos = auth.VALIDADE_CODIGO_EMAIL_MINUTOS
    expira = agora + timedelta(minutes=minutos)
    # Um código pendente por conta: os anteriores são queimados antes do novo.
    db.query(CodigoEmail).filter(
        CodigoEmail.usuario_id == usuario.id, CodigoEmail.usado_em.is_(None)
    ).update({"usado_em": agora.isoformat()}, synchronize_session=False)
    codigo = auth.gerar_codigo_email()
    db.add(
        CodigoEmail(
            usuario_id=usuario.id,
            email=usuario.email,
            finalidade="validacao_email",
            codigo_hash=auth.hash_codigo_email(codigo),
            criado_em=agora.isoformat(),
            expira_em=expira.isoformat(),
            tentativas=0,
        )
    )
    db.commit()

    try:
        send_email(
            usuario.email,
            ASSUNTO_CODIGO_EMAIL,
            _texto_codigo_email(usuario, codigo, minutos),
        )
    except Exception as erro:  # noqa: BLE001 - SMTP ausente/inválido/offline
        detalhe = detalhe_erro_smtp(erro)
        logger.warning(
            "Falha ao enviar o código de confirmação para %s (usuário %s): %s",
            usuario.email,
            usuario.id,
            detalhe,
        )
        return {
            "email_enviado": False,
            "expira_em": expira.isoformat(),
            "aviso": (
                "Não foi possível enviar o e-mail com o código agora "
                f"(Erro SMTP: {detalhe}). Use \"Reenviar código\" em instantes."
            ),
        }
    logger.info(
        "Código de confirmação de e-mail enviado para %s (usuário %s).",
        usuario.email,
        usuario.id,
    )
    return {"email_enviado": True, "expira_em": expira.isoformat(), "aviso": None}


def _garantir_configuracao_alerta_padrao(db: Session, usuario: Usuario):
    """Configuração de alerta padrão da conta (criada na confirmação do e-mail).

    Padrão decidido com o cliente: alerta LIGADO, canal e-mail e destinatário o próprio
    e-mail da conta — o cliente só troca/adiciona um e-mail secundário se quiser. Os
    três relatórios (diário, semanal e mensal) já nascem ligados às 06:00.
    """
    config = _configuracao_alerta(db, usuario)
    if config is not None:
        return config
    config = ConfiguracaoAlerta(
        usuario_id=usuario.id,
        ativo=True,
        canal="email",
        email=usuario.email,
        variacao_minima=motor_alertas.VARIACAO_MINIMA_PADRAO,
        janela_dias=motor_alertas.JANELA_PADRAO_DIAS,
        incluir_aumentos=True,
        incluir_divergencias=True,
        alerta_diario=True,
        alerta_semanal=True,
        alerta_mensal=True,
        ultima_nota_alerta_id=0,
        ultima_nota_semanal_id=0,
        ultima_nota_mensal_id=0,
        atualizado_em=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    db.add(config)
    db.commit()
    return config


@app.post("/api/auth/registrar")
def registrar_usuario(dados: AuthCadastroRequest, db: Session = Depends(get_db)):
    """Cria a conta do cliente e envia o código de confirmação do e-mail.

    A senha é gravada apenas como hash (passlib/bcrypt). **Nenhuma sessão nasce aqui**:
    a conta entra como ``email_verificado = False`` e o token só é emitido depois de
    POST /api/auth/validar-email com o código de 6 dígitos enviado por SMTP. Isso
    garante que o e-mail é real e que o cliente já recebe os alertas no endereço certo.
    """
    nome, email = auth.validar_cadastro(dados.nome, dados.email, dados.senha)
    if db.query(Usuario).filter(func.lower(Usuario.email) == email).first():
        raise HTTPException(
            status_code=409, detail="Este e-mail já está cadastrado. Use a aba Entrar."
        )
    usuario = Usuario(
        nome=nome,
        email=email,
        senha_hash=auth.gerar_hash_senha(dados.senha),
        email_verificado=False,
    )
    db.add(usuario)
    db.commit()
    db.refresh(usuario)
    envio = _enviar_codigo_confirmacao(db, usuario)
    return {
        "sucesso": True,
        "verificacao_pendente": True,
        "email": usuario.email,
        "nome": usuario.nome,
        "mensagem": (
            f"Conta criada! Enviamos um código de 6 dígitos para {usuario.email}."
        ),
        **envio,
    }


@app.post("/api/auth/validar-email")
def validar_email_do_cadastro(
    dados: AuthValidarEmailRequest, db: Session = Depends(get_db)
):
    """Confirma o e-mail com o código de 6 dígitos e ABRE a sessão.

    Regras (todas conferidas aqui, nunca só na tela):

    - conta inexistente → 404; conta já confirmada → devolve a sessão direto
      (idempotente: clicar duas vezes não gera erro);
    - sem código pendente/expirado → 400 pedindo um novo código;
    - código errado → 400 e conta a tentativa; depois de
      ``auth.TENTATIVAS_CODIGO_EMAIL`` erros o código é queimado (429);
    - código correto → marca ``email_verificado``, cria a configuração de alerta padrão
      (ativo, e-mail = e-mail da conta) e devolve o token JWT.
    """
    email = auth.normalizar_email(dados.email)
    usuario = db.query(Usuario).filter(func.lower(Usuario.email) == email).first()
    if not usuario:
        raise HTTPException(
            status_code=404, detail="Nenhuma conta encontrada para este e-mail."
        )
    if bool(getattr(usuario, "email_verificado", False)):
        return _sessao_json(usuario, f"Bem-vindo(a), {usuario.nome.split()[0]}!")

    codigo = re.sub(r"\D", "", str(dados.codigo or ""))
    if len(codigo) != 6:
        raise HTTPException(
            status_code=400, detail="Informe o código de 6 dígitos enviado por e-mail."
        )

    agora = datetime.now(timezone.utc)
    registro = (
        db.query(CodigoEmail)
        .filter(CodigoEmail.usuario_id == usuario.id, CodigoEmail.usado_em.is_(None))
        .order_by(CodigoEmail.id.desc())
        .first()
    )
    if registro is None:
        raise HTTPException(
            status_code=400,
            detail="Não há código pendente para esta conta. Peça um novo código.",
        )
    try:
        expira = datetime.fromisoformat(str(registro.expira_em))
    except ValueError:
        expira = agora
    if expira <= agora:
        registro.usado_em = agora.isoformat()
        db.commit()
        raise HTTPException(
            status_code=400, detail="O código expirou. Peça um novo código."
        )
    tentativas = int(registro.tentativas or 0)
    if tentativas >= auth.TENTATIVAS_CODIGO_EMAIL:
        raise HTTPException(
            status_code=429,
            detail="Muitas tentativas com este código. Peça um novo código.",
        )
    if not auth.codigo_email_valido(codigo, registro.codigo_hash):
        registro.tentativas = tentativas + 1
        esgotado = registro.tentativas >= auth.TENTATIVAS_CODIGO_EMAIL
        db.commit()
        raise HTTPException(
            status_code=429 if esgotado else 400,
            detail=(
                "Muitas tentativas com este código. Peça um novo código."
                if esgotado
                else "Código incorreto. Confira o e-mail e tente de novo."
            ),
        )

    registro.usado_em = agora.isoformat()
    usuario.email_verificado = True
    usuario.email_verificado_em = agora.isoformat(timespec="seconds")
    _garantir_configuracao_alerta_padrao(db, usuario)
    db.commit()
    db.refresh(usuario)
    logger.info("E-mail confirmado para %s (usuário %s).", usuario.email, usuario.id)
    return _sessao_json(
        usuario, f"E-mail confirmado! Bem-vindo(a), {usuario.nome.split()[0]}."
    )


@app.post("/api/auth/reenviar-codigo")
def reenviar_codigo_de_confirmacao(
    dados: AuthReenviarCodigoRequest, db: Session = Depends(get_db)
):
    """Envia um novo código de confirmação (botão "📨 Reenviar código").

    Limites: um código por minuto e 10 por hora por conta (429). E-mail inexistente
    devolve a MESMA mensagem genérica (não revela quais contas existem).
    """
    email = auth.normalizar_email(dados.email)
    resposta = {
        "sucesso": True,
        "mensagem": "Se este e-mail estiver cadastrado, enviamos um novo código.",
        "email_enviado": False,
        "aviso": None,
    }
    usuario = db.query(Usuario).filter(func.lower(Usuario.email) == email).first()
    if usuario is None:
        return resposta
    if bool(getattr(usuario, "email_verificado", False)):
        resposta["mensagem"] = "Este e-mail já está confirmado. Faça login."
        return resposta

    agora = datetime.now(timezone.utc)
    ultimo = (
        db.query(CodigoEmail)
        .filter(CodigoEmail.usuario_id == usuario.id)
        .order_by(CodigoEmail.id.desc())
        .first()
    )
    if ultimo is not None:
        try:
            criado = datetime.fromisoformat(str(ultimo.criado_em))
        except ValueError:
            criado = None
        if criado is not None:
            espera = REENVIO_CODIGO_SEGUNDOS - int((agora - criado).total_seconds())
            if espera > 0:
                raise HTTPException(
                    status_code=429,
                    detail=f"Aguarde {espera} segundo(s) para pedir outro código.",
                )
    desde = (agora - timedelta(hours=1)).isoformat()
    enviados = (
        db.query(func.count(CodigoEmail.id))
        .filter(CodigoEmail.usuario_id == usuario.id, CodigoEmail.criado_em >= desde)
        .scalar()
        or 0
    )
    if int(enviados) >= REENVIO_CODIGO_LIMITE_HORA:
        raise HTTPException(
            status_code=429,
            detail="Limite de códigos por hora atingido. Tente novamente mais tarde.",
        )

    resposta.update(_enviar_codigo_confirmacao(db, usuario))
    resposta["mensagem"] = (
        f"Novo código enviado para {usuario.email}."
        if resposta["email_enviado"]
        else "O código foi gerado, mas o e-mail não pôde ser enviado agora."
    )
    return resposta


@app.post("/api/auth/login")
def login(dados: AuthLoginRequest, db: Session = Depends(get_db)):
    """Autentica e-mail/senha e devolve o token JWT usado nas rotas protegidas.

    **Login com 2FA = senha + código de 6 dígitos (fail-closed).** Depois da senha, a
    conta que tem 2FA ativo só recebe a sessão se o código do autenticador conferir:

    - código ausente ou errado → 401 com `motivo` e um `desafio` curto (nada de sessão);
    - admin que ainda não configurou o 2FA → 403 com `configuracao_2fa_necessaria` e o
      desafio para mostrar o QR Code já no login (`/api/auth/2fa/setup`);
    - código correto → sessão + `token_2fa` (a Auditoria já abre sem pedir o código duas
      vezes, porque o segundo fator acabou de ser verificado).

    Conta comum (restaurante) sem 2FA entra apenas com e-mail e senha.
    """
    email = auth.normalizar_email(dados.email)
    usuario = db.query(Usuario).filter(func.lower(Usuario.email) == email).first()
    if not usuario or not auth.verificar_senha(dados.senha, usuario.senha_hash):
        # Mesma mensagem para e-mail inexistente e senha errada (não revela contas).
        raise HTTPException(status_code=401, detail="E-mail ou senha incorretos.")

    if not bool(getattr(usuario, "email_verificado", True)):
        # Conta criada, mas o e-mail não foi confirmado: NADA de sessão. O front abre a
        # tela do código (SESSAO_VERIFICACAO_EMAIL) e oferece "Reenviar código".
        raise HTTPException(
            status_code=403,
            detail={
                "mensagem": (
                    "Confirme o seu e-mail para entrar. Enviamos um código de 6 dígitos."
                ),
                "email_nao_verificado": True,
                "email": usuario.email,
            },
        )

    if auth.usuario_com_2fa(usuario):
        codigo = auth.normalizar_codigo_totp(dados.codigo_2fa)
        if not codigo:
            raise auth.desafio_login(usuario, "codigo_ausente")
        if not auth.codigo_totp_valido(usuario.totp_secret, codigo):
            raise auth.desafio_login(usuario, "codigo_invalido")
    elif bool(getattr(usuario, "is_admin", False)):
        # Administrador NUNCA entra sem 2FA: no primeiro acesso o QR Code aparece aqui,
        # antes de existir qualquer sessão.
        raise auth.desafio_login(usuario, "sem_cadastro")

    corpo = _sessao_json(usuario, f"Bem-vindo(a) de volta, {usuario.nome.split()[0]}!")
    if auth.usuario_com_2fa(usuario):
        token_2fa, expira_2fa = auth.criar_token_2fa(usuario)
        corpo.update(
            {
                "tipo_token_2fa": auth.TIPO_TOKEN_2FA,
                "token_2fa": token_2fa,
                "expira_em_2fa": expira_2fa.isoformat(),
            }
        )
    return corpo


@app.get("/api/auth/me")
def usuario_autenticado(
    usuario: Usuario = Depends(auth.usuario_atual), db: Session = Depends(get_db)
):
    """Confere se o token ainda vale (o front usa para detectar sessão expirada)."""
    total_empresas = (
        db.query(func.count(Empresa.id)).filter(Empresa.usuario_id == usuario.id).scalar() or 0
    )
    return {"usuario": auth.descricao_usuario(usuario), "total_empresas": int(total_empresas)}


# ──────────────────────────────────────────────────────────────────────────────
# 2FA (TOTP) — segundo fator da Auditoria
# ──────────────────────────────────────────────────────────────────────────────
@app.post("/api/auth/2fa/setup")
def configurar_2fa(
    dados: Auth2FASetupRequest,
    contexto: auth.Sessao2FA = Depends(auth.autenticacao_2fa),
    db: Session = Depends(get_db),
):
    """Gera (ou reaproveita) o segredo TOTP e devolve a URL do QR Code.

    Aceita o token de sessão (admin já logado abrindo a Auditoria) e também o **desafio
    do login** — é assim que o administrador configura o 2FA no primeiro acesso, antes de
    existir sessão (o login é fail-closed: sem 2FA não há token).

    - **Já ativo** e sem `reiniciar`: não devolve segredo nenhum (a conta continua com o
      autenticador que já está configurado) — o front mostra o campo do código;
    - **Segredo pendente** (QR já gerado e ainda não confirmado): devolve o MESMO segredo.
      Isso é essencial na tela: cada rerun do Streamlit reexecuta o script, e gerar um
      segredo novo a cada chamada invalidaria o QR que o usuário acabou de escanear;
    - **Primeiro acesso** (ou `reiniciar=True`): grava um segredo novo com
      `totp_ativo=False` e devolve `segredo`, `otpauth_url` e o PNG do QR Code em base64.

    O segredo só passa a valer para entrar depois de um código de 6 dígitos correto em
    /api/auth/2fa/verify.
    """
    usuario = contexto.usuario
    if usuario.totp_secret and not dados.reiniciar:
        ativo = bool(usuario.totp_ativo)
        url = auth.url_otpauth(usuario.totp_secret, usuario.email)
        return {
            "sucesso": True,
            "ativo": ativo,
            "ja_configurado": ativo,
            "segredo": None if ativo else usuario.totp_secret,
            "otpauth_url": None if ativo else url,
            "qr_code": None if ativo else auth.gerar_qr_code_data_uri(url),
            "conta": usuario.email,
            "emissor": auth.TOTP_EMISSOR,
            "mensagem": (
                "A autenticação em dois fatores já está ativa nesta conta."
                if ativo
                else "Escaneie o QR Code no aplicativo autenticador e confirme o código."
            ),
        }

    segredo = auth.gerar_segredo_totp()
    usuario.totp_secret = segredo
    usuario.totp_ativo = False  # só ativa na confirmação do primeiro código
    db.commit()

    url = auth.url_otpauth(segredo, usuario.email)
    return {
        "sucesso": True,
        "ativo": False,
        "ja_configurado": False,
        "segredo": segredo,
        "otpauth_url": url,
        "qr_code": auth.gerar_qr_code_data_uri(url),
        "conta": usuario.email,
        "emissor": auth.TOTP_EMISSOR,
        "mensagem": "Escaneie o QR Code no aplicativo autenticador e confirme o código.",
    }


@app.post("/api/auth/2fa/verify")
def verificar_2fa(
    dados: Auth2FAVerifyRequest,
    contexto: auth.Sessao2FA = Depends(auth.autenticacao_2fa),
    db: Session = Depends(get_db),
):
    """Valida o código de 6 dígitos do autenticador e libera a Auditoria.

    No primeiro código válido o 2FA é ATIVADO (`totp_ativo=True`). Em todos os casos
    devolve `token_2fa`: um JWT curto (2h) que o front manda em `X-2FA-Token` nas rotas
    de auditoria. Código errado/expirado → 401 e nada é ativado.

    Quando a chamada vem do **desafio do login** (admin que acabou de digitar e-mail e
    senha), a resposta traz também a sessão (`token`, `expira_em` e `usuario`): é o
    momento em que o login é concluído — e só depois de um código válido.
    """
    usuario = contexto.usuario
    if not getattr(usuario, "totp_secret", None):
        raise HTTPException(
            status_code=400,
            detail="Gere o QR Code do autenticador (2FA) antes de confirmar o código.",
        )
    codigo = auth.normalizar_codigo_totp(dados.codigo)
    if len(codigo) != auth.TOTP_DIGITOS:
        raise HTTPException(
            status_code=400,
            detail=f"O código tem {auth.TOTP_DIGITOS} dígitos. Confira e tente de novo.",
        )
    if not auth.codigo_totp_valido(usuario.totp_secret, codigo):
        raise HTTPException(
            status_code=401,
            detail=(
                "Código de verificação inválido ou expirado. Use o código atual do "
                "aplicativo autenticador."
            ),
        )

    ativado = False
    if not bool(getattr(usuario, "totp_ativo", False)):
        usuario.totp_ativo = True
        db.commit()
        db.refresh(usuario)
        ativado = True

    token_2fa, expira = auth.criar_token_2fa(usuario)
    corpo = {
        "sucesso": True,
        "ativado": ativado,
        "tipo_token": auth.TIPO_TOKEN_2FA,
        "token_2fa": token_2fa,
        "expira_em": expira.isoformat(),
        "usuario": auth.descricao_usuario(usuario),
        "mensagem": "2FA confirmado: a Auditoria está liberada." if ativado else "Código confirmado.",
    }
    if contexto.via_desafio:
        # 2ª etapa do LOGIN: a sessão nasce aqui, junto com o token do segundo fator.
        # `expira_em` continua sendo a validade do token 2FA (o front mostra/expira por
        # ela); a validade da sessão vai em `expira_em_sessao`.
        token_sessao, expira_sessao = auth.criar_token(usuario)
        corpo.update(
            {
                "token": token_sessao,
                "tipo_token": "bearer",
                "expira_em_sessao": expira_sessao.isoformat(),
            }
        )
        corpo["mensagem"] = (
            "2FA ativado e login concluído." if ativado else "Código confirmado: login concluído."
        )
    return corpo


@app.get("/api/empresas/vinculadas")
def listar_empresas_vinculadas(
    usuario: Usuario = Depends(auth.usuario_atual), db: Session = Depends(get_db)
):
    """Empresas (CNPJs) que pertencem ao usuário logado, com o total de notas de cada uma.

    Isolamento por conta: a lista nunca inclui lojas do admin nem lojas órfãs. A
    exceção é o PRÓPRIO administrador: ele precisa da visão de suporte de TODAS as
    lojas (com o e-mail do dono em `dono_email`) para ver/reenviar/excluir
    certificado sem entrar na conta do cliente.
    """
    dono_id = _id_do_usuario_logado(usuario)
    admin = bool(getattr(usuario, "is_admin", False))
    print(f"Buscando empresas para usuario_id={usuario.id} (admin={admin})", flush=True)
    consulta = db.query(Empresa)
    if not admin:
        consulta = consulta.filter(Empresa.usuario_id == dono_id)
    empresas = consulta.order_by(Empresa.nome_loja).all()
    print(f"Empresas encontradas: {len(empresas)}", flush=True)
    contagem = dict(
        db.query(NotaFiscal.empresa_id, func.count(NotaFiscal.id))
        .group_by(NotaFiscal.empresa_id)
        .all()
    )
    donos = {}
    if admin:
        ids_donos = {empresa.usuario_id for empresa in empresas if empresa.usuario_id}
        if ids_donos:
            donos = dict(
                db.query(Usuario.id, Usuario.email)
                .filter(Usuario.id.in_(ids_donos))
                .all()
            )
    resposta = []
    for empresa in empresas:
        dados = _empresa_json(empresa, contagem.get(empresa.id, 0))
        if admin:
            # Só o admin recebe o dono da loja: é o que separa "minha loja" de
            # "loja do cliente X" na tela de suporte.
            dados["dono_email"] = donos.get(empresa.usuario_id) or ""
        resposta.append(dados)
    return resposta

@app.get("/api/lojas")
def listar_lojas(
    usuario: Usuario = Depends(auth.usuario_atual), db: Session = Depends(get_db)
):
    """Lojas da CONTA LOGADA (usada pelo botão "🔌 Testar Conexão" da tela Minhas Lojas).

    Multitenancy: a rota exige token e devolve somente as empresas de
    ``empresas.usuario_id == usuario.id``. Antes ela não tinha autenticação e fazia
    ``db.query(Empresa).all()``: qualquer chamada listava as lojas de TODAS as contas, e o
    front imprimia o JSON cru na tela (vazamento entre clientes).

    A resposta tem só dados cadastrais públicos da própria conta — nenhum token, URL,
    endereço ou identificador de outra conta entra nela.
    """
    empresas = (
        db.query(Empresa)
        .filter(Empresa.usuario_id == _id_do_usuario_logado(usuario))
        .order_by(Empresa.nome_loja)
        .all()
    )
    return [
        {
            "id": empresa.id,
            "cnpj": empresa.cnpj,
            "nome_loja": empresa.nome_loja,
            "razao_social": empresa.razao_social,
        }
        for empresa in empresas
    ]


@app.get("/api/empresas/consultar-cnpj")
def consultar_cnpj_da_loja(
    cnpj: str = Query("", description="CNPJ da loja (com ou sem pontuação)"),
    usuario: Usuario = Depends(auth.usuario_atual),
    db: Session = Depends(get_db),
):
    """Autopreenchimento do formulário "➕ Vincular novo CNPJ" (tela Minhas Lojas).

    A tela consulta esta rota assim que o CNPJ fica completo (callback do campo, fora do
    `st.form`) e preenche razão social e endereço fiscal nos widgets do formulário — antes
    disso os campos nasciam vazios e o cliente tinha de digitar o endereço à mão ou o
    cadastro ia para a ACBr sem endereço (400 "Complete o cadastro da empresa").

    Devolve ``endereco`` já nos nomes do payload da ACBr (``logradouro``, ``numero``,
    ``complemento``, ``bairro``, ``codigo_municipio``, ``uf``, ``cep``), ``fonte``
    (``brasilapi`` ou ``indisponivel``) e ``aviso`` quando a Receita não respondeu.
    Loja que já é da conta completa o endereço com o que está gravado no banco; CNPJ de
    outra conta não é exposto (``de_outra_conta``) e a tela avisa antes do vínculo.
    """
    digitos = re.sub(r"\D", "", str(cnpj or ""))
    if len(digitos) != 14:
        raise HTTPException(status_code=400, detail="Informe um CNPJ com 14 dígitos.")

    dono_id = _id_do_usuario_logado(usuario)
    admin = bool(getattr(usuario, "is_admin", False))
    existente = db.query(Empresa).filter(Empresa.cnpj == digitos).first()
    loja_de_outra_conta = bool(
        existente and existente.usuario_id and existente.usuario_id != dono_id
    )
    if loja_de_outra_conta and not admin:
        return {
            "cnpj": digitos,
            "encontrado": False,
            "razao_social": "",
            "nome_loja": "",
            "municipio": "",
            "endereco": {},
            "fonte": "indisponivel",
            "aviso": "Este CNPJ já está vinculado a outra conta.",
            "da_conta": False,
            "de_outra_conta": True,
        }

    info = _consultar_cnpj(digitos)
    fonte = str(info.get("fonte") or "indisponivel")
    encontrado = fonte == "brasilapi"
    endereco = {
        campo: str(info.get(campo) or "").strip() for campo in CAMPOS_ENDERECO_EMPRESA
    }
    # O formulário manda o município com o nome do payload da ACBr ("cidade"), que não
    # está em CAMPOS_ENDERECO_EMPRESA — sem esta linha o campo "Município" ficaria vazio
    # e o endereço iria incompleto para a ACBr.
    endereco["cidade"] = str(info.get("municipio") or "").strip()
    if existente:
        # O endereço já gravado no vínculo completa o que a Receita não trouxe.
        for campo, valor in _endereco_empresa(existente).items():
            if valor and not endereco.get(campo):
                endereco[campo] = valor
        if not endereco["cidade"]:
            endereco["cidade"] = str(getattr(existente, "municipio", "") or "").strip()
    resposta = {
        "cnpj": digitos,
        "encontrado": encontrado,
        # `_consultar_cnpj` devolve placeholder ("Loja Exemplo") quando a Receita falha:
        # aqui ele NÃO pode ir para a tela, senão o cliente salvaria o nome errado.
        "razao_social": str(info.get("razao_social") or "") if encontrado else "",
        "nome_loja": str(info.get("nome_loja") or "") if encontrado else "",
        "municipio": str(info.get("municipio") or "") if encontrado else "",
        "endereco": endereco,
        "fonte": fonte,
        "aviso": _aviso_receita(info),
        "da_conta": bool(existente),
        "de_outra_conta": loja_de_outra_conta,
    }
    if loja_de_outra_conta and admin:
        # Visão de suporte: o admin pode reenviar o certificado de uma loja de
        # cliente. O front usa `dono_email` para avisar que a loja continua sendo
        # do cliente (o vínculo não muda de dono).
        dono = db.query(Usuario).filter(Usuario.id == existente.usuario_id).first()
        resposta["dono_email"] = str(getattr(dono, "email", "") or "")
    return resposta


@app.get("/api/empresas/consultar-cep")
def consultar_cep_da_loja(
    cep: str = Query("", description="CEP da loja (com ou sem pontuação)"),
    usuario: Usuario = Depends(auth.usuario_atual),
):
    """Autopreenchimento do endereço fiscal pelo CEP (tela "Minhas Lojas").

    A Receita não devolve o endereço de algumas lojas (429/CNPJ recém-aberto) e o
    cliente teria de digitar rua, bairro, município e UF à mão. O campo CEP da tela
    consulta esta rota (ViaCEP e, se ele falhar, a BrasilAPI CEP) e preenche esses
    campos — número e complemento continuam por conta do cliente.

    ``codigo_municipio`` (IBGE) vem do ViaCEP e NÃO aparece na tela: é guardado na
    sessão e vai no payload da ACBr, que exige os 7 dígitos do município.
    """
    digitos = re.sub(r"\D", "", str(cep or ""))
    if len(digitos) != 8:
        raise HTTPException(status_code=400, detail="Informe um CEP com 8 dígitos.")
    dados = consulta_cadastral.consultar_cep(digitos)
    if not dados:
        return {
            "cep": digitos,
            "encontrado": False,
            "endereco": {},
            "fonte": "indisponivel",
            "aviso": (
                "Não encontramos este CEP agora. Confira o número ou preencha o "
                "endereço à mão — ele é obrigatório para o cadastro na ACBr."
            ),
        }
    return {
        "cep": dados.get("cep") or digitos,
        "encontrado": True,
        "endereco": {
            "logradouro": dados.get("logradouro") or "",
            "bairro": dados.get("bairro") or "",
            "cidade": dados.get("cidade") or "",
            "uf": dados.get("uf") or "",
            "codigo_municipio": dados.get("codigo_municipio") or "",
        },
        "fonte": dados.get("fonte") or "",
        "aviso": None,
    }


@app.post("/api/empresas/cadastrar")
async def cadastrar_empresa(
    usuario: Usuario = Depends(auth.usuario_atual),
    db: Session = Depends(get_db),
    cnpj: str = Form("", description="CNPJ da loja (com ou sem pontuação)"),
    senha_certificado: str = Form("", description="Senha do certificado digital (.pfx/.p12)"),
    certificado: Optional[UploadFile] = File(
        None, description="Certificado digital A1 da empresa (.pfx ou .p12)"
    ),
):
    """Cadastra o CNPJ e VINCULA a loja à conta logada (tela "Minhas Lojas").

    É multipart/form-data porque a **barreira do certificado digital** exige o arquivo
    .pfx/.p12 (`certificado`) e a `senha_certificado`: sem os dois o vínculo é bloqueado
    com 400 — nada é gravado. Só os dados públicos do certificado (titular/validade)
    ficam no banco; arquivo e senha não são persistidos (backend/certificados.py).

    Depois da barreira:
    - CNPJ novo: consulta a Receita (BrasilAPI) e cria a loja já com o dono;
    - CNPJ que já existe sem dono (importado antes do login existir): a loja é adotada
      pela conta;
    - CNPJ de outra conta: 409 — cada CNPJ pertence a um único usuário.

    Em TODOS os casos o `nome_loja` é atualizado com o **Nome Fantasia da BrasilAPI**
    (`_atualizar_dados_cadastrais`): é a garantia de que uma loja renomeada na Receita
    (ex.: "Moça Bar") não continua com o nome antigo no banco. Quando a consulta falha
    (offline/limite da API) o nome atual é preservado — a resposta traz
    `nome_atualizado` para o front avisar.
    """
    cnpj_limpo = re.sub(r"\D", "", str(cnpj or ""))
    if len(cnpj_limpo) != 14:
        raise HTTPException(status_code=400, detail="Informe um CNPJ com 14 dígitos.")
    # Dono único e explícito: a conta do token, nunca o admin/fixo 1.
    dono_id = _id_do_usuario_logado(usuario)
    print(
        f"DEBUG: Tentando cadastrar CNPJ {cnpj_limpo} via "
        f"/api/empresas/cadastrar (usuario_id={dono_id})",
        flush=True,
    )
    logger.info(
        "Cadastro de loja %s solicitado pelo usuário %s (%s)",
        cnpj_limpo,
        dono_id,
        usuario.email,
    )

    conteudo_certificado = await certificado.read() if certificado is not None else None
    try:
        dados_certificado = certificados.validar_certificado(
            certificado.filename if certificado is not None else None,
            conteudo_certificado,
            senha_certificado,
            cnpj=cnpj_limpo,
        )
    except HTTPException as erro:
        logger.warning(
            "Cadastro da loja %s recusado pelo certificado: %s",
            cnpj_limpo,
            erro.detail,
        )
        raise

    existente = db.query(Empresa).filter(Empresa.cnpj == cnpj_limpo).first()
    if existente:
        loja_de_outra_conta = bool(
            existente.usuario_id and existente.usuario_id != dono_id
        )
        if loja_de_outra_conta and not bool(getattr(usuario, "is_admin", False)):
            logger.warning(
                "Cadastro da loja %s recusado: CNPJ já vinculado ao usuário %s.",
                cnpj_limpo,
                existente.usuario_id,
            )
            raise HTTPException(
                status_code=409, detail="Este CNPJ já está vinculado a outra conta."
            )
        # Admin em loja de cliente: troca SÓ o certificado (suporte), o dono continua
        # sendo o cliente. Adotar a loja aqui tiraria os relatórios dele.
        gerenciada_pelo_admin = loja_de_outra_conta
        adotada = existente.usuario_id is None
        if not gerenciada_pelo_admin:
            existente.usuario_id = dono_id
        _aplicar_certificado(existente, dados_certificado)
        # Nome fantasia sempre vem da Receita (regra do cadastro): a loja renomeada
        # ("Moça Bar") não fica com o nome antigo.
        info = _consultar_cnpj(cnpj_limpo)
        nome_atualizado = _atualizar_dados_cadastrais(existente, info)
        # Guarda o endereço para o envio à ACBr. Se a BrasilAPI estiver fora,
        # `info` vem vazio e o endereço já gravado é preservado.
        _aplicar_endereco_empresa(existente, info)
        _salvar_empresa_no_banco(
            db,
            existente,
            "certificado_admin"
            if gerenciada_pelo_admin
            else ("adotada" if adotada else "atualizada"),
        )
        if gerenciada_pelo_admin:
            mensagem = (
                f"Certificado da loja '{existente.nome_loja}' atualizado pelo "
                "administrador (a loja continua na conta do cliente)."
            )
        elif adotada:
            mensagem = f"Loja '{existente.nome_loja}' vinculada à sua conta!"
        else:
            mensagem = f"A loja '{existente.nome_loja}' já estava na sua conta."
        if nome_atualizado:
            mensagem += " Nome atualizado com o Nome Fantasia da Receita."
        return {
            "sucesso": True,
            "mensagem": mensagem,
            "empresa": _empresa_json(existente, _total_notas(db, existente.id)),
            "adotada": adotada,
            "nome_atualizado": nome_atualizado,
            "salva_no_banco": True,
            "acao_banco": (
                "certificado_admin"
                if gerenciada_pelo_admin
                else ("adotada" if adotada else "atualizada")
            ),
            # Rastro da consulta à Receita: "indisponivel" avisa o front que o
            # endereço pode ter de ser preenchido à mão antes da ACBr.
            "fonte_dados": str(info.get("fonte") or "indisponivel"),
            "aviso": _aviso_receita(info),
        }

    info = _consultar_cnpj(cnpj_limpo)
    nova_empresa = Empresa(
        cnpj=cnpj_limpo,
        razao_social=info["razao_social"],
        nome_loja=info["nome_loja"],
        municipio=info["municipio"],
        # Dono = conta logada (nunca o admin/fixo 1).
        usuario_id=dono_id,
    )
    _aplicar_certificado(nova_empresa, dados_certificado)
    _aplicar_endereco_empresa(nova_empresa, info)
    db.add(nova_empresa)
    _salvar_empresa_no_banco(db, nova_empresa, "cadastrada")
    print(
        f"DEBUG: Empresa criada com ID {nova_empresa.id} "
        f"(usuario_id={nova_empresa.usuario_id})",
        flush=True,
    )
    return {
        "sucesso": True,
        "mensagem": f"Empresa '{nova_empresa.nome_loja}' cadastrada e vinculada à sua conta!",
        "empresa": _empresa_json(nova_empresa, 0),
        "adotada": False,
        "nome_atualizado": True,
        "salva_no_banco": True,
        "acao_banco": "criada",
        # Rastro da consulta à Receita (ver `_aviso_receita`).
        "fonte_dados": str(info.get("fonte") or "indisponivel"),
        "aviso": _aviso_receita(info),
    }


@app.delete("/api/empresas/{empresa_id}/certificado")
def excluir_certificado_da_empresa(
    empresa_id: int,
    usuario: Usuario = Depends(auth.usuario_atual),
    db: Session = Depends(get_db),
):
    """Botão "Excluir Certificado" do cartão da loja ("Minhas Lojas").

    Apaga APENAS os dados públicos do certificado (titular/validade/data de envio): a
    loja, o dono e o histórico de notas continuam intactos. O dono da loja e o
    administrador (suporte em qualquer loja) podem excluir — loja de outra conta
    responde 403 (e 404 quando o id não existe).
    """
    empresa = db.query(Empresa).filter(Empresa.id == empresa_id).first()
    if not empresa:
        raise HTTPException(status_code=404, detail="Loja não encontrada.")
    if not _pode_gerenciar_loja(usuario, empresa):
        raise HTTPException(
            status_code=403,
            detail="Esta loja não pertence à sua conta.",
        )
    havia = certificados.limpar_certificado(empresa)
    db.commit()
    db.refresh(empresa)
    return {
        "sucesso": True,
        "mensagem": (
            "Certificado excluído. A loja e o histórico de notas continuam intactos."
            if havia
            else "Esta loja já estava sem certificado cadastrado."
        ),
        "certificado_excluido": havia,
        "empresa": _empresa_json(empresa, _total_notas(db, empresa.id)),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Integração ACBr API: empresa/certificado + download dos XMLs da Distribuição
# ──────────────────────────────────────────────────────────────────────────────
# Um único cliente por processo reaproveita o token OAuth até perto do vencimento.
_CLIENTE_ACBR = None
# Diagnóstico da última automação por CNPJ. O download disparado no cadastro roda em
# background, então o erro só aparece depois da resposta; a rota
# /api/acbr/status/{cnpj} devolve este resumo para o front exibir na tela. A fonte
# definitiva continua sendo o log da API.
_ULTIMAS_AUTOMACOES_ACBR = {}


def _cliente_acbr() -> acbr_client.AcbrClient:
    """Cliente ACBr compartilhado (mantém o token em cache entre as rotas)."""
    global _CLIENTE_ACBR
    if _CLIENTE_ACBR is None:
        _CLIENTE_ACBR = acbr_client.AcbrClient()
    return _CLIENTE_ACBR


def _texto_acbr(valor) -> str:
    return str(valor or "").strip()


def _primeiro_preenchido(*valores) -> str:
    for valor in valores:
        texto = _texto_acbr(valor)
        if texto:
            return texto
    return ""


def _erro_acbr_para_http(erro: acbr_client.ErroAcbr) -> HTTPException:
    """Converte o erro da ACBr em resposta HTTP sem esconder o motivo."""
    if isinstance(erro, acbr_client.AcbrNaoConfigurado):
        return HTTPException(status_code=503, detail=str(erro))
    status = erro.status if isinstance(erro.status, int) else 502
    if status in (401, 403) or status >= 500:
        status = 502
    elif status < 400:
        status = 502
    return HTTPException(status_code=status, detail=str(erro))


def _ambiente_acbr(valor: Optional[str], cliente: acbr_client.AcbrClient) -> str:
    """Valida o ambiente pedido e cai para o configurado no `.env`."""
    alvo = _texto_acbr(valor).lower() or cliente.ambiente_distribuicao
    if alvo not in {"homologacao", "producao"}:
        raise HTTPException(
            status_code=400,
            detail="Ambiente inválido: use 'homologacao' ou 'producao'.",
        )
    return alvo


def _montar_empresa_acbr(
    empresa: Empresa,
    info: dict,
    overrides: dict,
    email_padrao: str = "",
) -> dict:
    """Monta o JSON exigido por ``POST /empresas`` da ACBr.

    O contrato exige nome, e-mail e endereço completo (`cep`, `uf`,
    `codigo_municipio`, `bairro`, `numero` e `logradouro`). A prioridade é:
    dados do formulário -> endereço já gravado na loja -> BrasilAPI. Assim o
    cadastro continua funcionando quando a BrasilAPI está limitada (429) ou
    offline, desde que a loja tenha sido vinculada com o endereço completo.
    """
    info = info or {}
    overrides = overrides or {}
    endereco_empresa = _endereco_empresa(empresa)
    cnpj = re.sub(r"\D", "", str(empresa.cnpj or ""))
    nome = _primeiro_preenchido(
        empresa.razao_social,
        empresa.nome_loja,
        info.get("razao_social"),
    )
    email = _primeiro_preenchido(
        overrides.get("email"), info.get("email"), email_padrao
    )
    endereco = {
        "logradouro": _primeiro_preenchido(
            overrides.get("logradouro"),
            endereco_empresa.get("logradouro"),
            info.get("logradouro"),
        ),
        "numero": _primeiro_preenchido(
            overrides.get("numero"),
            endereco_empresa.get("numero"),
            info.get("numero"),
            "S/N",
        ),
        "complemento": _primeiro_preenchido(
            overrides.get("complemento"),
            endereco_empresa.get("complemento"),
            info.get("complemento"),
        ),
        "bairro": _primeiro_preenchido(
            overrides.get("bairro"),
            endereco_empresa.get("bairro"),
            info.get("bairro"),
        ),
        "codigo_municipio": re.sub(
            r"\D",
            "",
            _primeiro_preenchido(
                overrides.get("codigo_municipio"),
                endereco_empresa.get("codigo_municipio"),
                info.get("codigo_municipio"),
            ),
        ),
        "cidade": _primeiro_preenchido(
            overrides.get("cidade"),
            empresa.municipio,
            info.get("municipio"),
        ),
        "uf": _primeiro_preenchido(
            overrides.get("uf"),
            endereco_empresa.get("uf"),
            info.get("uf"),
        ).upper(),
        "cep": re.sub(
            r"\D",
            "",
            _primeiro_preenchido(
                overrides.get("cep"),
                endereco_empresa.get("cep"),
                info.get("cep"),
            ),
        ),
        "codigo_pais": "1058",
        "pais": "BRASIL",
    }

    faltando = [
        rotulo
        for campo, rotulo in (
            ("logradouro", "logradouro"),
            ("numero", "número"),
            ("bairro", "bairro"),
            ("codigo_municipio", "código IBGE do município"),
            ("uf", "UF"),
            ("cep", "CEP"),
        )
        if not endereco[campo]
    ]
    if not nome:
        faltando.insert(0, "razão social")
    if not email:
        faltando.append("e-mail")
    if faltando:
        raise HTTPException(
            status_code=400,
            detail=(
                "Complete o cadastro da empresa para a ACBr. Faltando: "
                + ", ".join(faltando)
                + "."
            ),
        )
    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise HTTPException(status_code=400, detail="Informe um e-mail válido.")
    if len(endereco["cep"]) != 8:
        raise HTTPException(
            status_code=400, detail="O CEP deve ter 8 dígitos (sem máscara)."
        )
    if len(endereco["codigo_municipio"]) != 7:
        raise HTTPException(
            status_code=400,
            detail="O código IBGE do município deve ter 7 dígitos.",
        )
    if len(endereco["uf"]) != 2:
        raise HTTPException(
            status_code=400, detail="Informe a UF com 2 letras (ex.: SP)."
        )

    dados = {
        "cpf_cnpj": cnpj,
        "nome_razao_social": nome,
        "nome_fantasia": _primeiro_preenchido(empresa.nome_loja, nome),
        "email": email,
        "endereco": endereco,
    }
    telefone = _primeiro_preenchido(overrides.get("telefone"), info.get("telefone"))
    for campo, valor in (
        ("fone", telefone),
        ("inscricao_estadual", overrides.get("inscricao_estadual")),
        ("inscricao_municipal", overrides.get("inscricao_municipal")),
    ):
        if _texto_acbr(valor):
            dados[campo] = _texto_acbr(valor)
    return dados

# ──────────────────────────────────────────────────────────────────────────────

def _data_acbr(valor) -> Optional[date]:
    """Converte data/hora ISO da ACBr (ou do XML) em `date`; None se inválida."""
    texto = _texto_acbr(valor)
    if not texto:
        return None
    try:
        return date.fromisoformat(texto[:10])
    except ValueError:
        return None


def _dados_xml_acbr(conteudo: bytes) -> tuple:
    """Extrai (data de emissão, chave, emitente) do XML completo da NF-e."""
    try:
        raiz = ET.fromstring(conteudo)
    except (ET.ParseError, TypeError, ValueError):
        return None, "", ""
    inf_nota = raiz.find(".//{*}infNFe")
    if inf_nota is None:
        return None, "", ""
    chave = re.sub(r"\D", "", str(inf_nota.get("Id") or ""))
    ide = inf_nota.find("{*}ide")
    data_emissao = _data_acbr(
        _texto_xml(ide, "{*}dhEmi") or _texto_xml(ide, "{*}dEmi")
    )
    emitente = _texto_xml(inf_nota.find("{*}emit"), "{*}xNome")
    return data_emissao, chave, emitente


def _data_documento_acbr(documento: dict) -> Optional[date]:
    """Data fiscal de fallback quando o XML não pôde ser interpretado.

    `created_at` (data em que a ACBr armazenou o documento) não entra: ele não
    representa a emissão e poderia classificar um XML no período errado.
    """
    for campo in ("data_recebimento", "data_evento"):
        data = _data_acbr((documento or {}).get(campo))
        if data:
            return data
    return None


def _pasta_xmls_acbr(cnpj: str) -> str:
    """Pasta local dos XMLs baixados (`xmls/acbr/<CNPJ>` por padrão).

    `ACBR_XML_DIR` pode apontar para outro diretório (usado nos testes). O CNPJ
    é só dígitos, então não há como escapar da pasta base.
    """
    bruto = _texto_acbr(os.getenv("ACBR_XML_DIR")) or "xmls/acbr"
    base = bruto if os.path.isabs(bruto) else os.path.join(PROJECT_ROOT, bruto)
    base = os.path.abspath(base)
    destino = os.path.abspath(os.path.join(base, cnpj))
    if os.path.commonpath([destino, base]) != base:
        raise HTTPException(status_code=400, detail="CNPJ inválido para salvar os XMLs.")
    os.makedirs(destino, exist_ok=True)
    return destino


def _caminho_publico(caminho: str) -> str:
    """Caminho relativo à raiz do projeto (sem expor o disco na resposta)."""
    try:
        relativo = os.path.relpath(caminho, PROJECT_ROOT)
    except ValueError:
        return caminho
    if relativo.startswith(".."):
        return caminho
    return relativo.replace("\\", "/")


def _salvar_xml_acbr(cnpj: str, identificador: str, conteudo: bytes) -> str:
    """Grava o XML (sem sobrescrever fora da pasta da empresa) e devolve o caminho."""
    nome = re.sub(r"[^A-Za-z0-9_.-]", "_", _texto_acbr(identificador))
    if not nome:
        nome = f"documento-{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
    if not nome.lower().endswith(".xml"):
        nome += ".xml"
    pasta = _pasta_xmls_acbr(cnpj)
    caminho = os.path.join(pasta, nome)
    with open(caminho, "wb") as arquivo:
        arquivo.write(conteudo)
    return caminho


def _resumo_distribuicao(distribuicao: dict) -> dict:
    """Só os campos de status que o front precisa (sem despejar o lote inteiro)."""
    distribuicao = distribuicao or {}
    return {
        campo: distribuicao.get(campo)
        for campo in (
            "id",
            "status",
            "ambiente",
            "tipo_consulta",
            "codigo_status",
            "motivo_status",
            "data_hora_resposta",
            "ultimo_nsu",
            "ultNSU",
            "max_nsu",
        )
        if campo in distribuicao
    }


def _registrar_automacao_acbr(cnpj: str, resultado: dict) -> dict:
    """Guarda o resumo da última automação para a rota de status.

    O cadastro do certificado responde antes do download terminar. Sem este
    registro, um erro da ACBr (certificado ausente, fila vazia, tempo de espera)
    ficaria só no log e o cliente não teria como diagnosticar pela tela.
    """
    registro = dict(resultado or {})
    registro["registrado_em"] = datetime.now(timezone.utc).isoformat()
    _ULTIMAS_AUTOMACOES_ACBR[cnpj] = registro
    return registro


def _resumo_download_automacao(resultado: dict) -> dict:
    """Resumo pequeno do download (sem a lista completa de XMLs) para o status."""
    resultado = resultado or {}
    return {
        "mensagem": resultado.get("mensagem"),
        "primeiro_download": resultado.get("primeiro_download"),
        "periodo": resultado.get("periodo"),
        "documentos_encontrados": resultado.get("documentos_encontrados", 0),
        "documentos_baixados": resultado.get("documentos_baixados", 0),
        "documentos_ignorados": resultado.get("documentos_ignorados", 0),
        "ultimo_nsu_acbr": resultado.get("ultimo_nsu_acbr"),
        "distribuicao": resultado.get("distribuicao") or {},
        "avisos": list(resultado.get("avisos") or []),
    }


DIAS_PRIMEIRO_DOWNLOAD_PADRAO = 7


def _dias_primeiro_download_acbr() -> int:
    """Lê ``DIAS_PRIMEIRO_DOWNLOAD``: janela do primeiro download da ACBr.

    O padrão é 7 dias porque o cliente já importa o histórico manual de notas
    antigas; baixar todo o histórico na primeira consulta só consumiria créditos.
    Valores vazios, inválidos ou menores que 1 caem no padrão para não quebrar
    a inicialização da API.
    """
    texto = _texto_acbr(
        os.getenv("DIAS_PRIMEIRO_DOWNLOAD", str(DIAS_PRIMEIRO_DOWNLOAD_PADRAO))
    )
    try:
        dias = int(texto)
    except (TypeError, ValueError):
        dias = 0
    if dias < 1:
        logger.warning(
            "DIAS_PRIMEIRO_DOWNLOAD inválido (%r); usando %s dias.",
            texto,
            DIAS_PRIMEIRO_DOWNLOAD_PADRAO,
        )
        return DIAS_PRIMEIRO_DOWNLOAD_PADRAO
    return dias


DIAS_PRIMEIRO_DOWNLOAD_ACBR = _dias_primeiro_download_acbr()


def _normalizar_nsu_acbr(valor) -> str:
    """NSU como texto numérico, sem zeros à esquerda; vazio quando não há valor."""
    if valor is None or isinstance(valor, bool):
        return ""
    texto = str(valor).strip()
    if not texto:
        return ""
    try:
        return str(max(0, int(texto)))
    except ValueError:
        pass
    try:
        return str(max(0, int(float(texto))))
    except ValueError:
        digitos = re.sub(r"\D", "", texto)
        return str(int(digitos)) if digitos else ""


def _nsu_acbr_int(valor) -> int:
    """NSU como inteiro para enviar à ACBr (0 = primeiro download)."""
    normalizado = _normalizar_nsu_acbr(valor)
    return int(normalizado) if normalizado else 0


def _ultimo_nsu_distribuicao(distribuicao: dict) -> str:
    """Último NSU devolvido pela ACBr (aceita os nomes usados pelas versões da API)."""
    distribuicao = distribuicao if isinstance(distribuicao, dict) else {}
    candidatos = []
    for campo in ("ultimo_nsu", "ultNSU", "ult_nsu", "ultimoNSU"):
        normalizado = _normalizar_nsu_acbr(distribuicao.get(campo))
        if normalizado:
            candidatos.append(int(normalizado))

    # Alguns retornos trazem o lote em `documentos`, cada um com seu NSU. Usar o
    # maior valor também cobre APIs que devolvem `ultimo_nsu = 0` junto do lote.
    documentos = distribuicao.get("documentos") or distribuicao.get("data") or []
    for documento in documentos if isinstance(documentos, list) else []:
        if not isinstance(documento, dict):
            continue
        for campo in ("nsu", "NSU", "ultNSU"):
            normalizado = _normalizar_nsu_acbr(documento.get(campo))
            if normalizado:
                candidatos.append(int(normalizado))
                break
    return str(max(candidatos)) if candidatos else ""


def _periodo_consulta_acbr(
    data_inicial: date, data_final: date, primeiro_download: bool
) -> tuple:
    """Primeiro download cobre a janela configurada (padrão: 7 dias).

    A janela vem de ``DIAS_PRIMEIRO_DOWNLOAD``. O front pode pedir um período
    maior, mas a primeira consulta é limitada a essa janela para não consumir
    créditos da ACBr com o histórico que o cliente já importou manualmente.
    Depois do primeiro NSU salvo, o período informado é mantido e o avanço
    passa a ser pelo NSU.
    """
    if not primeiro_download:
        return data_inicial, data_final
    hoje = date.today()
    return (
        hoje - timedelta(days=DIAS_PRIMEIRO_DOWNLOAD_ACBR),
        hoje + timedelta(days=1),
    )


def _automacao_acbr_habilitada() -> bool:
    """Se o cadastro do certificado deve disparar download + importação.

    Fica ligado por padrão ("zero cliques"). A variável de ambiente
    ``ACBR_AUTO_IMPORTAR_APOS_CERTIFICADO=0`` desliga a automação em testes ou em
    ambientes que preferem baixar/importar manualmente pela rota.
    """
    valor = _texto_acbr(os.getenv("ACBR_AUTO_IMPORTAR_APOS_CERTIFICADO", "1")).lower()
    return valor not in {"0", "false", "nao", "não", "off", "desligado"}


@app.post("/api/acbr/empresas")
async def cadastrar_empresa_na_acbr(
    background_tasks: BackgroundTasks,
    usuario: Usuario = Depends(auth.usuario_atual),
    db: Session = Depends(get_db),
    cnpj: str = Form("", description="CNPJ da loja (criada no banco se ainda não existir)"),
    senha_certificado: str = Form("", description="Senha do certificado .pfx/.p12"),
    certificado: Optional[UploadFile] = File(
        None, description="Certificado digital A1 da empresa (.pfx ou .p12)"
    ),
    cep: str = Form(""),
    logradouro: str = Form(""),
    numero: str = Form(""),
    complemento: str = Form(""),
    bairro: str = Form(""),
    codigo_municipio: str = Form("", description="Código IBGE com 7 dígitos"),
    cidade: str = Form(""),
    uf: str = Form(""),
    email: str = Form(""),
    telefone: str = Form(""),
    inscricao_estadual: str = Form(""),
    inscricao_municipal: str = Form(""),
    configurar_distribuicao: bool = Form(True),
):
    """Executa o cadastro ACBr exibindo no terminal o erro real de qualquer falha.

    Erros esperados da aplicação (``HTTPException``: CNPJ inválido, loja de outra
    conta, certificado recusado etc.) continuam com o status/detail originais —
    transformá-los em 500 esconderia a causa. Qualquer exceção inesperada vira um
    JSON 500 com ``detail: "Erro interno: ..."`` para aparecer na tela e no log.
    """
    print(f"DEBUG: Tentando cadastrar CNPJ {cnpj}", flush=True)
    try:
        return await _cadastrar_empresa_na_acbr_logica(
            background_tasks=background_tasks,
            usuario=usuario,
            db=db,
            cnpj=cnpj,
            senha_certificado=senha_certificado,
            certificado=certificado,
            cep=cep,
            logradouro=logradouro,
            numero=numero,
            complemento=complemento,
            bairro=bairro,
            codigo_municipio=codigo_municipio,
            cidade=cidade,
            uf=uf,
            email=email,
            telefone=telefone,
            inscricao_estadual=inscricao_estadual,
            inscricao_municipal=inscricao_municipal,
            configurar_distribuicao=configurar_distribuicao,
        )
    except HTTPException as erro:
        print(
            f"DEBUG: Erro HTTP {erro.status_code} no cadastro de {cnpj}: "
            f"{erro.detail}",
            flush=True,
        )
        raise
    except Exception as erro:
        print(f"DEBUG: Erro: {erro}", flush=True)
        logger.exception(
            "Erro interno inesperado no cadastro ACBr do CNPJ %s", cnpj
        )
        return JSONResponse(
            status_code=500,
            content={"detail": f"Erro interno: {str(erro)}"},
        )


async def _cadastrar_empresa_na_acbr_logica(
    background_tasks: BackgroundTasks,
    usuario: Usuario,
    db: Session,
    cnpj: str,
    senha_certificado: str,
    certificado: Optional[UploadFile],
    cep: str,
    logradouro: str,
    numero: str,
    complemento: str,
    bairro: str,
    codigo_municipio: str,
    cidade: str,
    uf: str,
    email: str,
    telefone: str,
    inscricao_estadual: str,
    inscricao_municipal: str,
    configurar_distribuicao: bool,
):
    """Cadastra/atualiza a empresa e o certificado digital na ACBr API.

    É multipart porque recebe o `.pfx/.p12` e a senha — os mesmos dados da
    barreira de vínculo da loja. O arquivo e a senha NÃO são persistidos: seguem
    apenas para a ACBr (base64) e o banco guarda só os dados públicos do
    certificado. Uma loja já existente precisa pertencer à conta logada; se o
    CNPJ ainda não estiver no banco, a rota cria a loja para a conta logada
    depois de validar o certificado (assim o cadastro nunca fica "silencioso":
    a empresa aparece em ``/api/empresas/vinculadas`` mesmo se a ACBr falhar).

    O JSON da ACBr exige endereço completo. Os campos do formulário têm
    prioridade; o que não for enviado é completado pelo endereço já gravado na
    loja e, por último, pela BrasilAPI. O e-mail cai para o e-mail da conta
    quando a Receita não informa um. O endereço usado no payload é persistido
    na empresa para que um reenvio não dependa da BrasilAPI.

    Depois do commit, o download da Distribuição NF-e e a importação dos XMLs
    rodam em background (``BackgroundTasks``). A rota responde imediatamente e o
    cliente não precisa clicar em nada para ver os relatórios preenchidos.
    """
    cnpj_limpo = re.sub(r"\D", "", str(cnpj or ""))
    if len(cnpj_limpo) != 14:
        raise HTTPException(status_code=400, detail="Informe um CNPJ com 14 dígitos.")
    # A loja criada nesta rota pertence SEMPRE à conta do token (nunca ao admin).
    dono_id = _id_do_usuario_logado(usuario)
    logger.info(
        "Cadastro ACBr do CNPJ %s solicitado pelo usuário %s (%s)",
        cnpj_limpo,
        dono_id,
        usuario.email,
    )

    empresa = db.query(Empresa).filter(Empresa.cnpj == cnpj_limpo).first()
    banco_acao = "existente"
    if empresa is not None:
        if not _pode_gerenciar_loja(usuario, empresa):
            raise HTTPException(
                status_code=403, detail="Esta loja não pertence à sua conta."
            )
        logger.info(
            "Empresa %s já cadastrada no banco: id=%s usuario_id=%s",
            cnpj_limpo,
            empresa.id,
            empresa.usuario_id,
        )
    else:
        logger.info(
            "Empresa %s ainda não existe no banco; será criada após validar o certificado.",
            cnpj_limpo,
        )

    conteudo_certificado = await certificado.read() if certificado is not None else None
    try:
        dados_certificado = certificados.validar_certificado(
            certificado.filename if certificado is not None else None,
            conteudo_certificado,
            senha_certificado,
            cnpj=cnpj_limpo,
        )
    except HTTPException as erro:
        logger.warning(
            "Erro ao cadastrar empresa %s na ACBr (certificado): %s",
            cnpj_limpo,
            erro.detail,
        )
        raise

    # A rota também funciona de forma autônoma (Swagger/reenvio): o CNPJ novo é
    # criado no banco com o dono logado ANTES da ACBr. Assim uma recusa da ACBr
    # não faz a loja "desaparecer" — ela continua na lista para correção/reenvio.
    info_cadastro = {}
    if empresa is None:
        # Sem nenhuma condição do tipo "o usuário já tem loja?": CNPJ que ainda não
        # existe é SEMPRE criado para a conta do token, mesmo que ela tenha zero
        # lojas. A consulta acima só evita duplicar a linha quando o CNPJ já está
        # no banco (reenvio do certificado) — não é pré-requisito para cadastrar.
        try:
            print(f"DEBUG: Tentando cadastrar CNPJ {cnpj_limpo}", flush=True)
            print(
                f"DEBUG: Criando empresa para usuario_id={usuario.id}",
                flush=True,
            )
            info_cadastro = _consultar_cnpj(cnpj_limpo)
            nova_empresa = Empresa(
                cnpj=cnpj_limpo,
                razao_social=info_cadastro["razao_social"],
                nome_loja=info_cadastro["nome_loja"],
                municipio=info_cadastro["municipio"],
                # Dono = conta logada (nunca o admin/fixo 1). Sem fallback silencioso.
                usuario_id=dono_id,
            )
            _aplicar_certificado(nova_empresa, dados_certificado)
            _aplicar_endereco_empresa(nova_empresa, info_cadastro)
            db.add(nova_empresa)
            _salvar_empresa_no_banco(db, nova_empresa, "cadastrada")
            empresa = nova_empresa
            banco_acao = "criada"
            print(
                f"DEBUG: Empresa criada com ID {nova_empresa.id}",
                flush=True,
            )
        except HTTPException as erro:
            # Erros esperados da aplicação (4xx) mantêm o status/detail originais.
            # O 500 de `_salvar_empresa_no_banco` entra no formato pedido pela tela.
            if erro.status_code < 500:
                raise
            db.rollback()
            print(f"DEBUG: Erro ao cadastrar {cnpj_limpo}: {erro.detail}", flush=True)
            logger.exception(
                "Falha ao criar a empresa %s no banco antes do envio à ACBr",
                cnpj_limpo,
            )
            return JSONResponse(
                status_code=500,
                content={"detail": f"Erro ao cadastrar: {erro.detail}"},
            )
        except Exception as erro:
            # Qualquer falha de persistência vira 500 com o motivo real, em vez de
            # deixar a criação "silenciosa" (antes a tela só via a lista vazia).
            db.rollback()
            print(f"DEBUG: Erro ao cadastrar {cnpj_limpo}: {erro}", flush=True)
            logger.exception(
                "Falha ao criar a empresa %s no banco antes do envio à ACBr",
                cnpj_limpo,
            )
            return JSONResponse(
                status_code=500,
                content={"detail": f"Erro ao cadastrar: {str(erro)}"},
            )

    overrides = {
        "cep": cep,
        "logradouro": logradouro,
        "numero": numero,
        "complemento": complemento,
        "bairro": bairro,
        "codigo_municipio": codigo_municipio,
        "cidade": cidade,
        "uf": uf,
        "email": email,
        "telefone": telefone,
        "inscricao_estadual": inscricao_estadual,
        "inscricao_municipal": inscricao_municipal,
    }
    # Só consulta a Receita quando nem o formulário nem o cadastro da loja têm o
    # endereço inteiro. Isso evita depender da BrasilAPI (e tomar 429) quando o
    # endereço já foi persistido no vínculo da loja.
    endereco_obrigatorio = ("cep", "logradouro", "bairro", "codigo_municipio", "uf")
    endereco_empresa = _endereco_empresa(empresa)
    info = info_cadastro
    if not info and any(
        not _texto_acbr(overrides.get(campo) or endereco_empresa.get(campo))
        for campo in endereco_obrigatorio
    ):
        info = _consultar_cnpj(cnpj_limpo)
    dados_acbr = _montar_empresa_acbr(
        empresa, info, overrides, email_padrao=usuario.email
    )

    # Persiste o endereço efetivamente enviado (formulário + banco + BrasilAPI)
    # ANTES da chamada à ACBr. Se a ACBr recusar o cadastro, o próximo reenvio
    # ainda consegue montar o payload sem depender da BrasilAPI.
    if _aplicar_endereco_empresa(empresa, dados_acbr.get("endereco") or {}):
        _salvar_empresa_no_banco(db, empresa, "atualizada")

    cliente = _cliente_acbr()
    try:
        logger.info("Enviando empresa %s para a ACBr API...", cnpj_limpo)
        existente = None
        try:
            existente = cliente.consultar_empresa(cnpj_limpo)
        except acbr_client.ErroAcbr as erro:
            if erro.status != 404:
                raise
        if existente is not None:
            empresa_acbr = cliente.atualizar_empresa(cnpj_limpo, dados_acbr)
            criada_na_acbr = False
        else:
            try:
                empresa_acbr = cliente.cadastrar_empresa(dados_acbr)
                criada_na_acbr = True
            except acbr_client.ErroAcbr as erro:
                # Corrida entre duas chamadas: a empresa já existe, então atualiza.
                if erro.status != 409:
                    raise
                empresa_acbr = cliente.atualizar_empresa(cnpj_limpo, dados_acbr)
                criada_na_acbr = False
        logger.info(
            "Empresa %s %s na ACBr API.",
            cnpj_limpo,
            "cadastrada" if criada_na_acbr else "atualizada",
        )

        certificado_acbr = cliente.enviar_certificado(
            cnpj_limpo, conteudo_certificado, senha_certificado
        )
        logger.info(
            "Certificado do CNPJ %s enviado para a ACBr API.", cnpj_limpo
        )
        configuracao = None
        aviso = None
        if configurar_distribuicao:
            try:
                configuracao = cliente.configurar_distribuicao_nfe(
                    cnpj_limpo, cliente.ambiente_distribuicao
                )
                logger.info(
                    "Distribuição NF-e do CNPJ %s configurada (%s).",
                    cnpj_limpo,
                    cliente.ambiente_distribuicao,
                )
            except acbr_client.ErroAcbr as erro:
                # A empresa e o certificado já foram aceitos; a configuração pode
                # ser refeita sem perder o cadastro.
                aviso = f"Empresa e certificado OK, mas a configuração de Distribuição NF-e falhou: {erro}"
                logger.warning(
                    "Empresa %s: certificado OK, mas a configuração da "
                    "Distribuição NF-e falhou: %s",
                    cnpj_limpo,
                    erro,
                )
    except acbr_client.ErroAcbr as erro:
        logger.exception(
            "Erro ao cadastrar empresa %s na ACBr API: %s", cnpj_limpo, erro
        )
        http = _erro_acbr_para_http(erro)
        # Mensagem amigável: o cliente vê o que fazer, sem perder o motivo real
        # devolvido pela ACBr (HTTP/status/corpo) no fim do texto.
        http.detail = f"Não foi possível concluir o cadastro na ACBr API: {http.detail}"
        raise http from erro

    _aplicar_certificado(empresa, dados_certificado)
    _salvar_empresa_no_banco(db, empresa, "atualizada")

    # "Zero cliques": a resposta sai antes do download; a rotina usa uma sessão
    # própria (o `db` da requisição é fechado ao terminar) e importa só a pasta
    # desta loja, preservando as notas que já existem.
    background_iniciado = _automacao_acbr_habilitada()
    if background_iniciado:
        background_tasks.add_task(
            _baixar_e_importar_acbr_em_background,
            cnpj_limpo,
            cliente.ambiente_distribuicao,
        )
        mensagem = (
            "Certificado enviado! Iniciando download e importação em background. "
            "Acompanhe em Minhas Lojas > Consultar Status ACBr."
        )
    else:
        mensagem = (
            "Empresa cadastrada na ACBr API com o certificado digital."
            if criada_na_acbr
            else "Empresa atualizada na ACBr API com o certificado digital."
        )

    return {
        "sucesso": True,
        "mensagem": mensagem,
        "background_iniciado": background_iniciado,
        "cnpj": cnpj_limpo,
        "ambiente": cliente.ambiente_distribuicao,
        "empresa": _empresa_json(empresa),
        "salva_no_banco": True,
        "banco": {
            "acao": banco_acao,
            "empresa_id": empresa.id,
            "usuario_id": empresa.usuario_id,
            "cnpj": empresa.cnpj,
        },
        "criada_na_acbr": criada_na_acbr,
        "empresa_acbr": empresa_acbr,
        "certificado_acbr": certificado_acbr,
        "certificado": certificados.certificado_json(empresa),
        "distribuicao_configurada": configuracao is not None,
        "distribuicao_config": configuracao,
        "aviso": aviso,
    }


class AcbrBaixarXmlsRequest(BaseModel):
    """Corpo de POST /api/acbr/baixar-xmls.

    ``data_inicial``/``data_final`` delimitam o período **local** usado para
    filtrar os documentos depois do download. Elas não são enviadas à ACBr:
    o contrato ``DistribuicaoNfePedido`` (v3.1.18) não aceita campos de data e
    o avanço é sempre pelo NSU gravado em ``Empresa.ultimo_nsu_acbr``. No
    primeiro download (NSU ``0``) a janela é ``DIAS_PRIMEIRO_DOWNLOAD`` dias
    (padrão 7); depois disso o recorte local continua valendo.
    """

    cnpj: str
    data_inicial: date
    data_final: date
    ambiente: Optional[str] = None
    # Mantido por compatibilidade de contrato: a fonte da verdade é o NSU salvo
    # na empresa. 0 é usado somente quando a loja ainda nunca baixou nada.
    dist_nsu: int = Field(0, ge=0)
    ignorar_tempo_espera: bool = False
    limite_documentos: int = Field(100, ge=1, le=500)
    salvar_arquivos: bool = True
    uf_autor: Optional[str] = None


@app.post("/api/acbr/baixar-xmls")
def baixar_xmls_da_acbr(
    dados: AcbrBaixarXmlsRequest,
    usuario: Usuario = Depends(auth.usuario_atual),
    db: Session = Depends(get_db),
):
    """Pede a distribuição, baixa os XMLs completos e filtra pelo período.

    Fluxo: garante a configuração de Distribuição NF-e, faz um pedido
    ``dist-nsu`` usando o cursor persistido em ``Empresa.ultimo_nsu_acbr``
    (loja nova, com NSU ``0``, começa do início da fila), lista os documentos já
    distribuídos (paginado), baixa o XML dos documentos completos de NF-e e
    compara a data de emissão com o período **local**. A ACBr não recebe datas:
    o schema de ``POST /distribuicao/nfe`` não possui esses campos. Os aprovados
    são gravados em ``xmls/acbr/<CNPJ>/<chave>.xml``. O último NSU devolvido pela
    ACBr é salvo em ``Empresa.ultimo_nsu_acbr``. Nenhum XML é importado no banco
    de notas: download e importação continuam separados.
    """
    cnpj_limpo = re.sub(r"\D", "", str(dados.cnpj or ""))
    if len(cnpj_limpo) != 14:
        raise HTTPException(status_code=400, detail="Informe um CNPJ com 14 dígitos.")
    if dados.data_inicial > dados.data_final:
        raise HTTPException(
            status_code=400,
            detail="A data inicial não pode ser posterior à data final.",
        )

    empresa = db.query(Empresa).filter(Empresa.cnpj == cnpj_limpo).first()
    if not empresa:
        raise HTTPException(status_code=404, detail="CNPJ não encontrado.")
    # Admin gerencia todas as lojas (suporte); a conta comum, só as próprias.
    if not _pode_gerenciar_loja(usuario, empresa):
        raise HTTPException(
            status_code=403, detail="Esta loja não pertence à sua conta."
        )

    nsu_salvo = _normalizar_nsu_acbr(empresa.ultimo_nsu_acbr) or "0"
    primeiro_download = _nsu_acbr_int(nsu_salvo) == 0
    periodo_inicial, periodo_final = _periodo_consulta_acbr(
        dados.data_inicial, dados.data_final, primeiro_download
    )

    cliente = _cliente_acbr()
    ambiente = _ambiente_acbr(dados.ambiente, cliente)
    try:
        cliente.configurar_distribuicao_nfe(cnpj_limpo, ambiente)
        distribuicao = cliente.distribuir_nfe(
            cnpj_limpo,
            ambiente=ambiente,
            dist_nsu=_nsu_acbr_int(nsu_salvo),
            uf_autor=dados.uf_autor,
            ignorar_tempo_espera=dados.ignorar_tempo_espera,
        )

        documentos = []
        paginas = 0
        skip = 0
        while len(documentos) < dados.limite_documentos and paginas < 20:
            paginas += 1
            top = min(50, dados.limite_documentos - len(documentos))
            lote = cliente.listar_documentos_nfe(
                cnpj_limpo, ambiente=ambiente, top=top, skip=skip
            )
            pagina = (lote or {}).get("data") or []
            if not isinstance(pagina, list):
                pagina = []
            restante = dados.limite_documentos - len(documentos)
            documentos.extend(pagina[:restante])
            total = (lote or {}).get("@count")
            try:
                total = int(total) if total is not None else None
            except (TypeError, ValueError):
                total = None
            if (
                not pagina
                or len(pagina) < top
                or (total is not None and skip + len(pagina) >= total)
            ):
                break
            skip += len(pagina)
    except acbr_client.ErroAcbr as erro:
        raise _erro_acbr_para_http(erro) from erro


    xmls = []
    ignorados = []
    resumos = 0
    vistos = set()
    for documento in documentos:
        documento = documento if isinstance(documento, dict) else {}
        documento_id = _texto_acbr(documento.get("id"))
        if not documento_id:
            ignorados.append({"id": None, "motivo": "documento sem id"})
            continue
        if documento_id in vistos:
            continue
        vistos.add(documento_id)

        tipo = _texto_acbr(documento.get("tipo_documento")).lower()
        if tipo and tipo != "nota":
            ignorados.append({"id": documento_id, "motivo": f"tipo {tipo}"})
            continue
        resumo = documento.get("resumo")
        if resumo is True or str(resumo or "").strip().lower() in {"true", "1", "sim"}:
            resumos += 1
            ignorados.append(
                {
                    "id": documento_id,
                    "motivo": "documento resumido (XML completo ainda não disponível)",
                }
            )
            continue

        try:
            conteudo = cliente.baixar_xml_documento(documento_id)
        except (acbr_client.ErroAcbr, ValueError) as erro:
            ignorados.append(
                {"id": documento_id, "motivo": f"falha no download: {erro}"}
            )
            continue
        if not conteudo:
            ignorados.append({"id": documento_id, "motivo": "XML vazio"})
            continue

        data_emissao, chave, emitente = _dados_xml_acbr(conteudo)
        data_referencia = data_emissao or _data_documento_acbr(documento)
        if not data_referencia:
            ignorados.append(
                {"id": documento_id, "motivo": "data de emissão não identificada"}
            )
            continue
        if not (periodo_inicial <= data_referencia <= periodo_final):
            ignorados.append(
                {
                    "id": documento_id,
                    "motivo": f"fora do período ({data_referencia.isoformat()})",
                }
            )
            continue

        chave = chave or re.sub(r"\D", "", str(documento.get("chave_acesso") or ""))
        item = {
            "documento_id": documento_id,
            "chave_acesso": chave,
            "data_emissao": data_referencia.isoformat(),
            "emitente": emitente
            or _texto_acbr(documento.get("emitente_nome_razao_social")),
            "tamanho_bytes": len(conteudo),
        }
        if dados.salvar_arquivos:
            try:
                caminho = _salvar_xml_acbr(
                    cnpj_limpo, chave or f"documento-{documento_id}", conteudo
                )
            except OSError as erro:
                raise HTTPException(
                    status_code=500,
                    detail=f"Não foi possível salvar o XML {documento_id}: {erro}",
                ) from erro
            item["arquivo"] = _caminho_publico(caminho)
        else:
            item["xml"] = conteudo.decode("utf-8-sig", errors="replace")
        xmls.append(item)

    avisos = []
    status_distribuicao = _texto_acbr((distribuicao or {}).get("status")).lower()
    if status_distribuicao == "processando":
        avisos.append(
            "A ACBr ainda está processando o pedido de distribuição; "
            "execute novamente em alguns minutos para buscar novos XMLs."
        )
    elif status_distribuicao == "erro":
        codigo = (distribuicao or {}).get("codigo_status")
        motivo = _texto_acbr((distribuicao or {}).get("motivo_status"))
        avisos.append(
            "A ACBr retornou erro no pedido de distribuição"
            + (f" ({codigo})" if codigo is not None else "")
            + (f": {motivo}" if motivo else ".")
        )
    if resumos:
        avisos.append(
            f"{resumos} documento(s) só tinham resumo (resNFe); "
            "o XML completo é liberado depois da manifestação/ciência."
        )
    if not documentos:
        avisos.append(
            "Nenhum documento foi listado pela ACBr para este CNPJ/ambiente."
        )

    nsu_retornado = _ultimo_nsu_distribuicao(distribuicao)
    if nsu_retornado:
        # A ACBr devolve o último NSU do lote; salvá-lo evita repetir a janela
        # inicial e faz as próximas consultas avançarem pelo NSU.
        empresa.ultimo_nsu_acbr = nsu_retornado
        db.commit()
        nsu_salvo = nsu_retornado
    else:
        avisos.append(
            "A ACBr não retornou o último NSU; o valor salvo foi mantido em "
            f"{nsu_salvo}."
        )

    mensagem = (
        f"Documentos baixados: {len(xmls)}. Novo NSU salvo: {nsu_salvo}."
    )

    return {
        "sucesso": True,
        "cnpj": cnpj_limpo,
        "ambiente": ambiente,
        "primeiro_download": primeiro_download,
        "periodo": {
            "data_inicial": periodo_inicial.isoformat(),
            "data_final": periodo_final.isoformat(),
        },
        "distribuicao": _resumo_distribuicao(distribuicao),
        "documentos_encontrados": len(documentos),
        "documentos_baixados": len(xmls),
        "documentos_ignorados": len(ignorados),
        "ultimo_nsu_acbr": nsu_salvo,
        "novo_nsu": nsu_salvo,
        "mensagem": mensagem,
        "xmls": xmls,
        "ignorados": ignorados,
        "avisos": avisos,
    }


@app.get("/api/acbr/status/{cnpj}")
def status_acbr(
    cnpj: str,
    usuario: Usuario = Depends(auth.usuario_atual),
    db: Session = Depends(get_db),
):
    """Compara o NSU salvo na loja com o cursor devolvido pela ACBr.

    Serve para diagnosticar por que o download automático não trouxe XMLs:
    devolve o NSU local, o último NSU devolvido na consulta, o estado do
    certificado e o resumo da última automação daquele CNPJ.

    A ACBr exige que ``dist_nsu`` seja igual ao ``ultNSU`` da consulta anterior;
    por isso o cliente lê ``empresas.ultimo_nsu_acbr`` imediatamente antes do
    POST (0 no primeiro acesso), grava o novo ``ultNSU`` devolvido e, se a ACBr
    informar que o cursor do banco está atrasado, sincroniza o valor exigido e
    repete a consulta uma vez. A consulta à ACBr pode falhar (credenciais,
    certificado, tempo de espera): nesse caso a rota responde 200 com
    ``sucesso: false`` e ``erro`` preenchido, preservando o NSU local para o
    front exibir as duas informações na tela.
    """
    cnpj_limpo = re.sub(r"\D", "", str(cnpj or ""))
    if len(cnpj_limpo) != 14:
        raise HTTPException(status_code=400, detail="Informe um CNPJ com 14 dígitos.")

    empresa = db.query(Empresa).filter(Empresa.cnpj == cnpj_limpo).first()
    if not empresa:
        raise HTTPException(status_code=404, detail="CNPJ não encontrado.")
    # Admin gerencia todas as lojas (suporte); a conta comum, só as próprias.
    if not _pode_gerenciar_loja(usuario, empresa):
        raise HTTPException(
            status_code=403, detail="Esta loja não pertence à sua conta."
        )

    nsu_local_anterior = _normalizar_nsu_acbr(empresa.ultimo_nsu_acbr) or "0"
    nsu_local = nsu_local_anterior
    resposta = {
        "sucesso": False,
        "cnpj": cnpj_limpo,
        "ultimo_nsu_local": nsu_local,
        "ultimo_nsu_anterior": nsu_local_anterior,
        "ultimo_nsu_acbr": None,
        "pendentes": None,
        "mensagem": f"Seu último NSU: {nsu_local}",
        "erro": None,
        "certificado": certificados.certificado_json(empresa),
        "ultima_automacao": _ULTIMAS_AUTOMACOES_ACBR.get(cnpj_limpo),
        "consultado_em": datetime.now(timezone.utc).isoformat(),
    }

    try:
        # A consulta parte do cursor persistido: `consultar_ultimo_nsu` lê
        # `empresas.ultimo_nsu_acbr` imediatamente antes da ACBr e, se a API
        # indicar um cursor mais novo, sincroniza o banco e repete o POST.
        nsu_acbr = _cliente_acbr().consultar_ultimo_nsu(cnpj_limpo)
    except acbr_client.ErroAcbr as erro:
        logger.error(
            "ACBr status %s: falha ao consultar a fila (HTTP %s): %s",
            cnpj_limpo,
            erro.status,
            erro,
        )
        resposta["erro"] = str(erro)
        resposta["acbr_status"] = erro.status
        resposta["mensagem"] = (
            f"Seu último NSU: {nsu_local} | Não foi possível consultar a ACBr."
        )
        return resposta
    except ValueError as erro:
        logger.error("ACBr status %s: resposta inválida: %s", cnpj_limpo, erro)
        resposta["erro"] = str(erro)
        resposta["mensagem"] = (
            f"Seu último NSU: {nsu_local} | Não foi possível consultar a ACBr."
        )
        return resposta

    pendentes = max(0, _nsu_acbr_int(nsu_acbr) - _nsu_acbr_int(nsu_local_anterior))
    if nsu_acbr and nsu_acbr != nsu_local:
        # A ACBr devolveu um cursor novo. Persistir aqui mantém a próxima
        # consulta/download alinhada ao que a API espera receber em `dist_nsu`.
        empresa.ultimo_nsu_acbr = nsu_acbr
        db.commit()
        db.refresh(empresa)
        nsu_local = nsu_acbr
    resposta.update(
        {
            "sucesso": True,
            "ultimo_nsu_local": nsu_local,
            "ultimo_nsu_anterior": nsu_local_anterior,
            "ultimo_nsu_acbr": nsu_acbr,
            "pendentes": pendentes,
            "mensagem": (
                f"Seu último NSU: {nsu_local} | "
                f"Último NSU na ACBr: {nsu_acbr}"
            ),
        }
    )
    logger.info(
        "ACBr status %s: NSU local %s, NSU ACBr %s, pendentes %s.",
        cnpj_limpo,
        nsu_local,
        nsu_acbr,
        pendentes,
    )
    return resposta


def _baixar_e_importar_acbr_em_background(cnpj: str, ambiente: str = "") -> dict:
    """Baixa os XMLs da loja e importa o resultado sem exigir novo clique.

    Executado como ``BackgroundTask`` depois que o certificado é aceito pela ACBr.
    Usa uma sessão própria porque a sessão da requisição já foi fechada quando a
    tarefa roda. O download segue a mesma regra da rota manual (cursor
    ``ultimo_nsu_acbr``; loja nova começa em 0) e o período é aplicado apenas
    localmente. A importação é incremental: só a pasta desta loja, sem limpar as
    notas existentes.
    """
    cnpj_limpo = re.sub(r"\D", "", str(cnpj or ""))
    if len(cnpj_limpo) != 14:
        logger.error("ACBr automático: CNPJ inválido %r; tarefa ignorada.", cnpj)
        return {"sucesso": False, "motivo": "CNPJ inválido"}

    db = SessionLocal()
    try:
        empresa = db.query(Empresa).filter(Empresa.cnpj == cnpj_limpo).first()
        if empresa is None:
            logger.error(
                "ACBr automático: empresa %s não encontrada; download ignorado.",
                cnpj_limpo,
            )
            return _registrar_automacao_acbr(
                cnpj_limpo,
                {"sucesso": False, "motivo": "Empresa não encontrada"},
            )
        if empresa.usuario_id is None:
            logger.error(
                "ACBr automático: empresa %s sem usuário vinculado; download ignorado.",
                cnpj_limpo,
            )
            return _registrar_automacao_acbr(
                cnpj_limpo,
                {"sucesso": False, "motivo": "Empresa sem usuário"},
            )

        nsu_salvo = _normalizar_nsu_acbr(empresa.ultimo_nsu_acbr) or "0"
        primeiro_download = _nsu_acbr_int(nsu_salvo) == 0
        hoje = date.today()
        if primeiro_download:
            # Mesma janela configurável da rota manual (padrão 7 dias).
            data_inicial = hoje - timedelta(days=DIAS_PRIMEIRO_DOWNLOAD_ACBR)
            data_final = hoje + timedelta(days=1)
        else:
            # A ACBr não recebe datas: o NSU já delimita o lote. O período
            # local fica aberto para não descartar um XML novo com emissão
            # mais antiga.
            data_inicial = date(2000, 1, 1)
            data_final = hoje + timedelta(days=1)

        requisicao = AcbrBaixarXmlsRequest(
            cnpj=cnpj_limpo,
            data_inicial=data_inicial,
            data_final=data_final,
            ambiente=ambiente or None,
            limite_documentos=500,
            salvar_arquivos=True,
        )
        # A rota é uma função normal; chamá-la aqui reaproveita TODA a regra de
        # NSU, paginação, filtro de período e persistência. O objeto `usuario`
        # só precisa do ID para a checagem de dono da loja.
        resultado = baixar_xmls_da_acbr(
            requisicao,
            usuario=SimpleNamespace(id=empresa.usuario_id),
            db=db,
        )
        logger.info(
            "ACBr automático %s: %s XML(s) baixado(s), NSU %s.",
            cnpj_limpo,
            resultado.get("documentos_baixados", 0),
            resultado.get("ultimo_nsu_acbr"),
        )
        # A rota de download transforma falhas da ACBr em `avisos` (status
        # "erro", fila vazia, resumo sem XML etc.). Registrar cada um deixa o
        # motivo real no log mesmo quando nenhum documento é baixado.
        for aviso in resultado.get("avisos") or []:
            logger.warning("ACBr automático %s: %s", cnpj_limpo, aviso)
        if not resultado.get("documentos_baixados"):
            logger.warning(
                "ACBr automático %s: nenhum XML baixado (encontrados=%s, ignorados=%s).",
                cnpj_limpo,
                resultado.get("documentos_encontrados", 0),
                resultado.get("documentos_ignorados", 0),
            )

        # Import local para o banco. `limpar=False` é essencial: o script de
        # linha de comando apaga as notas antigas, mas a automação não pode
        # destruir o histórico das outras lojas.
        from import_xmls import importar_com_ean_e_ncm

        resumo = importar_com_ean_e_ncm(
            pastas=[_pasta_xmls_acbr(cnpj_limpo)],
            limpar=False,
            cnpj_padrao=cnpj_limpo,
            apenas_novos=True,
            silencioso=False,
        )
        logger.info(
            "ACBr automático %s: importação concluída (%s nota(s), %s já existente(s), %s erro(s)).",
            cnpj_limpo,
            resumo.get("importadas"),
            resumo.get("ignoradas"),
            resumo.get("erros"),
        )
        resumo_automacao = _resumo_download_automacao(resultado)
        resumo_automacao["sucesso"] = True
        resumo_automacao["importacao"] = resumo
        _registrar_automacao_acbr(cnpj_limpo, resumo_automacao)
        return {
            "sucesso": True,
            "download": resultado,
            "importacao": resumo,
        }
    except HTTPException as erro:
        logger.error(
            "ACBr automático %s: download falhou (HTTP %s): %s",
            cnpj_limpo,
            erro.status_code,
            erro.detail,
        )
        return _registrar_automacao_acbr(
            cnpj_limpo,
            {
                "sucesso": False,
                "motivo": str(erro.detail),
                "status_http": erro.status_code,
            },
        )
    except Exception as erro:  # noqa: BLE001 — background não pode derrubar o processo
        logger.exception("ACBr automático %s: falha inesperada: %s", cnpj_limpo, erro)
        return _registrar_automacao_acbr(
            cnpj_limpo,
            {"sucesso": False, "motivo": str(erro)},
        )
    finally:
        db.close()


# Motor de alertas (WhatsApp/e-mail): configuração do cliente + simulador da mensagem
# ──────────────────────────────────────────────────────────────────────────────
class AlertaConfigRequest(BaseModel):
    """Corpo de POST /api/alertas/configurar (e dos overrides da simulação).

    `lojas` são os CNPJs que entram no alerta; lista vazia = todas as lojas da conta.
    `canal` aceita ``whatsapp``, ``email`` ou ``ambos``; `email` é o destino do canal
    de e-mail e `email_secundario` é a cópia opcional (Cc). A validação exige somente
    os dados do canal escolhido.
    """
    ativo: bool = True
    telefone: str = ""
    email: str = ""
    email_secundario: str = ""
    canal: str = "whatsapp"
    variacao_minima: float = motor_alertas.VARIACAO_MINIMA_PADRAO
    janela_dias: int = motor_alertas.JANELA_PADRAO_DIAS
    incluir_aumentos: bool = True
    incluir_divergencias: bool = True
    lojas: List[str] = []
    # Relatórios por e-mail: quais tipos o agendador envia (padrão: todos).
    alerta_diario: bool = True
    alerta_semanal: bool = True
    alerta_mensal: bool = True


class AlertaEmailRequest(BaseModel):
    """Corpo de POST /api/alertas/enviar-email.

    A lista contém os textos já montados pelo simulador (uma entrada por parte da
    mensagem). O destinatário NÃO vem do corpo: é sempre o e-mail salvo na
    configuração da conta logada.
    """

    mensagens: List[str] = Field(default_factory=list)


ASSUNTO_EMAIL_ALERTA = "Alertas de preço — Inteligência de Custos NF-e"
ERRO_EMAIL_ALERTA_VAZIO = "Preencha o e-mail de alertas na tela de configuração"
ERRO_EMAIL_ALERTA_SEM_MENSAGENS = (
    "Nenhuma mensagem simulada para enviar. Gere as mensagens antes do envio."
)
ERRO_SMTP_AUTENTICACAO = (
    "Falha na autenticação SMTP: o servidor recusou o usuário ou a senha. "
    "Confira SMTP_USER e a Senha de App no .env."
)
ERRO_SMTP_ENVIO = "Não foi possível enviar o e-mail."
ERRO_SMTP_CONFIGURACAO = (
    "O envio de e-mail não está configurado corretamente no servidor. "
    "Avise o administrador."
)


def _falha_smtp_http(
    mensagem: str, erro: BaseException, destino: str, status_code: int = 502
) -> JSONResponse:
    """Monta a resposta 502 com o erro SMTP real e sem expor a senha.

    O Streamlit exibe o campo ``detail`` de respostas não-200. Incluir a classe, o
    código e a resposta do Gmail (ex.: ``SMTPServerDisconnected``, 535, timeout, TLS)
    permite diagnosticar a falha sem consultar o terminal da API. ``detalhe_erro_smtp``
    mascara a Senha de App antes de qualquer log ou resposta HTTP.
    """
    detalhe = detalhe_erro_smtp(erro)
    logger.warning(
        "Falha SMTP ao enviar alerta para %s (%s): %s",
        destino or "<sem destino>",
        type(erro).__name__,
        detalhe,
    )
    return JSONResponse(
        status_code=status_code,
        content={"detail": f"{mensagem} Erro SMTP: {detalhe}"},
    )



class ConfiguracaoSistemaRequest(BaseModel):
    """Corpo de POST /api/admin/configuracoes (token da API do WhatsApp)."""
    whatsapp_token: str = ""


def _configuracao_alerta(db: Session, usuario: Usuario):
    """Configuração de alerta da conta (None quando o cliente ainda não salvou)."""
    return (
        db.query(ConfiguracaoAlerta)
        .filter(ConfiguracaoAlerta.usuario_id == usuario.id)
        .first()
    )


def _cnpjs_da_configuracao(valor) -> list:
    """CNPJs gravados em `configuracoes_alerta.lojas` (CSV) -> lista de 14 dígitos."""
    return [
        re.sub(r"\D", "", parte)
        for parte in str(valor or "").split(",")
        if re.sub(r"\D", "", parte)
    ]


def _empresas_do_usuario(db: Session, usuario: Usuario) -> list:
    """Lojas da conta, ordenadas pelo nome (a mesma lista da tela "Minhas Lojas")."""
    return (
        db.query(Empresa)
        .filter(Empresa.usuario_id == usuario.id)
        .order_by(Empresa.nome_loja)
        .all()
    )


def _valor_configuracao_sistema(db: Session, chave: str) -> str:
    """Valor de uma configuração global ("" quando não existe)."""
    registro = (
        db.query(ConfiguracaoSistema).filter(ConfiguracaoSistema.chave == chave).first()
    )
    return str(registro.valor or "") if registro else ""


def _configuracao_alerta_json(db: Session, usuario: Usuario, config=None) -> dict:
    """Configuração do alerta + as lojas que o cliente pode escolher.

    Nunca devolve o token do WhatsApp: o cliente só sabe se o envio está habilitado
    (`whatsapp_configurado`), porque o token é global e do administrador.
    """
    config = config if config is not None else _configuracao_alerta(db, usuario)
    empresas = _empresas_do_usuario(db, usuario)
    selecionadas = _cnpjs_da_configuracao(config.lojas) if config else []
    if config is None:
        # Padrão da conta NOVA (criada na confirmação do e-mail): alerta ligado, canal
        # e-mail e destinatário o próprio e-mail do cadastro. A tela só pede o e-mail
        # secundário (opcional) — nada de variação mínima/janela/lojas.
        dados = {
            "ativo": True,
            "telefone": "",
            "email": str(usuario.email or ""),
            "email_secundario": "",
            "canal": "email",
            "variacao_minima": motor_alertas.VARIACAO_MINIMA_PADRAO,
            "janela_dias": motor_alertas.JANELA_PADRAO_DIAS,
            "incluir_aumentos": True,
            "incluir_divergencias": True,
            "lojas": [],
            "atualizado_em": None,
            "alerta_diario": True,
            "alerta_semanal": True,
            "alerta_mensal": True,
        }
    else:
        canal, _erro_canal = motor_alertas.normalizar_canal(
            getattr(config, "canal", None)
        )
        dados = {
            "ativo": bool(config.ativo),
            "telefone": str(config.telefone or ""),
            "email": str(config.email or ""),
            "email_secundario": str(config.email_secundario or ""),
            "canal": canal or "whatsapp",
            "variacao_minima": float(
                config.variacao_minima or motor_alertas.VARIACAO_MINIMA_PADRAO
            ),
            "janela_dias": int(config.janela_dias or motor_alertas.JANELA_PADRAO_DIAS),
            "incluir_aumentos": bool(config.incluir_aumentos),
            "incluir_divergencias": bool(config.incluir_divergencias),
            "lojas": selecionadas,
            "atualizado_em": config.atualizado_em,
            "alerta_diario": bool(getattr(config, "alerta_diario", True)),
            "alerta_semanal": bool(getattr(config, "alerta_semanal", True)),
            "alerta_mensal": bool(getattr(config, "alerta_mensal", True)),
        }
    return {
        "configuracao": dados,
        "telefone_exibicao": motor_alertas.telefone_para_exibicao(dados["telefone"]),
        "canais": list(motor_alertas.canais_de_envio(dados["canal"])),
        "rotulos_canais": dict(motor_alertas.ROTULOS_CANAIS),
        "lojas": [
            {
                "cnpj": re.sub(r"\D", "", str(empresa.cnpj or "")),
                "nome_loja": empresa.nome_loja,
            }
            for empresa in empresas
        ],
        # Sem seleção explícita o alerta cobre a conta inteira ("todas as lojas").
        "lojas_selecionadas": selecionadas,
        "todas_as_lojas": not selecionadas,
        "whatsapp_configurado": bool(
            _valor_configuracao_sistema(db, motor_alertas.CHAVE_TOKEN_WHATSAPP)
        ),
        "janelas_validas": list(motor_alertas.JANELAS_VALIDAS),
        "limiar_divergencia_pct": motor_alertas.LIMIAR_DIVERGENCIA_PCT,
    }


@app.get("/api/alertas/configurar")
def ler_configuracao_alerta(
    usuario: Usuario = Depends(auth.usuario_atual), db: Session = Depends(get_db)
):
    """Preferências de alerta da conta logada (aba "🔔 Configurar Alertas")."""
    return _configuracao_alerta_json(db, usuario)


@app.post("/api/alertas/configurar")
def salvar_configuracao_alerta(
    dados: AlertaConfigRequest,
    usuario: Usuario = Depends(auth.usuario_atual),
    db: Session = Depends(get_db),
):
    """Grava as preferências de alerta do cliente (uma linha por conta).

    Regras (backend/alertas.py): o destino é validado conforme o canal (WhatsApp exige
    telefone, E-mail exige e-mail e Ambos exige os dois), variação mínima entre 0,5% e
    100% e janela de 7/15/30/60/90 dias. As lojas escolhidas precisam ser da própria
    conta — CNPJ de outra conta responde 403.
    """
    validado, erro = motor_alertas.validar_configuracao(
        dados.ativo,
        dados.telefone,
        dados.variacao_minima,
        dados.janela_dias,
        email=dados.email,
        canal=dados.canal,
        email_secundario=dados.email_secundario,
    )
    if erro:
        raise HTTPException(status_code=400, detail=erro)

    cnpjs_conta = {
        re.sub(r"\D", "", str(empresa.cnpj or ""))
        for empresa in _empresas_do_usuario(db, usuario)
    }
    lojas = []
    for cnpj in dados.lojas or []:
        digitos = re.sub(r"\D", "", str(cnpj or ""))
        if not digitos:
            continue
        if digitos not in cnpjs_conta:
            raise HTTPException(
                status_code=403, detail="A loja informada não pertence à sua conta."
            )
        if digitos not in lojas:
            lojas.append(digitos)

    config = _configuracao_alerta(db, usuario)
    if config is None:
        config = ConfiguracaoAlerta(usuario_id=usuario.id)
        db.add(config)
    config.ativo = validado["ativo"]
    config.telefone = validado["telefone"] or None
    config.email = validado["email"] or None
    config.email_secundario = validado["email_secundario"] or None
    config.canal = validado["canal"]
    config.variacao_minima = validado["variacao_minima"]
    config.janela_dias = validado["janela_dias"]
    config.incluir_aumentos = bool(dados.incluir_aumentos)
    config.incluir_divergencias = bool(dados.incluir_divergencias)
    config.alerta_diario = bool(dados.alerta_diario)
    config.alerta_semanal = bool(dados.alerta_semanal)
    config.alerta_mensal = bool(dados.alerta_mensal)
    config.lojas = ",".join(lojas) or None
    config.atualizado_em = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.commit()
    db.refresh(config)

    corpo = _configuracao_alerta_json(db, usuario, config)
    corpo["sucesso"] = True
    corpo["mensagem"] = "Preferências salvas com sucesso!"
    return corpo


@app.get("/api/resumo-precos")
def obter_resumo_precos(loja: Optional[str] = Query(None), dias: int = Query(90), db: Session = Depends(get_db)):
    query = db.query(ItemNota.id, ItemNota.ean, ItemNota.descricao_produto.label("descricao"), ItemNota.valor_unitario, ItemNota.quantidade, ItemNota.valor_total, ItemNota.ncm, ItemNota.marca, ItemNota.unidade_compra, NotaFiscal.data_emissao, Empresa.nome_loja, Empresa.cnpj.label("cnpj"), Fornecedor.nome.label("fornecedor")).join(NotaFiscal, ItemNota.nota_id == NotaFiscal.id).join(Empresa, NotaFiscal.empresa_id == Empresa.id).join(Fornecedor, ItemNota.fornecedor_id == Fornecedor.id)
    if loja and str(loja).strip().lower() not in LOJAS_TODAS_ALIASES: query = query.filter(Empresa.nome_loja.ilike(loja))
    resultados = query.all()
    if not resultados: return []
    # `cnpj` acompanha cada item para o front separar as lojas da conta mesmo quando
    # duas empresas usam o mesmo nome fantasia. `marca` é a marca inferida do item:
    # o front usa na chave inteligente (Produto + Marca).
    return [{"id": r.id, "ean": r.ean, "descricao": r.descricao, "valor_unitario": r.valor_unitario, "quantidade": r.quantidade, "valor_total": r.valor_total, "ncm": r.ncm if r.ncm else "00000000", "marca": r.marca, "unidade_compra": r.unidade_compra, "data_emissao": str(r.data_emissao), "nome_loja": r.nome_loja, "cnpj": r.cnpj, "fornecedor": r.fornecedor} for r in resultados]


def _empresas_do_alerta(db: Session, usuario: Usuario, cnpjs=None) -> list:
    """Lojas da conta que entram no alerta (todas quando `cnpjs` vem vazio).

    O recorte é feito por `Empresa.id`: é ele que filtra a consulta SQL das notas,
    evitando carregar o histórico de outras contas e permitindo que o SQLite use o
    índice de `notas_fiscais.empresa_id`.
    """
    empresas = _empresas_do_usuario(db, usuario)
    selecionados = {
        re.sub(r"\D", "", str(cnpj or "")) for cnpj in (cnpjs or [])
    }
    selecionados.discard("")
    if not selecionados:
        return empresas
    return [
        empresa
        for empresa in empresas
        if re.sub(r"\D", "", str(empresa.cnpj or "")) in selecionados
    ]


def _referencia_alertas(db: Session, empresas) -> Optional[date]:
    """Maior data de compra VÁLIDA das lojas do alerta (a mesma referência da janela).

    Bonificação/doação (CFOP 5910/6910) e itens com valor unitário zero não entram,
    exatamente como em `_carregar_itens_relatorio`. Assim a consulta seguinte pode
    filtrar `data_emissao` no SQL em vez de trazer todo o histórico para escolher a
    janela em Python.
    """
    empresa_ids = [empresa.id for empresa in empresas if empresa.id is not None]
    if not empresa_ids:
        return None
    return (
        db.query(func.max(NotaFiscal.data_emissao))
        .join(ItemNota, ItemNota.nota_id == NotaFiscal.id)
        .filter(
            NotaFiscal.empresa_id.in_(empresa_ids),
            ItemNota.valor_unitario > 0,
            or_(
                ItemNota.cfop.is_(None),
                func.trim(ItemNota.cfop).notin_(CFOPS_BONIFICACAO),
            ),
        )
        .scalar()
    )


def _itens_do_usuario(db: Session, usuario: Usuario, cnpjs=None, data_inicio=None, data_fim=None) -> list:
    """Itens das lojas da conta, já com as regras de integridade dos relatórios.

    `cnpjs` (opcional) recorta ainda mais; CNPJ que não é da conta é ignorado — nenhuma
    consulta de alerta vê dados de outra empresa. `data_inicio`/`data_fim` limitam a
    janela no próprio SQL; o simulador de alertas usa a data da última compra válida
    para trazer só o período configurado (em vez do histórico inteiro).
    """
    empresas = _empresas_do_alerta(db, usuario, cnpjs)
    empresa_ids = [empresa.id for empresa in empresas if empresa.id is not None]
    if not empresa_ids:
        return []
    # A auditoria continua sendo calculada sobre todas as lojas da conta (o mesmo
    # universo da aba Auditoria); só os itens que entram no alerta são recortados.
    empresa_ids_conta = [
        empresa.id
        for empresa in _empresas_do_usuario(db, usuario)
        if empresa.id is not None
    ]
    return _carregar_itens_relatorio(
        db,
        data_inicio=data_inicio,
        data_fim=data_fim,
        empresa_ids=empresa_ids,
        auditoria_empresa_ids=empresa_ids_conta,
        ordem_descendente=True,
    )


TIPOS_RELATORIO_ALERTA = relatorios_alerta.TIPOS


def _hoje_brasilia() -> date:
    """Data de hoje no fuso de Brasília (o servidor pode estar em UTC)."""
    return datetime.now(ZoneInfo("America/Sao_Paulo")).date()


def _periodo_do_relatorio(tipo: str, hoje: date) -> dict:
    """Janela do relatório: hoje, semana anterior (seg-dom) ou mês anterior fechado."""
    if tipo == "semanal":
        # `weekday()` = 0 na segunda: recua até a segunda da semana ANTERIOR.
        inicio = hoje - timedelta(days=hoje.weekday() + 7)
        fim = inicio + timedelta(days=6)
    elif tipo == "mensal":
        fim = hoje.replace(day=1) - timedelta(days=1)
        inicio = fim.replace(day=1)
    else:
        inicio = fim = hoje
    return {"inicio": inicio, "fim": fim, "referencia": fim}


def _lote_do_relatorio(notas, config, tipo: str, periodo: dict) -> dict:
    """Notas do relatório + as recebidas com atraso (por ORDEM DE IMPORTAÇÃO).

    O cursor (`ultima_nota_*_id`) é comparado com `notas_fiscais.id`, não com a data
    de emissão: assim uma nota emitida ontem e importada hoje entra no relatório de
    hoje. Nota com data anterior à janela mas importada depois do cursor é marcada
    como "recebida com atraso" e entra no relatório seguinte.

    Primeira execução (cursor 0) NÃO despeja o histórico inteiro: o diário considera
    só o último dia com notas; semanal/mensal já são limitados pela janela.
    """
    cursor = {
        "diario": getattr(config, "ultima_nota_alerta_id", 0),
        "semanal": getattr(config, "ultima_nota_semanal_id", 0),
        "mensal": getattr(config, "ultima_nota_mensal_id", 0),
    }.get(tipo) or 0
    inicio, fim = periodo["inicio"], periodo["fim"]
    lote, atrasadas = set(), set()
    if tipo == "diario":
        if cursor > 0:
            lote = {nota.id for nota in notas if int(nota.id) > int(cursor)}
        else:
            datas = [
                nota.data_emissao
                for nota in notas
                if nota.data_emissao is not None and nota.data_emissao <= fim
            ]
            if datas:
                ultimo_dia = max(datas)
                lote = {
                    nota.id for nota in notas if nota.data_emissao == ultimo_dia
                }
    else:
        lote = {
            nota.id
            for nota in notas
            if nota.data_emissao is not None and inicio <= nota.data_emissao <= fim
        }
        # Só existe "atrasada" quando já houve um envio anterior (cursor > 0). Na
        # primeira execução o relatório cobre apenas a janela do período — nunca o
        # histórico inteiro importado antes de o alerta existir.
        if cursor > 0:
            for nota in notas:
                if (
                    int(nota.id) > int(cursor)
                    and nota.data_emissao is not None
                    and nota.data_emissao < inicio
                ):
                    lote.add(nota.id)
                    atrasadas.add(nota.id)
    return {"cursor": cursor, "notas_lote": lote, "atrasadas": atrasadas}


AVISOS_SEM_NOTAS = {
    "diario": "Nenhuma nota nova desde o último relatório diário.",
    "semanal": "Nenhuma nota da semana anterior (nem nota atrasada) para relatar.",
    "mensal": "Nenhuma nota do mês anterior (nem nota atrasada) para relatar.",
}


def _montar_relatorio_alerta(
    db: Session,
    usuario: Usuario,
    tipo: str,
    cnpjs=None,
    hoje: Optional[date] = None,
) -> dict:
    """Relatório (diário/semanal/mensal) das lojas da conta, pronto para prévia/envio.

    Usa a MESMA base dos relatórios (`_carregar_itens_relatorio`): bonificação,
    doação e descrições pendentes na Auditoria ficam de fora. A janela de leitura vai
    até 400 dias antes do lote para que a "compra imediatamente anterior" exista mesmo
    quando o produto ficou meses sem ser comprado, e para o consumo de 90 dias.
    """
    tipo = str(tipo or "diario").lower()
    if tipo not in TIPOS_RELATORIO_ALERTA:
        raise HTTPException(
            status_code=400,
            detail="Tipo de relatório inválido. Use 'diario', 'semanal' ou 'mensal'.",
        )
    dados = _itens_do_relatorio_alerta(db, usuario, tipo, cnpjs, hoje)
    relatorio = relatorios_alerta.montar_relatorio(dados["itens"], dados["contexto"])
    relatorio["aviso"] = dados["aviso"]
    relatorio["texto_email"] = relatorios_alerta.montar_texto(relatorio)
    return relatorio

def _itens_do_relatorio_alerta(
    db: Session,
    usuario: Usuario,
    tipo: str,
    cnpjs=None,
    hoje: Optional[date] = None,
    ignorar_lote: bool = False,
) -> dict:
    """Contexto, itens e aviso do relatório — base única da prévia, envio e Excel.

    Separado de `_montar_relatorio_alerta` porque a memória de cálculo (Excel) precisa
    dos MESMOS itens, mas não do texto do e-mail. Devolve ``{"contexto", "itens",
    "aviso"}``: lista vazia com `aviso` preenchido significa "nada a relatar".

    ``ignorar_lote=True`` carrega os itens da janela mesmo quando não há nota nova para
    listar (ex.: o mês já foi relatado e o cliente só quer o Excel de conferência).
    """
    tipo = str(tipo or "diario").lower()
    if tipo not in TIPOS_RELATORIO_ALERTA:
        raise HTTPException(
            status_code=400,
            detail="Tipo de relatório inválido. Use 'diario', 'semanal' ou 'mensal'.",
        )
    hoje = hoje or _hoje_brasilia()
    periodo = _periodo_do_relatorio(tipo, hoje)
    config = _configuracao_alerta(db, usuario)
    empresas = _empresas_do_alerta(db, usuario, cnpjs or [])
    empresa_ids = [empresa.id for empresa in empresas if empresa.id is not None]
    empresas_conta = [
        empresa.id
        for empresa in _empresas_do_usuario(db, usuario)
        if empresa.id is not None
    ]
    nomes = {
        re.sub(r"\D", "", str(empresa.cnpj or "")): str(empresa.nome_loja or "")
        for empresa in empresas
    }
    contexto = {
        **periodo,
        "tipo": tipo,
        "lojas": nomes,
        "notas_lote": set(),
        "atrasadas": set(),
    }
    if not empresa_ids:
        return {
            "contexto": contexto,
            "itens": [],
            "aviso": (
                "Nenhuma loja vinculada (ou selecionada) nesta conta — nada a relatar."
            ),
        }

    notas = (
        db.query(NotaFiscal.id, NotaFiscal.data_emissao)
        .filter(
            NotaFiscal.empresa_id.in_(empresa_ids),
            NotaFiscal.data_emissao.isnot(None),
        )
        .order_by(NotaFiscal.id)
        .all()
    )
    lote = _lote_do_relatorio(notas, config, tipo, periodo)
    contexto.update({"notas_lote": lote["notas_lote"], "atrasadas": lote["atrasadas"]})
    if not lote["notas_lote"] and not ignorar_lote:
        return {"contexto": contexto, "itens": [], "aviso": AVISOS_SEM_NOTAS[tipo]}

    datas_lote = [
        nota.data_emissao
        for nota in notas
        if nota.id in lote["notas_lote"] and nota.data_emissao is not None
    ]
    data_inicio = min([periodo["inicio"], *datas_lote]) - timedelta(days=400)
    data_fim = max([periodo["referencia"], *datas_lote])
    itens = _carregar_itens_relatorio(
        db,
        data_inicio=data_inicio,
        data_fim=data_fim,
        empresa_ids=empresa_ids,
        auditoria_empresa_ids=empresas_conta,
        ordem_descendente=True,
    )
    return {"contexto": contexto, "itens": itens, "aviso": None}


def _registrar_envio_relatorio(db: Session, usuario: Usuario, config, relatorio) -> None:
    """Grava a cobertura do relatório e avança o cursor — SÓ depois do envio confirmado.

    - ``alertas_notas``: uma linha por nota coberta (por conta e tipo), para auditoria.
    - cursor: o MAIOR id entre o cursor atual e as notas do lote (nunca o máximo da
      conta), para uma nota importada durante o envio não ser pulada.
    - marcadores de data/semana/mês: impedem o agendador de repetir o mesmo relatório.
    """
    tipo = str(relatorio.get("tipo") or "diario")
    notas = [int(nota) for nota in (relatorio.get("notas_lote_ids") or [])]
    agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ja_registradas = set()
    if notas:
        ja_registradas = {
            int(linha[0])
            for linha in db.query(AlertaNota.nota_id)
            .filter(
                AlertaNota.usuario_id == usuario.id,
                AlertaNota.tipo == tipo,
                AlertaNota.nota_id.in_(notas),
            )
            .all()
        }
    for nota_id in notas:
        if nota_id in ja_registradas:
            continue
        db.add(
            AlertaNota(
                usuario_id=usuario.id,
                nota_id=nota_id,
                tipo=tipo,
                criado_em=agora,
            )
        )

    if tipo == "diario":
        cursor_atual = int(config.ultima_nota_alerta_id or 0)
        config.ultima_nota_alerta_id = max([cursor_atual, *notas])
        config.ultimo_envio_diario = _hoje_brasilia().isoformat()
    elif tipo == "semanal":
        hoje = _hoje_brasilia()
        cursor_atual = int(config.ultima_nota_semanal_id or 0)
        config.ultima_nota_semanal_id = max([cursor_atual, *notas])
        config.ultimo_envio_semanal = (
            hoje - timedelta(days=hoje.weekday())
        ).isoformat()
    else:
        cursor_atual = int(config.ultima_nota_mensal_id or 0)
        config.ultima_nota_mensal_id = max([cursor_atual, *notas])
        config.ultimo_envio_mensal = str(relatorio.get("fim") or "")[:7]
    db.commit()


@app.post("/api/alertas/gerar-mensagens")
def gerar_mensagens_alerta(
    dados: Optional[AlertaConfigRequest] = None,
    usuario: Usuario = Depends(auth.usuario_atual),
    db: Session = Depends(get_db),
):
    """SIMULADOR do disparo: monta o texto que seria enviado no canal escolhido.

    Não envia nada e não grava configuração. Lê os itens das lojas da conta (mesmas
    regras de integridade dos relatórios), compara a última com a penúltima compra na
    janela e devolve as mensagens já formatadas (divididas em partes) + o resumo. O
    campo `canal` (whatsapp/email/ambos) é o mesmo respeitado pelo disparo real.

    O corpo é opcional: sem corpo usa a configuração salva; com corpo usa os valores
    enviados (é o "simular antes de salvar" da tela).
    """
    config = _configuracao_alerta(db, usuario)
    valores = {
        "telefone": (config.telefone if config else "") or "",
        "email": (config.email if config else "") or "",
        "email_secundario": (config.email_secundario if config else "") or "",
        "canal": (config.canal if config else None) or "whatsapp",
        "variacao_minima": float(
            (config.variacao_minima if config else None)
            or motor_alertas.VARIACAO_MINIMA_PADRAO
        ),
        "janela_dias": int(
            (config.janela_dias if config else None) or motor_alertas.JANELA_PADRAO_DIAS
        ),
        "incluir_aumentos": bool(config.incluir_aumentos) if config else True,
        "incluir_divergencias": bool(config.incluir_divergencias) if config else True,
        "lojas": _cnpjs_da_configuracao(config.lojas) if config else [],
    }
    if dados is not None:
        valores.update(
            {
                "telefone": dados.telefone or "",
                "email": dados.email or "",
                "email_secundario": dados.email_secundario or "",
                "canal": dados.canal or "whatsapp",
                "variacao_minima": dados.variacao_minima,
                "janela_dias": dados.janela_dias,
                "incluir_aumentos": bool(dados.incluir_aumentos),
                "incluir_divergencias": bool(dados.incluir_divergencias),
                "lojas": dados.lojas or [],
            }
        )
    # O destino não é exigido para simular: só os limites da janela/variação e, se o
    # cliente digitou telefone/e-mail, a validade do que foi digitado.
    validado, erro = motor_alertas.validar_configuracao(
        False,
        valores["telefone"],
        valores["variacao_minima"],
        valores["janela_dias"],
        email=valores["email"],
        canal=valores["canal"],
        email_secundario=valores["email_secundario"],
    )
    if erro:
        raise HTTPException(status_code=400, detail=erro)

    janela = validado["janela_dias"]
    variacao_minima = validado["variacao_minima"]
    telefone = validado["telefone"]
    email = validado["email"]
    canal = validado["canal"]
    canais = list(motor_alertas.canais_de_envio(canal))

    empresas = _empresas_do_usuario(db, usuario)
    nomes = {
        re.sub(r"\D", "", str(empresa.cnpj or "")): empresa.nome_loja
        for empresa in empresas
    }
    lojas = [
        cnpj
        for cnpj in (re.sub(r"\D", "", str(item or "")) for item in valores["lojas"])
        if cnpj in nomes
    ]
    # A janela é resolvida no SQL: primeiro a última compra válida das lojas
    # selecionadas, depois só as notas do período. Antes, todo o histórico era
    # carregado para descobrir a data e a "penúltima" compra em Python.
    empresas_alerta = _empresas_do_alerta(db, usuario, lojas)
    referencia = _referencia_alertas(db, empresas_alerta)
    inicio = referencia - timedelta(days=janela) if referencia else None
    itens = (
        _itens_do_usuario(db, usuario, lojas, data_inicio=inicio, data_fim=referencia)
        if referencia
        else []
    )

    linhas = motor_alertas.linhas_de_variacao(
        itens,
        janela_dias=janela,
        referencia=referencia,
        variacao_minima=variacao_minima,
    )
    rotulo = " + ".join(dict.fromkeys(nomes[cnpj] for cnpj in lojas)) or "Todas as lojas"
    contexto = {
        "lojas_rotulo": rotulo,
        "inicio": inicio,
        "referencia": referencia,
        "variacao_minima": variacao_minima,
    }
    resultado = motor_alertas.montar_mensagens(
        linhas,
        contexto,
        {
            "incluir_aumentos": valores["incluir_aumentos"],
            "incluir_divergencias": valores["incluir_divergencias"],
        },
    )
    return {
        "simulado": True,
        "enviado": False,
        "telefone": telefone,
        "telefone_exibicao": motor_alertas.telefone_para_exibicao(telefone),
        "email": email,
        "email_secundario": validado["email_secundario"],
        "canal": canal,
        "canais": canais,
        "canal_rotulo": motor_alertas.ROTULOS_CANAIS.get(canal, canal),
        "whatsapp_configurado": bool(
            _valor_configuracao_sistema(db, motor_alertas.CHAVE_TOKEN_WHATSAPP)
        ),
        "mensagens": resultado["mensagens"],
        "resumo": {
            **resultado["resumo"],
            "janela_dias": janela,
            "variacao_minima": variacao_minima,
            "referencia": referencia.isoformat() if referencia else None,
            "inicio": inicio.isoformat() if inicio else None,
            "lojas": [nomes[cnpj] for cnpj in lojas],
            "todas_as_lojas": not lojas,
            "limiar_divergencia_pct": motor_alertas.LIMIAR_DIVERGENCIA_PCT,
        },
        "aviso": (
            None
            if itens
            else "Nenhuma nota importada para as lojas selecionadas — nada a alertar."
        ),
    }


@app.post("/api/alertas/enviar-email")
def enviar_alerta_email(
    dados: AlertaEmailRequest,
    usuario: Usuario = Depends(auth.usuario_atual),
    db: Session = Depends(get_db),
):
    """Envia por SMTP as mensagens já simuladas na tela de alertas.

    O destinatário é lido de ``configuracoes_alerta.email`` da conta logada — nunca
    do corpo da requisição. Assim o cliente só recebe no endereço que ele mesmo
    cadastrou e uma conta não consegue disparar e-mail para terceiros por esta rota.
    O ``email_secundario`` salvo vai apenas como cópia (Cc) da mensagem.
    """
    config = _configuracao_alerta(db, usuario)
    destino = str(getattr(config, "email", "") or "").strip() if config else ""
    copia = (
        str(getattr(config, "email_secundario", "") or "").strip() if config else ""
    )
    if not destino:
        raise HTTPException(status_code=400, detail=ERRO_EMAIL_ALERTA_VAZIO)
    if not dados.mensagens:
        raise HTTPException(status_code=400, detail=ERRO_EMAIL_ALERTA_SEM_MENSAGENS)

    # Relê as chaves SMTP_* do `.env` com prioridade sobre o ambiente: uma Senha de
    # App nova passa a valer já no próximo clique, mesmo que a API esteja rodando há
    # horas ou que exista uma SMTP_PASSWORD antiga exportada no terminal/IDE. As
    # demais variáveis do processo (ACBr, DeepSeek, banco) são preservadas.
    try:
        recarregar_configuracao_smtp()
    except Exception as erro:
        logger.exception("Falha ao recarregar o .env antes do envio de e-mail")
        return _falha_smtp_http(
            ERRO_SMTP_CONFIGURACAO, erro, destino, status_code=502
        )

    enviados = 0
    detalhes = []
    for indice, texto in enumerate(dados.mensagens):
        try:
            enviado = send_email(
                destino,
                ASSUNTO_EMAIL_ALERTA,
                str(texto or ""),
                cc_email=copia,
            )
        except smtplib.SMTPAuthenticationError as erro:
            return _falha_smtp_http(
                ERRO_SMTP_AUTENTICACAO, erro, destino, status_code=502
            )
        except smtplib.SMTPRecipientsRefused as erro:
            return _falha_smtp_http(
                ERRO_SMTP_ENVIO, erro, destino, status_code=502
            )
        except (smtplib.SMTPException, OSError) as erro:
            return _falha_smtp_http(
                ERRO_SMTP_ENVIO, erro, destino, status_code=502
            )
        except ValueError as erro:
            # Credenciais SMTP ausentes, porta/timeout inválidos ou destinatário vazio:
            # erro de servidor, não do cliente. O detalhe não expõe valores secretos.
            return _falha_smtp_http(
                ERRO_SMTP_CONFIGURACAO, erro, destino, status_code=502
            )
        except Exception as erro:
            logger.exception(
                "Erro inesperado no envio de e-mail para %s", destino
            )
            return _falha_smtp_http(
                "Falha inesperada no envio do e-mail.",
                erro,
                destino,
                status_code=502,
            )

        if enviado:
            enviados += 1
        else:
            detalhes.append(
                {"indice": indice, "erro": "O servidor SMTP não confirmou o envio."}
            )

    logger.info(
        "Alertas por e-mail: %s mensagem(ns) enviada(s) para %s (Cc: %s), %s falha(s).",
        enviados,
        destino,
        copia or "sem cópia",
        len(detalhes),
    )
    return {
        "enviados": enviados,
        "falhas": len(detalhes),
        "detalhes": detalhes,
    }


class RelatorioAlertaRequest(BaseModel):
    """Corpo de /api/alertas/relatorio/previa e /api/alertas/relatorio/enviar.

    `tipo` escolhe diário/semanal/mensal e `lojas` são CNPJs da conta (lista vazia =
    todas as lojas). Nenhum destinatário vem do corpo: o e-mail é sempre o salvo em
    `configuracoes_alerta` da conta logada.
    """

    tipo: str = "diario"
    lojas: List[str] = []


def _tipo_e_lojas_do_relatorio(dados) -> tuple:
    """(tipo normalizado, CNPJs) do corpo opcional da prévia/envio."""
    tipo = str((dados.tipo if dados else "") or "diario").strip().lower()
    lojas = [str(cnpj) for cnpj in ((dados.lojas if dados else []) or [])]
    return tipo, lojas


@app.post("/api/alertas/relatorio/previa")
def previa_relatorio_alerta(
    dados: Optional[RelatorioAlertaRequest] = None,
    usuario: Usuario = Depends(auth.usuario_atual),
    db: Session = Depends(get_db),
):
    """Prévia do relatório que o agendador enviaria (não envia e não grava nada).

    O lote é o mesmo do envio real: notas novas desde o cursor (diário), notas da
    semana anterior (semanal) ou do mês anterior fechado (mensal), sempre com as
    atrasadas que ainda não foram relatadas. A prévia serve para o cliente conferir
    números e textos antes de ligar o envio automático.
    """
    tipo, lojas = _tipo_e_lojas_do_relatorio(dados)
    return _montar_relatorio_alerta(db, usuario, tipo, lojas)


@app.post("/api/alertas/relatorio/enviar")
def enviar_relatorio_alerta(
    dados: Optional[RelatorioAlertaRequest] = None,
    usuario: Usuario = Depends(auth.usuario_atual),
    db: Session = Depends(get_db),
):
    """Envia o relatório por SMTP e marca as notas cobertas (cursor só após confirmar).

    Uma falha de SMTP devolve 502 e NÃO avança o cursor: as mesmas notas continuam no
    próximo relatório, sem perder compra nenhuma.
    """
    tipo, lojas = _tipo_e_lojas_do_relatorio(dados)
    config = _garantir_configuracao_alerta_padrao(db, usuario)
    destino = str(getattr(config, "email", "") or "").strip()
    copia = str(getattr(config, "email_secundario", "") or "").strip()
    if not destino:
        raise HTTPException(status_code=400, detail=ERRO_EMAIL_ALERTA_VAZIO)

    relatorio = _montar_relatorio_alerta(db, usuario, tipo, lojas)
    resumo = relatorio.get("resumo") or {}
    if not resumo.get("notas_lote"):
        return {
            "enviado": False,
            "motivo": relatorio.get("aviso"),
            "relatorio": relatorio,
        }

    texto = relatorios_alerta.montar_texto(relatorio)
    html_corpo = relatorios_alerta.montar_html(relatorio)
    assunto = f"{relatorio.get('titulo')} — Inteligência de Custos NF-e"
    try:
        recarregar_configuracao_smtp()
    except Exception as erro:
        logger.exception("Falha ao recarregar o .env antes do relatório de alertas")
        return _falha_smtp_http(ERRO_SMTP_CONFIGURACAO, erro, destino, status_code=502)
    try:
        enviado = send_email(
            destino,
            assunto,
            texto,
            cc_email=copia,
            html_body=html_corpo,
        )
    except smtplib.SMTPAuthenticationError as erro:
        return _falha_smtp_http(ERRO_SMTP_AUTENTICACAO, erro, destino, status_code=502)
    except smtplib.SMTPRecipientsRefused as erro:
        return _falha_smtp_http(ERRO_SMTP_ENVIO, erro, destino, status_code=502)
    except (smtplib.SMTPException, OSError) as erro:
        return _falha_smtp_http(ERRO_SMTP_ENVIO, erro, destino, status_code=502)
    except ValueError as erro:
        return _falha_smtp_http(ERRO_SMTP_CONFIGURACAO, erro, destino, status_code=502)
    except Exception as erro:  # noqa: BLE001 - a tela precisa do motivo real
        logger.exception("Erro inesperado no envio do relatório de alertas")
        return _falha_smtp_http(
            "Falha inesperada no envio do e-mail.", erro, destino, status_code=502
        )
    if not enviado:
        return {
            "enviado": False,
            "motivo": "O servidor SMTP não confirmou o envio.",
            "relatorio": relatorio,
        }

    _registrar_envio_relatorio(db, usuario, config, relatorio)
    logger.info(
        "Relatório %s de alertas enviado para %s (%s nota(s), %s variação(ões)).",
        tipo,
        destino,
        resumo.get("notas_lote"),
        resumo.get("variacoes"),
    )
    return {
        "enviado": True,
        "destinatario": destino,
        "copia": copia,
        "relatorio": relatorio,
    }


@app.get("/api/alertas/relatorio/memoria-calculo")
def memoria_calculo_relatorio(
    tipo: str = "mensal",
    usuario: Usuario = Depends(auth.usuario_atual),
    db: Session = Depends(get_db),
):
    """Baixa o Excel que ABRE a conta da inflação do mês fechado (conferência).

    Mesma base da prévia/envio (`_itens_do_relatorio_alerta`): bonificação, CFOP
    5910/6910 e descrições pendentes na Auditoria ficam fora. O arquivo traz a aba
    Resumo (totais), a comparação produto a produto com as fórmulas do Excel, a série
    histórica e a metodologia — o cliente confere o número do e-mail célula por célula.

    `tipo=mensal` (padrão) usa o mês anterior fechado, exatamente o período do
    relatório mensal. Sem compras no mês anterior, devolve 404 com o motivo.
    """
    tipo = str(tipo or "mensal").strip().lower()
    dados = _itens_do_relatorio_alerta(
        db, usuario, tipo, [], hoje=_hoje_brasilia(), ignorar_lote=True
    )
    memoria = inflacao.memoria_calculo(dados["itens"], dados["contexto"]["referencia"])
    if not memoria:
        raise HTTPException(
            status_code=404,
            detail=(
                "Não há compras no mês anterior fechado nesta conta — sem cesta, não "
                "existe conta para abrir."
            ),
        )
    conteudo, nome_arquivo = memoria_calculo.montar_planilha(memoria)
    return StreamingResponse(
        BytesIO(conteudo),
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": f'attachment; filename="{nome_arquivo}"',
            "X-Mes-Referencia": memoria["mes_atual"]["rotulo"],
            "Access-Control-Expose-Headers": (
                "Content-Disposition, X-Mes-Referencia"
            ),
        },
    )


def _executar_agendamento(db: Session, usuario: Usuario, tipo: str) -> dict:
    """Callback do agendador: roda a MESMA rotina do botão "Enviar agora".

    Reaproveita `enviar_relatorio_alerta` (validação, SMTP, cursor) para não haver
    duas versões do envio. Falha de SMTP vira exceção: o agendador registra e tenta de
    novo no próximo ciclo, sem avançar o cursor.
    """
    resultado = enviar_relatorio_alerta(
        dados=RelatorioAlertaRequest(tipo=tipo, lojas=[]),
        usuario=usuario,
        db=db,
    )
    if isinstance(resultado, JSONResponse):
        corpo = (
            resultado.body.decode("utf-8", "replace")
            if isinstance(resultado.body, bytes)
            else str(resultado.body)
        )
        raise RuntimeError(f"HTTP {resultado.status_code}: {corpo[:300]}")
    return {
        "enviado": bool(resultado.get("enviado")),
        "motivo": resultado.get("motivo"),
    }


class AuditRequest(BaseModel):
    chave_identificacao: str

class AuditPendenteRequest(BaseModel):
    """Chaves que devem ficar PENDENTES de validação (ex.: aberração de variação).

    `motivo`: "manual" (enviada pela tela; sai dos relatórios) ou "aberrante"
    (variação brusca marcada automaticamente; continua visível na tabela, fora dos totais).
    """
    chaves: List[str] = []
    motivo: str = "manual"


class IAValidarMarcasRequest(BaseModel):
    """Corpo de /api/auditoria/ia/validar-marcas."""

    palavras: List[str] = Field(
        ...,
        min_length=1,
        max_length=ia_consenso.MAXIMO_PALAVRAS,
        description="Palavras candidatas a marca (fabricante/grife/linha comercial)",
    )


class IAResolverAmbiguidadeRequest(BaseModel):
    """Corpo de /api/auditoria/ia/resolver-ambiguidade."""

    texto1: str = Field(
        ..., min_length=1, max_length=ia_consenso.MAXIMO_DESCRICAO,
        description="Primeira descrição do par ambíguo",
    )
    texto2: str = Field(
        ..., min_length=1, max_length=ia_consenso.MAXIMO_DESCRICAO,
        description="Segunda descrição do par ambíguo",
    )


class IAAuditarBaseRequest(BaseModel):
    """Corpo opcional de /api/ia/auditar-base-completa.

    `limite` existe para o administrador rodar a análise em lotes (custo/tempo
    da IA). Sem ele, todos os grupos com 2+ descrições entram na rodada.
    """

    limite: Optional[int] = Field(
        None,
        ge=1,
        le=500,
        description="Máximo de grupos analisados nesta chamada (vazio = todos)",
    )


class IAAplicarSugestaoRequest(BaseModel):
    """Corpo opcional de /api/ia/aplicar-sugestao/{id}.

    Sem `chave_destino`, a sugestão mantém o comportamento antigo: isola a
    descrição em um grupo NOVO_. Com a chave de um grupo existente, a descrição
    passa a ser uma variação daquele grupo (mesma regra "mover" usada pelo
    seletor de destino da Auditoria).
    """

    chave_destino: Optional[str] = Field(
        None,
        description="Chave de um grupo já existente (vazio = criar grupo novo)",
    )


class IAMarcasProcessarRequest(BaseModel):
    """Lote de descrições únicas para a memória/IA de marcas.

    O front divide a base em lotes para desenhar a barra de progresso sem manter uma
    única requisição gigante aberta. Cada descrição passa por `obter_marca_inteligente`:
    cache primeiro e DeepSeek só quando for inédita.
    """

    descricoes: List[str] = Field(
        ...,
        min_length=1,
        max_length=ia_consenso.MAXIMO_LOTE_MARCAS,
        description="Descrições únicas do lote (máximo definido por MAXIMO_LOTE_MARCAS)",
    )
    usar_ia: bool = Field(
        True,
        description=(
            "False = somente memória (nenhum token gasto). O front desliga após uma "
            "falha de configuração da IA e termina os lotes restantes pelo cache."
        ),
    )


# ──────────────────────────────────────────────────────────────────────────────
# Consenso com IA (DeepSeek) — segunda opinião da Auditoria. Nenhuma decisão da
# IA altera a base: as rotas só devolvem o veredito para o auditor conferir.
# ──────────────────────────────────────────────────────────────────────────────
def validar_marcas_com_ia(lista_palavras):
    """Confirma com a IA se cada palavra é uma marca (fabricante/grife/linha).

    Função pública do backend: delega ao motor em `backend/ia_consenso.py`.
    Levanta `ia_consenso.ErroIA` (502/503) quando a IA não está configurada ou
    não responde; a rota converte em HTTP com mensagem amigável.
    """
    return ia_consenso.validar_marcas_com_ia(lista_palavras)


def resolver_ambiguidade_com_ia(texto1, texto2):
    """Pergunta à IA se duas descrições são o mesmo produto ("iguais"/"diferentes").

    Devolve também `confianca`, `motivo` e `diferencas`. A decisão é sugestão:
    o auditor continua sendo quem grava (ou não) a regra de agrupamento.
    """
    return ia_consenso.resolver_ambiguidade_com_ia(texto1, texto2)


@app.post("/api/auditoria/ia/validar-marcas")
def validar_marcas_ia(
    dados: IAValidarMarcasRequest,
    admin: Usuario = Depends(auth.get_admin_user_2fa),
):
    """Valida uma lista de palavras como marcas usando o DeepSeek."""
    try:
        return validar_marcas_com_ia(dados.palavras)
    except ia_consenso.ErroIA as erro:
        raise HTTPException(status_code=erro.status, detail=str(erro)) from erro


@app.post("/api/auditoria/ia/resolver-ambiguidade")
def resolver_ambiguidade_ia(
    dados: IAResolverAmbiguidadeRequest,
    admin: Usuario = Depends(auth.get_admin_user_2fa),
):
    """Compara duas descrições e devolve o veredito do DeepSeek."""
    try:
        return resolver_ambiguidade_com_ia(dados.texto1, dados.texto2)
    except ia_consenso.ErroIA as erro:
        raise HTTPException(status_code=erro.status, detail=str(erro)) from erro


# ──────────────────────────────────────────────────────────────────────────────
# Auditoria em massa com IA — a IA só SUGERE; a base só muda no "Aplicar".
# ──────────────────────────────────────────────────────────────────────────────
STATUS_SUGESTAO_PENDENTE = "pendente"
STATUS_SUGESTAO_APROVADA = "aprovada"
STATUS_SUGESTAO_IGNORADA = "ignorada"
STATUS_SUGESTAO_VALIDOS = {
    STATUS_SUGESTAO_PENDENTE,
    STATUS_SUGESTAO_APROVADA,
    STATUS_SUGESTAO_IGNORADA,
}


def sugerir_correcao_com_ia(descricoes, descricao_padrao=None):
    """Wrapper público do motor de IA (facilita testar a rota sem rede)."""
    return ia_consenso.sugerir_correcao_grupo(descricoes, descricao_padrao)


def _agora_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sugestao_json(sugestao: SugestaoCorrecaoIA) -> dict:
    """Sugestão pronta para os cards da Auditoria."""
    return {
        "id": sugestao.id,
        "grupo_origem": sugestao.grupo_origem,
        "item_sugerido_sair": sugestao.item_sugerido_sair,
        "motivo_ia": sugestao.motivo_ia or "",
        "status_aprovacao": sugestao.status_aprovacao or STATUS_SUGESTAO_PENDENTE,
        "grupo_destino": sugestao.grupo_destino or "",
        "criado_em": sugestao.criado_em,
        "atualizado_em": sugestao.atualizado_em,
        "aplicado_em": sugestao.aplicado_em,
    }


def _equivalencia_descricao(descricao) -> str:
    """Chave que trata "PRODUTO - 1" e "PRODUTO - 2" como a mesma descrição."""
    return ia_consenso.chave_equivalencia_descricao(descricao)


def _descricoes_equivalentes(descricao, descricoes) -> list:
    """Descrições do grupo que são a MESMA descrição (sem números de controle)."""
    alvo = _equivalencia_descricao(descricao)
    if not alvo:
        return []
    return [
        str(outra).strip()
        for outra in descricoes or []
        if str(outra).strip() and _equivalencia_descricao(outra) == alvo
    ]


def _sugestao_ainda_valida(sugestao: SugestaoCorrecaoIA, grupos) -> bool:
    """True quando a sugestão ainda aponta um item real de um grupo com 2+ produtos.

    Chaves de agrupamento mudam quando uma regra nova (marca, volume, embalagem) entra
    em vigor. Sugestões antigas ficam presas a uma chave que não existe mais; elas não
    devem aparecer nem ser recriadas. O grupo também precisa ter OUTRA descrição
    (equivalente não conta): depois de remover "- 1..4", as quatro variações são uma
    única descrição e não há o que separar.
    """
    grupo = (grupos or {}).get(str(sugestao.grupo_origem or "").strip())
    if not grupo:
        return False
    descricoes = grupo.get("descricoes") or []
    equivalentes = _descricoes_equivalentes(sugestao.item_sugerido_sair, descricoes)
    if not equivalentes:
        return False
    restantes = [
        descricao
        for descricao in descricoes
        if _equivalencia_descricao(descricao)
        != _equivalencia_descricao(sugestao.item_sugerido_sair)
    ]
    return bool(restantes)


def _consolidar_sugestoes_pendentes(db: Session, grupos) -> int:
    """Mantém UMA sugestão pendente por descrição e ignora as desatualizadas.

    A rodada de IA é o momento de limpeza: chaves de grupo antigas não existem mais e
    variações "- 1..4" da mesma descrição já são o mesmo card. Em vez de apagar o
    histórico, a sugestão excedente vira ``ignorada`` (não volta na próxima rodada).
    """
    pendentes = (
        db.query(SugestaoCorrecaoIA)
        .filter(
            or_(
                SugestaoCorrecaoIA.status_aprovacao == STATUS_SUGESTAO_PENDENTE,
                SugestaoCorrecaoIA.status_aprovacao.is_(None),
            )
        )
        .order_by(SugestaoCorrecaoIA.id.desc())
        .all()
    )
    vistas, ignoradas = set(), 0
    agora = _agora_iso()
    for sugestao in pendentes:
        equivalencia = _equivalencia_descricao(sugestao.item_sugerido_sair)
        if (
            not equivalencia
            or equivalencia in vistas
            or not _sugestao_ainda_valida(sugestao, grupos)
        ):
            sugestao.status_aprovacao = STATUS_SUGESTAO_IGNORADA
            sugestao.atualizado_em = agora
            ignoradas += 1
            continue
        vistas.add(equivalencia)
    if ignoradas:
        db.commit()
    return ignoradas


def _auditar_base_completa(db: Session, limite: Optional[int] = None) -> dict:
    """Analisa os grupos com 2+ descrições e grava SÓ sugestões pendentes.

    Nenhum ItemNota, ItemAuditado ou RegraAgrupamento é alterado aqui. Cada
    grupo recebe uma consulta à IA; falhas pontuais são devolvidas em `erros` e
    não impedem as demais sugestões de serem gravadas. Sugestões já ignoradas
    ou já aplicadas pelo auditor não são recriadas.

    Duas travas evitam os cards repetidos:
    - números de controle ("- 1", "- 2") saem antes da consulta e as descrições
      equivalentes viram UMA só entrada para a IA;
    - se já existe sugestão PENDENTE para a mesma descrição (mesmo em uma chave de
      grupo antiga), o registro existente é reaproveitado em vez de criar outro.
    """
    mapa_regras, mapa_descricoes = _mapas_regras_relatorio(db)
    grupos = _grupos_produto_relatorio(db, mapa_regras, mapa_descricoes)
    _consolidar_sugestoes_pendentes(db, grupos)
    elegiveis = {}
    for chave, grupo in grupos.items():
        descricoes = []
        vistos = set()
        for descricao in grupo.get("descricoes") or []:
            texto = str(descricao).strip()
            equivalencia = _equivalencia_descricao(texto)
            if texto and equivalencia and equivalencia not in vistos:
                vistos.add(equivalencia)
                descricoes.append(texto)
        if len(descricoes) >= 2:
            elegiveis[chave] = {**grupo, "descricoes_analise": descricoes}
    chaves = sorted(elegiveis)
    total_elegiveis = len(chaves)
    if limite:
        chaves = chaves[: int(limite)]

    # Índice das sugestões já gravadas por descrição equivalente. O número de
    # controle não entra: "POLENTA - 1" e "POLENTA - 2" são a MESMA descrição.
    sugestoes_existentes = db.query(SugestaoCorrecaoIA).all()
    por_equivalencia = {}
    for sugestao in sugestoes_existentes:
        equivalencia = _equivalencia_descricao(sugestao.item_sugerido_sair)
        por_equivalencia.setdefault(equivalencia, []).append(sugestao)

    criadas = atualizadas = grupos_com_sugestao = 0
    erros = []
    vistas_na_rodada = set()
    for chave in chaves:
        grupo = elegiveis[chave]
        descricoes_grupo = grupo.get("descricoes") or []
        try:
            resposta = sugerir_correcao_com_ia(
                grupo.get("descricoes_analise") or [],
                grupo.get("descricao_padrao"),
            )
        except ia_consenso.ErroIA as erro:
            erros.append({"grupo_origem": chave, "erro": str(erro)})
            continue
        except Exception as erro:  # falha de uma IA não derruba a rodada inteira
            erros.append({"grupo_origem": chave, "erro": f"Falha inesperada: {erro}"})
            continue

        sugestoes = resposta.get("sugestoes") or []
        if not sugestoes:
            continue
        grupos_com_sugestao += 1
        agora = _agora_iso()
        for item in sugestoes:
            descricao = str(item.get("item_sugerido_sair") or "").strip()
            if not descricao:
                continue
            equivalencia = _equivalencia_descricao(descricao)
            equivalentes = _descricoes_equivalentes(descricao, descricoes_grupo)
            # Descrição inventada pela IA (ou que já saiu do grupo) é descartada.
            if not equivalencia or not equivalentes:
                continue
            # A mesma descrição só pode gerar UMA sugestão por rodada, mesmo que
            # apareça em grupos diferentes ou em variações "- 1", "- 2".
            if equivalencia in vistas_na_rodada:
                continue
            vistas_na_rodada.add(equivalencia)
            representante = equivalentes[0]
            motivo = str(item.get("motivo") or "").strip()

            existentes = por_equivalencia.get(equivalencia, [])
            do_mesmo_grupo = [
                sugestao
                for sugestao in existentes
                if str(sugestao.grupo_origem or "").strip() == chave
            ]
            if do_mesmo_grupo:
                finalizadas = [
                    sugestao
                    for sugestao in do_mesmo_grupo
                    if (sugestao.status_aprovacao or STATUS_SUGESTAO_PENDENTE)
                    != STATUS_SUGESTAO_PENDENTE
                ]
                pendentes_mesmo = [
                    sugestao
                    for sugestao in do_mesmo_grupo
                    if (sugestao.status_aprovacao or STATUS_SUGESTAO_PENDENTE)
                    == STATUS_SUGESTAO_PENDENTE
                ]
                if finalizadas:
                    # Já houve decisão para esta descrição neste grupo; uma pendente
                    # antiga (mesma equivalência) não pode voltar a aparecer.
                    for outra in existentes:
                        if (
                            (outra.status_aprovacao or STATUS_SUGESTAO_PENDENTE)
                            == STATUS_SUGESTAO_PENDENTE
                        ):
                            db.delete(outra)
                    continue
                if pendentes_mesmo:
                    escolhida = pendentes_mesmo[0]
                    # A mesma descrição não pode ficar pendente em outra chave
                    # antiga: sobra um único card para o auditor decidir.
                    for outra in existentes:
                        if (
                            outra is not escolhida
                            and (outra.status_aprovacao or STATUS_SUGESTAO_PENDENTE)
                            == STATUS_SUGESTAO_PENDENTE
                        ):
                            db.delete(outra)
                    escolhida.item_sugerido_sair = representante
                    escolhida.motivo_ia = motivo
                    escolhida.atualizado_em = agora
                    atualizadas += 1
                # Sem pendente e sem decisão neste grupo: não há o que fazer aqui.
                continue

            pendentes = [
                sugestao
                for sugestao in existentes
                if (sugestao.status_aprovacao or STATUS_SUGESTAO_PENDENTE)
                == STATUS_SUGESTAO_PENDENTE
            ]
            if pendentes:
                # Reaproveita a sugestão pendente em vez de criar outra. Se a chave
                # antiga não existe mais, ela é curada para o grupo atual.
                escolhida = next(
                    (
                        sugestao
                        for sugestao in pendentes
                        if _sugestao_ainda_valida(sugestao, grupos)
                    ),
                    pendentes[0],
                )
                for outra in pendentes:
                    if outra is not escolhida:
                        db.delete(outra)
                if not _sugestao_ainda_valida(escolhida, grupos):
                    escolhida.grupo_origem = chave
                escolhida.item_sugerido_sair = representante
                escolhida.motivo_ia = motivo
                escolhida.atualizado_em = agora
                por_equivalencia[equivalencia] = [escolhida]
                atualizadas += 1
                continue

            db.add(
                SugestaoCorrecaoIA(
                    grupo_origem=chave,
                    item_sugerido_sair=representante,
                    motivo_ia=motivo,
                    status_aprovacao=STATUS_SUGESTAO_PENDENTE,
                    criado_em=agora,
                    atualizado_em=agora,
                )
            )
            criadas += 1
        db.commit()

    return {
        "status": "sucesso",
        "modelo": ia_consenso.modelo(),
        "grupos_elegiveis": total_elegiveis,
        "grupos_analisados": len(chaves),
        "grupos_com_sugestao": grupos_com_sugestao,
        "sugestoes_criadas": criadas,
        "sugestoes_atualizadas": atualizadas,
        "restantes": max(0, total_elegiveis - len(chaves)),
        "erros": erros,
    }


def _aplicar_sugestao_ia(
    db: Session, sugestao: SugestaoCorrecaoIA, chave_destino: Optional[str] = None
) -> dict:
    """Aplica uma sugestão: cria regra "mover" para um grupo novo ou existente.

    A operação não reescreve a descrição nem apaga compras: apenas registra a
    mesma regra de-para que o botão manual "Mover" usa. Sem `chave_destino`, a
    descrição é isolada em um grupo NOVO_ (comportamento histórico). Com a chave
    de um grupo que já existe, a descrição passa a ser uma variação dele. Antes
    de aplicar, confere se a descrição ainda pertence ao grupo de origem — se os
    dados/regras mudaram, a sugestão fica desatualizada e a rota responde 409.

    A sugestão representa a descrição SEM os números de controle: ao aplicar,
    TODAS as variações equivalentes ("POLENTA - 1", "- 2"... que estavam no grupo)
    são movidas juntas, senão as demais voltariam na próxima rodada.
    """
    item = str(sugestao.item_sugerido_sair or "").strip()
    grupo_origem = str(sugestao.grupo_origem or "").strip()
    if not item or not grupo_origem:
        raise HTTPException(status_code=400, detail="Sugestão de IA inválida.")

    mapa_regras, mapa_descricoes = _mapas_regras_relatorio(db)
    grupos = _grupos_produto_relatorio(db, mapa_regras, mapa_descricoes)
    grupo = grupos.get(grupo_origem)
    if not grupo:
        raise HTTPException(
            status_code=409,
            detail=(
                "A sugestão está desatualizada: o grupo de origem não existe "
                "mais. Rode a auditoria com IA novamente."
            ),
        )
    descricoes_grupo = grupo.get("descricoes") or []
    equivalentes = _descricoes_equivalentes(item, descricoes_grupo)
    if not equivalentes:
        raise HTTPException(
            status_code=409,
            detail=(
                "A sugestão está desatualizada: a descrição não pertence mais a "
                "este grupo. Rode a auditoria com IA novamente."
            ),
        )
    chave_equivalente = _equivalencia_descricao(item)
    restantes = [
        descricao
        for descricao in descricoes_grupo
        if _equivalencia_descricao(descricao) != chave_equivalente
    ]
    if not restantes:
        raise HTTPException(
            status_code=409,
            detail="O grupo de origem já não tem outra descrição para manter.",
        )

    destino_escolhido = str(chave_destino or "").strip()
    if destino_escolhido:
        if destino_escolhido == grupo_origem:
            raise HTTPException(
                status_code=400,
                detail="Escolha um grupo de destino diferente do grupo de origem.",
            )
        if destino_escolhido not in grupos:
            raise HTTPException(
                status_code=409,
                detail=(
                    "O grupo de destino não existe mais. Atualize a tela e "
                    "escolha outro grupo."
                ),
            )
        destino_base = destino_escolhido
    else:
        destino_base = _chave_grupo_novo_relatorio(item)

    destino_final = destino_base
    for descricao in equivalentes:
        chave_origem = f"DESC_{grupo_origem}_{_limpar_texto_relatorio(descricao)}"
        if chave_origem.startswith(f"DESC_{destino_base}_"):
            raise HTTPException(
                status_code=400, detail="Destino inválido para a sugestão."
            )

        regra = (
            db.query(RegraAgrupamento)
            .filter(RegraAgrupamento.chave_origem == chave_origem)
            .first()
        )
        if regra:
            regra.chave_destino = destino_base
            regra.tipo = "mover"
        else:
            db.add(
                RegraAgrupamento(
                    chave_origem=chave_origem,
                    chave_destino=destino_base,
                    tipo="mover",
                )
            )

        destino_final = _aplicar_volume_na_chave(destino_base, descricao)
        chave_destino_aprovada = (
            f"DESC_{destino_final}_{_limpar_texto_relatorio(descricao)}"
        )
        aprovada = (
            db.query(ItemAuditado)
            .filter(ItemAuditado.chave_identificacao == chave_destino_aprovada)
            .first()
        )
        if aprovada:
            aprovada.auditado = True
            aprovada.motivo = MOTIVO_PENDENCIA_MANUAL
        else:
            db.add(
                ItemAuditado(
                    chave_identificacao=chave_destino_aprovada,
                    auditado=True,
                    motivo=MOTIVO_PENDENCIA_MANUAL,
                )
            )

        # A chave antiga deixa de existir no grupo de origem; apagar evita que uma
        # aprovação/pendência antiga volte a casar com a mesma descrição.
        antiga = (
            db.query(ItemAuditado)
            .filter(ItemAuditado.chave_identificacao == chave_origem)
            .first()
        )
        if antiga and antiga.chave_identificacao != chave_destino_aprovada:
            db.delete(antiga)

    movidas = len(equivalentes)

    agora = _agora_iso()
    sugestao.status_aprovacao = STATUS_SUGESTAO_APROVADA
    sugestao.grupo_destino = destino_final
    sugestao.aplicado_em = agora
    sugestao.atualizado_em = agora
    db.commit()
    db.refresh(sugestao)
    rotulo = (
        "Descrição movida"
        if movidas == 1
        else f"{movidas} descrições equivalentes movidas"
    )
    return {
        "status": "sucesso",
        "mensagem": (
            f"{rotulo} para o grupo novo {destino_final}."
            if not destino_escolhido
            else f"{rotulo} para o grupo {destino_final}."
        ),
        "sugestao": _sugestao_json(sugestao),
    }


@app.post("/api/ia/auditar-base-completa")
def auditar_base_completa(
    dados: Optional[IAAuditarBaseRequest] = None,
    admin: Usuario = Depends(auth.get_admin_user_2fa),
    db: Session = Depends(get_db),
):
    """Envia os grupos com 2+ descrições ao DeepSeek e grava sugestões.

    A IA NÃO altera a base: as respostas ficam em `sugestoes_correcao_ia` como
    ``pendente`` até o administrador clicar em Aplicar. Sem DEEPSEEK_API_KEY a
    rota responde 503 com instrução, como as demais rotas de IA.
    """
    if not ia_consenso.configurada():
        raise HTTPException(
            status_code=503,
            detail=(
                "IA não configurada no servidor: defina DEEPSEEK_API_KEY e "
                "reinicie a API."
            ),
        )
    limite = dados.limite if dados else None
    try:
        return _auditar_base_completa(db, limite)
    except ia_consenso.ErroIA as erro:
        raise HTTPException(status_code=erro.status, detail=str(erro)) from erro


@app.get("/api/ia/sugestoes")
def listar_sugestoes_ia(
    status_aprovacao: Optional[str] = Query(
        None,
        description="Filtra por pendente, aprovada ou ignorada (vazio = todas)",
    ),
    admin: Usuario = Depends(auth.get_admin_user_2fa),
    db: Session = Depends(get_db),
):
    """Lista as sugestões de correção para os cards da Auditoria.

    Sugestões PENDENTES são validadas contra o agrupamento atual e deduplicadas por
    descrição (ignorando os números de controle): chaves antigas que não existem mais
    não aparecem, e "COCA-COLA ZERO - 1..3" vira um único card. O histórico de
    aprovadas/ignoradas continua disponível quando filtrado por status.
    """
    consulta = db.query(SugestaoCorrecaoIA)
    if status_aprovacao:
        status = str(status_aprovacao).strip().lower()
        if status not in STATUS_SUGESTAO_VALIDOS:
            raise HTTPException(
                status_code=400,
                detail="Status inválido: use pendente, aprovada ou ignorada.",
            )
        if status == STATUS_SUGESTAO_PENDENTE:
            consulta = consulta.filter(
                or_(
                    SugestaoCorrecaoIA.status_aprovacao == status,
                    SugestaoCorrecaoIA.status_aprovacao.is_(None),
                )
            )
        else:
            consulta = consulta.filter(SugestaoCorrecaoIA.status_aprovacao == status)
    registros = consulta.order_by(SugestaoCorrecaoIA.id.desc()).all()
    if any(
        (registro.status_aprovacao or STATUS_SUGESTAO_PENDENTE)
        == STATUS_SUGESTAO_PENDENTE
        for registro in registros
    ):
        mapa_regras, mapa_descricoes = _mapas_regras_relatorio(db)
        grupos = _grupos_produto_relatorio(db, mapa_regras, mapa_descricoes)
        vistos, filtrados = set(), []
        for registro in registros:
            status = registro.status_aprovacao or STATUS_SUGESTAO_PENDENTE
            if status == STATUS_SUGESTAO_PENDENTE:
                if not _sugestao_ainda_valida(registro, grupos):
                    continue
                equivalencia = _equivalencia_descricao(
                    registro.item_sugerido_sair
                )
                if equivalencia in vistos:
                    continue
                vistos.add(equivalencia)
            filtrados.append(registro)
        registros = filtrados
    return {
        "status": "sucesso",
        "total": len(registros),
        "sugestoes": [_sugestao_json(registro) for registro in registros],
    }


@app.post("/api/ia/aplicar-sugestao/{sugestao_id}")
def aplicar_sugestao_ia(
    sugestao_id: int,
    dados: Optional[IAAplicarSugestaoRequest] = None,
    admin: Usuario = Depends(auth.get_admin_user_2fa),
    db: Session = Depends(get_db),
):
    """Aprova a sugestão e move o item para um grupo novo ou existente."""
    sugestao = (
        db.query(SugestaoCorrecaoIA)
        .filter(SugestaoCorrecaoIA.id == sugestao_id)
        .first()
    )
    if not sugestao:
        raise HTTPException(status_code=404, detail="Sugestão de IA não encontrada.")
    status = sugestao.status_aprovacao or STATUS_SUGESTAO_PENDENTE
    if status == STATUS_SUGESTAO_APROVADA:
        return {
            "status": "sucesso",
            "mensagem": "Esta sugestão já foi aplicada.",
            "sugestao": _sugestao_json(sugestao),
        }
    if status == STATUS_SUGESTAO_IGNORADA:
        raise HTTPException(
            status_code=409,
            detail="A sugestão foi ignorada; rode a auditoria com IA novamente.",
        )
    return _aplicar_sugestao_ia(
        db,
        sugestao,
        chave_destino=dados.chave_destino if dados else None,
    )


@app.post("/api/ia/ignorar-sugestao/{sugestao_id}")
def ignorar_sugestao_ia(
    sugestao_id: int,
    admin: Usuario = Depends(auth.get_admin_user_2fa),
    db: Session = Depends(get_db),
):
    """Descarta a sugestão sem tocar na base (não é recriada na próxima rodada)."""
    sugestao = (
        db.query(SugestaoCorrecaoIA)
        .filter(SugestaoCorrecaoIA.id == sugestao_id)
        .first()
    )
    if not sugestao:
        raise HTTPException(status_code=404, detail="Sugestão de IA não encontrada.")
    if (sugestao.status_aprovacao or STATUS_SUGESTAO_PENDENTE) == STATUS_SUGESTAO_APROVADA:
        raise HTTPException(
            status_code=409,
            detail="Sugestão já aplicada: não é possível ignorar.",
        )
    sugestao.status_aprovacao = STATUS_SUGESTAO_IGNORADA
    sugestao.atualizado_em = _agora_iso()
    db.commit()
    db.refresh(sugestao)
    return {
        "status": "sucesso",
        "mensagem": "Sugestão ignorada; a base não foi alterada.",
        "sugestao": _sugestao_json(sugestao),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Memória/cache da IA de extração de marca (tabela marcas_ia)
# ──────────────────────────────────────────────────────────────────────────────
def _descricoes_unicas_marcas_ia(db: Session) -> List[str]:
    """Descrições únicas na MESMA chave normalizada usada pela memória `marcas_ia`."""
    descricoes = set()
    for (descricao,) in db.query(ItemNota.descricao_produto).all():
        chave = regras_marcas.normalizar(
            regras_marcas.remover_numero_controle(descricao)
        ).strip()
        if chave:
            descricoes.add(chave)
    return sorted(descricoes)


def _resumo_memoria_marcas_ia(db: Session) -> dict:
    """Contadores da memória para o painel do administrador."""
    total = db.query(func.count(MarcaIA.id)).scalar() or 0
    sem_marca = (
        db.query(func.count(MarcaIA.id))
        .filter(
            or_(
                MarcaIA.marca_extraida.is_(None),
                MarcaIA.marca_extraida == ia_consenso.MARCA_SEM_MARCA,
            )
        )
        .scalar()
        or 0
    )
    ultima = db.query(func.max(MarcaIA.data_consulta)).scalar()
    return {
        "total": int(total),
        "com_marca": int(total) - int(sem_marca),
        "sem_marca": int(sem_marca),
        "ultima_consulta": ultima,
    }


@app.get("/api/marcas/ia/descricoes")
def listar_descricoes_marcas_ia(
    admin: Usuario = Depends(auth.get_admin_user_2fa),
    db: Session = Depends(get_db),
):
    """Descrições únicas da base para o front processar em lotes com progresso.

    A lista já vem normalizada (maiúsculas, sem acento e sem número de controle): é a
    chave da memória. O resumo do cache mostra quantas consultas o botão pode economizar.
    """
    descricoes = _descricoes_unicas_marcas_ia(db)
    return {
        "status": "sucesso",
        "total": len(descricoes),
        "descricoes": descricoes,
        "cache": _resumo_memoria_marcas_ia(db),
    }


@app.post("/api/marcas/ia/processar")
def processar_marcas_ia(
    dados: IAMarcasProcessarRequest,
    admin: Usuario = Depends(auth.get_admin_user_2fa),
    db: Session = Depends(get_db),
):
    """Analisa um lote: consulta a memória e só chama a DeepSeek para inéditas.

    O lote existe para o Streamlit desenhar a barra de progresso. A resposta informa
    ``consultas_ia`` (tokens gastos) e ``cache_hits`` (consultas economizadas). Se a IA
    falhar/sem chave, o lote continua apenas com o cache e devolve ``erro`` para a tela.
    """
    estatisticas = {
        "cache_hits": 0,
        "cache_misses": 0,
        "consultas_ia": 0,
        "sem_marca": 0,
        "marcas_encontradas": 0,
    }
    resultados = []
    erro = None
    permitir_ia = bool(dados.usar_ia)
    for descricao in dados.descricoes:
        chave = ia_consenso._normalizar_descricao_memoria(descricao)
        if not chave:
            continue
        try:
            marca = ia_consenso.obter_marca_inteligente(
                chave,
                db=db,
                usar_ia=permitir_ia,
                estatisticas=estatisticas,
            )
        except (ia_consenso.IAnaoconfigurada, ia_consenso.ErroIA) as falha:
            # A falha ocorre após o cache miss: a descrição atual fica sem resposta e os
            # itens seguintes do lote continuam só pela memória (nenhum token novo).
            erro = str(falha)
            permitir_ia = False
            marca = None
        sem_marca = marca is not None and ia_consenso.eh_sem_marca(marca)
        resultados.append(
            {
                "descricao": chave,
                "marca": None if sem_marca else marca,
                "sem_marca": bool(sem_marca),
            }
        )
    return {
        "status": "sucesso",
        "processadas": len(resultados),
        "resultados": resultados,
        **estatisticas,
        "erro": erro,
        "cache": _resumo_memoria_marcas_ia(db),
    }


@app.post("/api/marcas/ia/aplicar")
def aplicar_memoria_marcas_ia(
    admin: Usuario = Depends(auth.get_admin_user_2fa),
    db: Session = Depends(get_db),
):
    """Aplica ao catálogo APENAS o que já está na memória (sem chamar a IA).

    É a segunda etapa do botão: o front processou os lotes (e desenhou o progresso),
    agora o recálculo usa os cache hits e a heurística local para o que ainda faltar.
    """
    resumo = regras_marcas.recalcular_marcas(db, apenas_cache=True)
    return {
        "status": "sucesso",
        **resumo,
        "cache": _resumo_memoria_marcas_ia(db),
    }


@app.post("/api/marcas/ia/recalcular")
def recalcular_marcas_ia(
    admin: Usuario = Depends(auth.get_admin_user_2fa),
    db: Session = Depends(get_db),
):
    """Recalcula marcas do zero usando a memória e a IA para as descrições inéditas.

    Rota de uma chamada só (script/integração). O front usa `/processar` em lotes +
    `/aplicar` para conseguir mostrar a barra de progresso e as contagens parciais.
    """
    resumo = regras_marcas.recalcular_marcas(db, usar_ia=True)
    return {
        "status": "sucesso",
        **resumo,
        "cache": _resumo_memoria_marcas_ia(db),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Auditoria (ADMIN + 2FA). Todas as rotas abaixo usam `auth.get_admin_user_2fa`:
# sem token de sessão → 401; conta comum → 403; admin SEM 2FA ativo → 403 (a tela
# mostra o QR Code); admin com 2FA e sem o `X-2FA-Token` → 401. Só depois dessas
# barreiras a rota chega ao banco. Esconder o menu no front é conveniência — a
# autorização de verdade é esta.
# ──────────────────────────────────────────────────────────────────────────────
def _configuracao_sistema_json(db: Session) -> dict:
    """Configuração global do envio (token da API do WhatsApp) para a tela do admin.

    O valor NUNCA volta inteiro: só os 4 últimos caracteres (`whatsapp_token_mascarado`)
    e a data de atualização. O token real fica no banco e é usado pelo envio.
    """
    registro = (
        db.query(ConfiguracaoSistema)
        .filter(ConfiguracaoSistema.chave == motor_alertas.CHAVE_TOKEN_WHATSAPP)
        .first()
    )
    valor = str(registro.valor or "") if registro else ""
    return {
        "whatsapp_token_configurado": bool(valor),
        "whatsapp_token_mascarado": motor_alertas.mascarar_token(valor),
        "atualizado_em": registro.atualizado_em if registro else None,
        "atualizado_por": registro.atualizado_por if registro else None,
    }


@app.get("/api/admin/configuracoes")
def ler_configuracoes_do_sistema(
    admin: Usuario = Depends(auth.get_admin_user_2fa), db: Session = Depends(get_db)
):
    """Token da API do WhatsApp (campo do administrador, na aba ⚙️ Configurações)."""
    return _configuracao_sistema_json(db)


@app.post("/api/admin/configuracoes")
def salvar_configuracoes_do_sistema(
    dados: ConfiguracaoSistemaRequest,
    admin: Usuario = Depends(auth.get_admin_user_2fa),
    db: Session = Depends(get_db),
):
    """Grava o token da API do WhatsApp (envio dos alertas).

    Enviar o campo vazio REMOVE o token (o disparo real fica desligado). A resposta
    devolve o token mascarado — o valor completo nunca sai do servidor.
    """
    token = str(dados.whatsapp_token or "").strip()
    registro = (
        db.query(ConfiguracaoSistema)
        .filter(ConfiguracaoSistema.chave == motor_alertas.CHAVE_TOKEN_WHATSAPP)
        .first()
    )
    if registro is None:
        registro = ConfiguracaoSistema(chave=motor_alertas.CHAVE_TOKEN_WHATSAPP)
        db.add(registro)
    registro.valor = token or None
    registro.atualizado_em = datetime.now(timezone.utc).isoformat(timespec="seconds")
    registro.atualizado_por = admin.email
    db.commit()
    db.refresh(registro)

    corpo = _configuracao_sistema_json(db)
    corpo["sucesso"] = True
    corpo["mensagem"] = (
        "Token da API do WhatsApp salvo." if token else "Token da API do WhatsApp removido."
    )
    return corpo



@app.get("/api/auditados")
def listar_auditados(
    admin: Usuario = Depends(auth.get_admin_user_2fa), db: Session = Depends(get_db)
):
    """Chaves com descrição aprovada na Auditoria (restringe a administradores)."""
    return [a.chave_identificacao for a in db.query(ItemAuditado).filter(ItemAuditado.auditado == True).all()]

@app.get("/api/auditados/pendentes")
def listar_auditados_pendentes(
    motivo: Optional[str] = Query(
        None,
        description="Filtra pelo motivo da pendência: 'manual' (enviada pelo usuário) "
        "ou 'aberrante' (variação brusca marcada automaticamente)",
    ),
    admin: Usuario = Depends(auth.get_admin_user_2fa),
    db: Session = Depends(get_db),
):
    """Chaves marcadas como pendentes de validação na Auditoria.

    - motivo="manual": saem dos relatórios até serem aprovadas;
    - motivo="aberrante": variação brusca (>= 50%) — continuam visíveis nas tabelas com
      o alerta, mas fora dos totais até o auditor validar.
    """
    consulta = db.query(ItemAuditado).filter(ItemAuditado.auditado == False)  # noqa: E712
    if motivo:
        consulta = consulta.filter(ItemAuditado.motivo == str(motivo).strip().lower())
    return [registro.chave_identificacao for registro in consulta.all()]


def _aprovar_equivalentes_por_descricao(db: Session, chave: str) -> int:
    """Aprova também as chaves antigas da MESMA descrição.

    Quando a chave do grupo ganha um sufixo novo (marca/volume/embalagem), a aprovação
    da descrição cria a chave atual, mas uma pendência de variação brusca gravada com a
    chave antiga continuaria aparecendo no aviso amarelo. Aqui as chaves legadas da
    mesma descrição (sufixo após o prefixo ``DESC_<grupo>_``) são validadas juntas.
    """
    texto_chave = str(chave or "").strip()
    if not texto_chave.startswith("DESC_"):
        return 0
    descricoes = {
        _limpar_texto_relatorio(linha[0])
        for linha in db.query(ItemNota.descricao_produto).distinct().all()
        if str(linha[0] or "").strip()
    }
    candidatas = [
        descricao for descricao in descricoes if texto_chave.endswith("_" + descricao)
    ]
    if not candidatas:
        return 0
    alvo = max(candidatas, key=len)
    aprovadas = 0
    for registro in (
        db.query(ItemAuditado).filter(ItemAuditado.auditado == False).all()  # noqa: E712
    ):
        chave_registro = str(registro.chave_identificacao or "")
        if chave_registro == texto_chave or not chave_registro.endswith("_" + alvo):
            continue
        registro.auditado = True
        aprovadas += 1
    return aprovadas


@app.post("/api/auditar")
def marcar_auditado(
    data: AuditRequest,
    admin: Usuario = Depends(auth.get_admin_user_2fa),
    db: Session = Depends(get_db),
):
    """Aprova a chave (volta a contar nos relatórios). Restrito a administradores."""
    existente = db.query(ItemAuditado).filter(ItemAuditado.chave_identificacao == data.chave_identificacao).first()
    if not existente:
        db.add(ItemAuditado(chave_identificacao=data.chave_identificacao, auditado=True))
    else:
        # A chave pode estar marcada como pendente (auditado=False): aprovar precisa
        # liberá-la de volta para os relatórios.
        existente.auditado = True
    _aprovar_equivalentes_por_descricao(db, data.chave_identificacao)
    db.commit()
    return {"status": "sucesso"}


@app.post("/api/auditar/pendente")
def marcar_pendente(
    data: AuditPendenteRequest,
    admin: Usuario = Depends(auth.get_admin_user_2fa),
    db: Session = Depends(get_db),
):
    """Marca chaves como "Pendente de Aprovação" na Auditoria (restrito a administradores).

    O `motivo` decide o efeito nos relatórios:
    - "manual": o item sai dos relatórios até ser validado;
    - "aberrante": variação brusca — o item continua na tabela de Impacto com o alerta,
      mas nunca entra nos totais.
    """
    motivo = str(data.motivo or MOTIVO_PENDENCIA_MANUAL).strip().lower()
    if motivo not in {MOTIVO_PENDENCIA_MANUAL, MOTIVO_PENDENCIA_ABERRACAO}:
        raise HTTPException(
            status_code=400,
            detail="Motivo inválido: use 'manual' ou 'aberrante'.",
        )
    marcadas = 0
    for chave in data.chaves or []:
        chave = str(chave or "").strip()
        if not chave:
            continue
        existente = db.query(ItemAuditado).filter(ItemAuditado.chave_identificacao == chave).first()
        if existente:
            existente.auditado = False
            existente.motivo = motivo
        else:
            db.add(
                ItemAuditado(
                    chave_identificacao=chave,
                    auditado=False,
                    motivo=motivo,
                )
            )
        marcadas += 1
    db.commit()
    return {"status": "sucesso", "marcadas": marcadas, "motivo": motivo}

@app.delete("/api/auditar/{chave_identificacao}")
def desmarcar_auditado(
    chave_identificacao: str,
    admin: Usuario = Depends(auth.get_admin_user_2fa),
    db: Session = Depends(get_db),
):
    """Desfaz a aprovação da chave (restrito a administradores)."""
    existente = db.query(ItemAuditado).filter(ItemAuditado.chave_identificacao == chave_identificacao).first()
    if existente:
        db.delete(existente)
        db.commit()
        return {"status": "sucesso", "mensagem": "Aprovação desfeita."}
    return {"status": "sucesso", "mensagem": "Registro não encontrado (já estava pendente)."}

class RegraAgrupamentoRequest(BaseModel):
    chave_origem: str
    chave_destino: str
    tipo: str = "juntar"

@app.get("/api/regras-agrupamento")
def listar_regras_agrupamento(
    admin: Usuario = Depends(auth.get_admin_user_2fa), db: Session = Depends(get_db)
):
    """Regras de-para do catálogo — parte da Auditoria (restrito a administradores)."""
    regras = db.query(RegraAgrupamento).order_by(RegraAgrupamento.id).all()
    return [{"id": r.id, "chave_origem": r.chave_origem, "chave_destino": r.chave_destino, "tipo": r.tipo} for r in regras]

@app.post("/api/regras-agrupamento")
def criar_regra_agrupamento(
    data: RegraAgrupamentoRequest,
    admin: Usuario = Depends(auth.get_admin_user_2fa),
    db: Session = Depends(get_db),
):
    """Grava uma regra de-para (restrito a administradores).

    chave_origem pode ser:
    - a chave de um GRUPO (ex.: EAN_789... / SEM_EAN_...) -> tipo "juntar" (funde o grupo no destino);
    - a chave de uma DESCRIÇÃO (DESC_<chave_do_grupo>_<descricao>) -> tipo "mover" (move só aquela descrição).
    """
    origem = (data.chave_origem or "").strip()
    destino = (data.chave_destino or "").strip()
    tipo = (data.tipo or "juntar").strip().lower()
    if not origem or not destino:
        raise HTTPException(status_code=400, detail="Chave de origem e de destino são obrigatórias.")
    if origem == destino:
        raise HTTPException(status_code=400, detail="Origem e destino não podem ser o mesmo grupo.")
    if tipo not in ("juntar", "mover"):
        raise HTTPException(status_code=400, detail="Tipo inválido. Use 'juntar' ou 'mover'.")
    if destino.startswith("DESC_"):
        raise HTTPException(
            status_code=400,
            detail="O destino deve ser um grupo de produto, não uma descrição (mova para o grupo de destino).",
        )
    mapa = {r.chave_origem: r.chave_destino for r in db.query(RegraAgrupamento).all()}
    atual, visitados = destino, set()
    while atual in mapa and atual not in visitados:
        visitados.add(atual)
        atual = mapa[atual]
    if atual == origem:
        raise HTTPException(status_code=400, detail="Essa regra criaria um ciclo de agrupamento.")
    existente = db.query(RegraAgrupamento).filter(RegraAgrupamento.chave_origem == origem).first()
    if existente:
        existente.chave_destino = destino
        existente.tipo = tipo
    else:
        db.add(RegraAgrupamento(chave_origem=origem, chave_destino=destino, tipo=tipo))
    db.commit()
    return {"status": "sucesso", "chave_origem": origem, "chave_destino": destino, "tipo": tipo}

@app.delete("/api/regras-agrupamento/{regra_id}")
def remover_regra_agrupamento(
    regra_id: int,
    admin: Usuario = Depends(auth.get_admin_user_2fa),
    db: Session = Depends(get_db),
):
    """Remove uma regra de-para (restrito a administradores)."""
    regra = db.query(RegraAgrupamento).filter(RegraAgrupamento.id == regra_id).first()
    if not regra:
        raise HTTPException(status_code=404, detail="Regra de agrupamento não encontrada.")
    db.delete(regra)
    db.commit()
    return {"status": "sucesso", "mensagem": "Regra de agrupamento removida."}


# ──────────────────────────────────────────────────────────────────────────────
# Memória de aprendizado (De-Para fornecedor + cProd) e embalagens
# ──────────────────────────────────────────────────────────────────────────────
class MapeamentoProdutoRequest(BaseModel):
    """Correção manual de um de-para na Auditoria.

    Só os campos informados são alterados; a correção passa a valer também para as
    notas já importadas (`aplicar_mapeamento_historico`) e a importação nunca mais
    sobrescreve o registro (`origem = "manual"`).
    """

    cnpj_fornecedor: str
    c_prod: str
    ean_padrao: Optional[str] = None
    descricao_padrao: Optional[str] = None
    ncm_gravado: Optional[str] = None
    qtd_por_embalagem: Optional[float] = None
    unidade_base: Optional[str] = None


def _mapeamento_json(
    mapeamento: MapeamentoFornecedorProduto, nomes: dict, marcas_ia=None
) -> dict:
    """De-para pronto para a tabela da Auditoria (com embalagem e marca da IA)."""
    embalagem = aprendizado.formatar_embalagem(
        {
            "tipo_embalagem": aprendizado._prefixo_embalagem(
                mapeamento.u_com_gravada or ""
            )
            or "EMBALAGEM",
            "qtd_por_embalagem": float(mapeamento.qtd_por_embalagem or 1.0),
            "unidade_base": mapeamento.unidade_base or "UN",
            "confirmada": float(mapeamento.qtd_por_embalagem or 1.0) > 1.0,
        }
    )
    descricao_padrao = mapeamento.descricao_padrao or ""
    chave_marca = (
        ia_consenso._normalizar_descricao_memoria(descricao_padrao)
        if descricao_padrao
        else ""
    )
    marca_ia = (marcas_ia or {}).get(chave_marca)
    if not marca_ia or ia_consenso.eh_sem_marca(marca_ia) or ia_consenso._marca_invalida(
        marca_ia
    ):
        marca_ia = ia_consenso.MARCA_SEM_MARCA
    else:
        marca_ia = ia_consenso._completar_marca(str(marca_ia)[: ia_consenso.MAXIMO_MARCA])

    return {
        "id": mapeamento.id,
        "cnpj_fornecedor": mapeamento.cnpj_fornecedor,
        "fornecedor": nomes.get(mapeamento.cnpj_fornecedor, ""),
        "c_prod": mapeamento.c_prod,
        "ean_padrao": mapeamento.ean_padrao or "",
        "descricao_padrao": descricao_padrao,
        "marca_ia": marca_ia,
        "ncm_gravado": mapeamento.ncm_gravado or "",
        "u_com_gravada": mapeamento.u_com_gravada or "",
        "qtd_por_embalagem": float(mapeamento.qtd_por_embalagem or 1.0),
        "unidade_base": mapeamento.unidade_base or "UN",
        "embalagem": embalagem,
        "origem": mapeamento.origem or "automatico",
        "atualizado_em": mapeamento.atualizado_em,
    }

@app.get("/api/aprendizado-produtos")
def listar_aprendizado_produtos(
    busca: Optional[str] = Query(
        None, description="Filtra por cProd, descrição, EAN, NCM ou fornecedor."
    ),
    limite: int = Query(300, ge=1, le=2000),
    admin: Usuario = Depends(auth.get_admin_user_2fa),
    db: Session = Depends(get_db),
):
    """De-Para aprendido (fornecedor + cProd) — parte da Auditoria.

    Devolve também quantos itens da base já têm embalagem identificada, indicador de
    que o tratamento CX/FD → UN/KG está funcionando.
    """
    consulta = db.query(MapeamentoFornecedorProduto)
    termo = str(busca or "").strip()
    if termo:
        padrao = f"%{termo}%"
        consulta = consulta.filter(
            or_(
                MapeamentoFornecedorProduto.c_prod.ilike(padrao),
                MapeamentoFornecedorProduto.cnpj_fornecedor.ilike(padrao),
                MapeamentoFornecedorProduto.descricao_padrao.ilike(padrao),
                MapeamentoFornecedorProduto.ean_padrao.ilike(padrao),
                MapeamentoFornecedorProduto.ncm_gravado.ilike(padrao),
            )
        )
    total = consulta.count()
    registros = (
        consulta.order_by(MapeamentoFornecedorProduto.atualizado_em.desc())
        .limit(limite)
        .all()
    )
    nomes = {cnpj: nome for cnpj, nome in db.query(Fornecedor.cnpj, Fornecedor.nome).all()}
    marcas_ia = {
        str(registro.descricao_original): registro.marca_extraida
        for registro in db.query(MarcaIA).all()
    }
    itens = [
        _mapeamento_json(registro, nomes, marcas_ia) for registro in registros
    ]
    return {
        "total": total,
        "limite": limite,
        "itens": itens,
        "manuais": sum(1 for item in itens if item["origem"] == "manual"),
        "itens_com_embalagem": db.query(ItemNota)
        .filter(ItemNota.qtd_por_embalagem > 1.0)
        .count(),
        "itens_sem_codigo_fornecedor": db.query(ItemNota)
        .filter(ItemNota.c_prod.is_(None))
        .count(),
    }




@app.put("/api/aprendizado-produtos")
def salvar_aprendizado_produto(
    dados: MapeamentoProdutoRequest,
    admin: Usuario = Depends(auth.get_admin_user_2fa),
    db: Session = Depends(get_db),
):
    """Corrige um de-para manualmente e reaplica nos itens já importados.

    `origem` vira "manual": a partir daqui a importação só preenche campos vazios.
    """
    cnpj = aprendizado.normalizar_cnpj(dados.cnpj_fornecedor)
    c_prod = aprendizado.normalizar_codigo_produto(dados.c_prod)
    if not cnpj or not c_prod:
        raise HTTPException(
            status_code=400, detail="CNPJ do fornecedor e cProd são obrigatórios."
        )
    fator = dados.qtd_por_embalagem
    if fator is not None and fator < 1.0:
        raise HTTPException(
            status_code=400,
            detail="A quantidade por embalagem não pode ser menor que 1.",
        )
    mapeamento = (
        db.query(MapeamentoFornecedorProduto)
        .filter(MapeamentoFornecedorProduto.cnpj_fornecedor == cnpj)
        .filter(MapeamentoFornecedorProduto.c_prod == c_prod)
        .first()
    )
    if mapeamento is None:
        mapeamento = MapeamentoFornecedorProduto(cnpj_fornecedor=cnpj, c_prod=c_prod)
        db.add(mapeamento)
    if dados.ean_padrao is not None:
        mapeamento.ean_padrao = str(dados.ean_padrao).strip()
    if dados.descricao_padrao is not None:
        mapeamento.descricao_padrao = str(dados.descricao_padrao).strip()
    if dados.ncm_gravado is not None:
        mapeamento.ncm_gravado = str(dados.ncm_gravado).strip()
    if dados.unidade_base is not None:
        mapeamento.unidade_base = str(dados.unidade_base).strip().upper() or "UN"
    if fator is not None:
        mapeamento.qtd_por_embalagem = float(fator)
        if fator <= 1.0:
            mapeamento.unidade_base = mapeamento.unidade_base or "UN"
    mapeamento.origem = "manual"
    mapeamento.atualizado_em = aprendizado._agora()
    db.commit()
    db.refresh(mapeamento)
    afetados = aprendizado.aplicar_mapeamento_historico(db, mapeamento)
    return {
        "status": "sucesso",
        "mapeamento": _mapeamento_json(mapeamento, {}),
        "itens_atualizados": afetados,
    }


@app.post("/api/aprendizado-produtos/reprocessar")
def reprocessar_aprendizado(
    admin: Usuario = Depends(auth.get_admin_user_2fa), db: Session = Depends(get_db)
):
    """Reprocessa a base inteira (mesmo motor do `backfill_aprendizado.py`).

    Recalcula as embalagens dos itens antigos e completa a memória de de-para a partir
    das notas já importadas. Não altera mapeamentos de origem manual.
    """
    return {"status": "sucesso", **aprendizado.sincronizar_aprendizado(db)}



# ──────────────────────────────────────────────────────────────────────────────
# Visualização da Danfe / Nota Fiscal original
# ──────────────────────────────────────────────────────────────────────────────
# Os campos do XML são lidos com o curinga "{*}" do ElementTree, que ignora o
# namespace (http://www.portalfiscal.inf.br/nfe) — funciona também com NFe
# assinadas, nfeProc ou XMLs que usem prefixo diferente.
def _texto_xml(elemento, caminho):
    """Texto de um campo do XML, tolerante a namespace e a campos ausentes."""
    if elemento is None:
        return ""
    achado = elemento.find(caminho)
    if achado is None or achado.text is None:
        return ""
    return achado.text.strip()


def _numero_xml(texto):
    """Converte valores do XML ("383.10" / "1.234,56") em float; None quando inválido."""
    if texto is None:
        return None
    limpo = str(texto).strip()
    if not limpo:
        return None
    if "," in limpo:
        limpo = limpo.replace(".", "").replace(",", ".")
    try:
        return float(limpo)
    except ValueError:
        return None


def _data_iso(valor):
    """Data (Date/datetime do banco) em ISO 8601, sem estourar se vier como texto."""
    if not valor:
        return ""
    return valor.isoformat() if hasattr(valor, "isoformat") else str(valor)


def _caminho_xml_absoluto(caminho_xml):
    """Resolve o caminho_xml gravado no banco (normalmente relativo à raiz do projeto).

    Retorna None quando o caminho é vazio ou quando um caminho relativo tentaria sair
    da pasta do projeto (proteção contra path traversal).
    """
    caminho = str(caminho_xml or "").strip().strip('"')
    if not caminho:
        return None
    if os.path.isabs(caminho):
        return os.path.abspath(caminho)
    raiz = os.path.abspath(PROJECT_ROOT)
    absoluto = os.path.abspath(os.path.join(raiz, caminho))
    try:
        if os.path.commonpath([absoluto, raiz]) != raiz:
            return None
    except ValueError:  # unidades de disco diferentes
        return None
    return absoluto


def _participante_vazio():
    return {
        "cnpj": "",
        "razao_social": "",
        "nome_fantasia": "",
        "inscricao_estadual": "",
        "logradouro": "",
        "numero": "",
        "complemento": "",
        "bairro": "",
        "municipio": "",
        "uf": "",
        "cep": "",
        "endereco": "",
    }


def _endereco_participante(elemento_endereco):
    """Endereço do emitente/destinatário já formatado no padrão de leitura do DANFE."""
    dados = _participante_vazio()
    if elemento_endereco is None:
        return dados
    dados["logradouro"] = _texto_xml(elemento_endereco, "{*}xLgr")
    dados["numero"] = _texto_xml(elemento_endereco, "{*}nro")
    dados["complemento"] = _texto_xml(elemento_endereco, "{*}xCpl")
    dados["bairro"] = _texto_xml(elemento_endereco, "{*}xBairro")
    dados["municipio"] = _texto_xml(elemento_endereco, "{*}xMun")
    dados["uf"] = _texto_xml(elemento_endereco, "{*}UF")
    dados["cep"] = _texto_xml(elemento_endereco, "{*}CEP")

    linha_logradouro = ", ".join(p for p in (dados["logradouro"], dados["numero"]) if p)
    if dados["complemento"]:
        linha_logradouro = (
            f"{linha_logradouro} - {dados['complemento']}" if linha_logradouro else dados["complemento"]
        )
    cidade = "/".join(p for p in (dados["municipio"], dados["uf"]) if p)
    cep = f"CEP {dados['cep']}" if dados["cep"] else ""
    dados["endereco"] = " - ".join(
        parte for parte in (linha_logradouro, dados["bairro"], cidade, cep) if parte
    )
    return dados


def _participante_xml(elemento):
    """Emitente/destinatário da NFe (CNPJ, razão social, fantasia e endereço)."""
    dados = _participante_vazio()
    if elemento is None:
        return dados
    dados["cnpj"] = _texto_xml(elemento, "{*}CNPJ") or _texto_xml(elemento, "{*}CPF")
    dados["razao_social"] = _texto_xml(elemento, "{*}xNome")
    dados["nome_fantasia"] = _texto_xml(elemento, "{*}xFant")
    dados["inscricao_estadual"] = _texto_xml(elemento, "{*}IE")
    endereco = elemento.find("{*}enderEmit")
    if endereco is None:
        endereco = elemento.find("{*}enderDest")
    dados.update({chave: valor for chave, valor in _endereco_participante(endereco).items() if valor})
    return dados


@app.get("/api/nota/{item_id}")
def obter_nota(
    item_id: int,
    admin: Usuario = Depends(auth.get_admin_user_2fa),
    db: Session = Depends(get_db),
):
    """Dados do DANFE do item: emitente (fornecedor), destinatário (loja), nota e item.

    Restrito a administradores: o DANFE expõe dados das duas pontas da nota, então não
    é servido para a conta comum (o botão "🔍 Ver Nota" só existe na Auditoria).

    Lê o XML original da nota (coluna caminho_xml) para trazer número, série, data/hora
    de emissão, natureza da operação, chave de acesso, protocolo de autorização, valores
    e endereços. Quando o arquivo não está disponível, responde com os dados do banco e
    a flag `xml_disponivel = false` para o front avisar o usuário.
    """
    item = db.query(ItemNota).filter(ItemNota.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail=f"Item {item_id} não encontrado.")
    nota = db.query(NotaFiscal).filter(NotaFiscal.id == item.nota_id).first()
    if not nota:
        raise HTTPException(status_code=404, detail=f"Nota fiscal do item {item_id} não encontrada.")
    empresa = db.query(Empresa).filter(Empresa.id == nota.empresa_id).first()
    fornecedor = db.query(Fornecedor).filter(Fornecedor.id == item.fornecedor_id).first()

    emitente = _participante_vazio()
    if fornecedor:
        emitente["cnpj"] = fornecedor.cnpj or ""
        emitente["razao_social"] = fornecedor.nome or ""
    destinatario = _participante_vazio()
    if empresa:
        destinatario["cnpj"] = empresa.cnpj or ""
        destinatario["razao_social"] = empresa.razao_social or ""
        destinatario["nome_fantasia"] = empresa.nome_loja or ""
        destinatario["municipio"] = empresa.municipio or ""

    resposta = {
        "item_id": item.id,
        "xml_disponivel": False,
        "aviso": None,
        "nota": {
            "id": nota.id,
            "numero": nota.numero_nota or "",
            "serie": nota.serie or "",
            "chave_acesso": nota.chave_acesso or "",
            "data_emissao": _data_iso(nota.data_emissao),
            "data_hora_emissao": "",
            "natureza_operacao": "",
            "valor_total": float(nota.valor_total or 0.0),
            "protocolo": "",
            "autorizada_em": "",
        },
        "emitente": emitente,
        "destinatario": destinatario,
        "totais": {
            "produtos": 0.0,
            "desconto": 0.0,
            "frete": 0.0,
            "outros": 0.0,
            "total": float(nota.valor_total or 0.0),
        },
        "item": {
            "id": item.id,
            "descricao": item.descricao_produto or "",
            "ean": item.ean or "",
            "ncm": item.ncm or "",
            "quantidade": float(item.quantidade or 0.0),
            "valor_unitario": float(item.valor_unitario or 0.0),
            "valor_total": float(item.valor_total or 0.0),
            "fornecedor": fornecedor.nome if fornecedor else "",
            "loja": empresa.nome_loja if empresa else "",
        },
    }

    caminho = _caminho_xml_absoluto(nota.caminho_xml)
    if not caminho or not os.path.exists(caminho):
        resposta["aviso"] = (
            f"Arquivo XML não encontrado ({nota.caminho_xml or 'caminho não informado'}). "
            "Exibindo os dados gravados no banco de dados."
        )
        return resposta

    try:
        raiz = ET.parse(caminho).getroot()
    except (ET.ParseError, OSError) as erro:
        resposta["aviso"] = f"Não foi possível ler o XML ({erro}). Exibindo os dados do banco de dados."
        return resposta

    inf_nota = raiz.find(".//{*}infNFe")
    ide = None if inf_nota is None else inf_nota.find("{*}ide")
    emitente_nf = None if inf_nota is None else inf_nota.find("{*}emit")
    destinatario_nf = None if inf_nota is None else inf_nota.find("{*}dest")
    totais = None if inf_nota is None else inf_nota.find("{*}total/{*}ICMSTot")
    protocolo = raiz.find(".//{*}protNFe/{*}infProt")

    nota_xml = resposta["nota"]
    numero = _texto_xml(ide, "{*}nNF")
    if numero:
        nota_xml["numero"] = numero
    serie = _texto_xml(ide, "{*}serie")
    if serie:
        nota_xml["serie"] = serie
    data_hora = _texto_xml(ide, "{*}dhEmi") or _texto_xml(ide, "{*}dEmi")
    if data_hora:
        nota_xml["data_hora_emissao"] = data_hora
        nota_xml["data_emissao"] = data_hora[:10]
    nota_xml["natureza_operacao"] = _texto_xml(ide, "{*}natOp")

    chave = ""
    if inf_nota is not None and inf_nota.get("Id"):
        chave = str(inf_nota.get("Id")).strip()
        if chave.upper().startswith("NFE"):
            chave = chave[3:]
    chave = chave or _texto_xml(protocolo, "{*}chNFe")
    if chave:
        nota_xml["chave_acesso"] = chave
    nota_xml["protocolo"] = _texto_xml(protocolo, "{*}nProt")
    nota_xml["autorizada_em"] = _texto_xml(protocolo, "{*}dhRecbto")

    valor_nf = _numero_xml(_texto_xml(totais, "{*}vNF"))
    if valor_nf is not None:
        nota_xml["valor_total"] = valor_nf
    resposta["totais"] = {
        "produtos": _numero_xml(_texto_xml(totais, "{*}vProd")) or 0.0,
        "desconto": _numero_xml(_texto_xml(totais, "{*}vDesc")) or 0.0,
        "frete": _numero_xml(_texto_xml(totais, "{*}vFrete")) or 0.0,
        "outros": _numero_xml(_texto_xml(totais, "{*}vOutro")) or 0.0,
        "total": valor_nf if valor_nf is not None else resposta["totais"]["total"],
    }

    # O XML é o documento original e tem prioridade; o banco só completa o que faltar.
    resposta["emitente"] = {
        **resposta["emitente"],
        **{campo: valor for campo, valor in _participante_xml(emitente_nf).items() if valor},
    }
    resposta["destinatario"] = {
        **resposta["destinatario"],
        **{campo: valor for campo, valor in _participante_xml(destinatario_nf).items() if valor},
    }
    resposta["xml_disponivel"] = True
    resposta["arquivo_xml"] = caminho
    return resposta



# ──────────────────────────────────────────────────────────────────────────────
# Relatórios gerenciais (agregação no servidor)
# ──────────────────────────────────────────────────────────────────────────────
# Motivação: o front baixava TODAS as compras em /api/resumo-precos e refazia os
# groupby a cada rerun do Streamlit. Aqui a conta acontece uma única vez por
# requisição e o front recebe só o agregado (período x loja x mercado) mais as
# compras detalhadas que ele realmente mostra no tooltip.
#
# PRIVACIDADE / LGPD: a lista de compras (data, fornecedor e valor) só é devolvida
# para as lojas informadas em `loja`/`lojas` — as lojas do próprio usuário. A coluna
# "Mercado", calculada com TODAS as empresas do banco, volta apenas como número.
DIAS_PERMITIDOS = (7, 15, 30, 90, 180, 360)
PERIODICIDADES = {
    "diaria": "Diária",
    "semanal": "Semanal",
    "quinzenal": "Quinzenal",
    "mensal": "Mensal",
    "bimestral": "Bimestral",
    "trimestral": "Trimestral",
}
PERIODICIDADE_PADRAO = "mensal"
LIMITE_LINHAS_PADRAO = 2000
# Termos que separam variantes do MESMO EAN — mesma lista de app.py (TERMOS_EXCLUSIVOS).
TERMOS_EXCLUSIVOS_PRODUTO = ("CREMOSA", "TOLETE", "PURE", "PALITO", "INTEIRA")
EANS_INVALIDOS = {"", "0", "NONE", "NAN", "NULL", "SEM_EAN", "SEM GTIN"}
# Divergência entre lojas só conta a partir deste percentual (evita ruído de centavos).
TOLERANCIA_DIVERGENCIA = 0.005

# ──────────────────────────────────────────────────────────────────────────────
# Integridade dos relatórios (regras de negócio)
# ──────────────────────────────────────────────────────────────────────────────
# 1) AUDITORIA: só entram nos cálculos as descrições já validadas. As que a aba de
#    Auditoria mostra como "Pendente de Aprovação" (grupos de exceção) e as marcadas
#    como pendentes em ItemAuditado ficam fora — inclusive da média de preço. A exceção
#    são as pendências de ABERRAÇÃO: elas continuam visíveis (marcadas com o alerta)
#    para o auditor validar, mas seguem fora dos totais.
# 2) ABERRAÇÃO: variação de 50% ou mais (para cima ou para baixo) entre a compra anterior
#    e a atual é indício de erro de unidade/cadastro. Não é economia nem aumento: vai
#    para o fim da lista com alerta e é enviada automaticamente para a auditoria.
# 3) BONIFICAÇÃO/DOAÇÃO/BRINDE: item com valor unitário R$ 0,00 ou CFOP 5910/6910 não
#    entra na média (o item não teve preço real de compra).
LIMIAR_ABERRACAO_PCT = 50.0
ALERTA_ABERRACAO = "Verificar Variação Brusca"
CFOPS_BONIFICACAO = {"5910", "6910"}
MOTIVO_PENDENCIA_MANUAL = "manual"
MOTIVO_PENDENCIA_ABERRACAO = "aberrante"
# O filtro de loja usa "Minhas Lojas (Todas)" como rótulo do grupo; todas estas formas
# (inclusive o rótulo antigo) significam "todas as lojas do usuário".
LOJAS_TODAS_ALIASES = {
    "todas",
    "todas as lojas",
    "todas as empresas",
    "minhas lojas",
    "minhas lojas (todas)",
}

# ──────────────────────────────────────────────────────────────────────────────
# Inteligência de marcas (chave Produto + Marca e tooltip)
# ──────────────────────────────────────────────────────────────────────────────
# A marca entra na chave inteligente: "POLENTA PALITO ARRICO" e "POLENTA
# PALITO EL CHEF" têm médias separadas, mesmo com EAN igual. A consulta paralela
# abaixo continua montando o tooltip de marcas substitutas (mesmo NCM + descrição
# parecida > 80% e marca diferente). Nada da consulta paralela entra em soma,
# média ou impacto.
SIMILARIDADE_MINIMA_MARCA = regras_marcas.SIMILARIDADE_MINIMA_SUBSTITUTA
LIMITE_MARCAS_POR_PRODUTO = 5
# Teto de produtos analisados por relatório (a consulta é paralela: sem teto, um
# relatório com milhares de produtos compararia cada descrição com todas as do NCM).
LIMITE_PRODUTOS_MARCAS = 400


def _validar_dias(dias: int) -> int:
    if dias not in DIAS_PERMITIDOS:
        permitidos = ", ".join(str(valor) for valor in DIAS_PERMITIDOS)
        raise HTTPException(
            status_code=400,
            detail=f"Parâmetro 'dias' inválido ({dias}). Use um destes valores: {permitidos}.",
        )
    return dias


def _validar_periodicidade(periodicidade: str) -> str:
    chave = str(periodicidade or PERIODICIDADE_PADRAO).strip().lower()
    if chave not in PERIODICIDADES:
        opcoes = ", ".join(PERIODICIDADES)
        raise HTTPException(
            status_code=400,
            detail=f"Periodicidade inválida ('{periodicidade}'). Use uma destas: {opcoes}.",
        )
    return chave


@lru_cache(maxsize=8192)
def _limpar_texto_relatorio(texto) -> str:
    """Normaliza a descrição do produto (sem acento, maiúscula) para comparar textos."""
    nfkd = unicodedata.normalize("NFKD", str(texto or ""))
    limpo = "".join(caractere for caractere in nfkd if not unicodedata.combining(caractere))
    limpo = re.sub(r"[^A-Z0-9\s]", "", limpo.upper())
    return " ".join(limpo.split())


@lru_cache(maxsize=8192)
def _tem_ean_valido_relatorio(ean) -> bool:
    if ean is None:
        return False
    return str(ean).strip().upper() not in EANS_INVALIDOS


# Unidades aceitas na regra obrigatória de volume/peso. A ordem da alternância
# importa: as formas longas vêm primeiro para "LITROS" não ser lido como "L" e
# "KG" não ser lido como "G". A regex usa apenas a descrição, nunca o EAN/NCM.
_UNIDADES_VOLUME = (
    r"MILILITROS?|MILILITRO|ML|LITROS?|LITRO|LTS?|LT|L|"
    r"QUILOGRAMAS?|QUILOGRAMA|QUILOS?|QUILO|KGS?|KG|"
    r"MILIGRAMAS?|MILIGRAMA|MGS?|MG|GRAMAS?|GRAMA|GRS?|GR|G"
)
_VOLUME_RE = re.compile(
    rf"(?<![A-Z0-9])(?:(?P<multiplicador>\d+(?:[.,]\d+)?)\s*[Xx]\s*)?"
    rf"(?P<quantidade>\d+(?:[.,]\d+)?)\s*(?P<unidade>{_UNIDADES_VOLUME})(?![A-Z])",
    re.IGNORECASE,
)
_VOLUME_SUFIXO_RE = re.compile(r"(?:_VOL_[^_]+)+$")
# A marca entra na chave do grupo como sufixo `_MARCA_<TOKEN>`. O token é sempre
# alfanumérico (sem espaço/pontuação), então basta um segmento por marca.
_MARCA_SUFIXO_RE = re.compile(r"(?:_MARCA_[^_]+)+$")
# Embalagem como IDENTIDADE do produto: `_EMB_CX24`, `_EMB_FD10X1KG`...
_EMB_SUFIXO_RE = re.compile(r"(?:_EMB_[^_]+)+$")
# Valores que significam "marca não identificada": NÃO entram na chave e o grupo
# continua sendo formado pelo nome base (mesma regra do front).
MARCAS_NAO_IDENTIFICADAS = {
    "",
    "0",
    "NAN",
    "NONE",
    "NULL",
    "SEM MARCA",
    "NAO IDENTIFICADA",
    "NAOIDENTIFICADA",
    "MARCA NAO IDENTIFICADA",
}
# "Polenta palito - 1", "- 2" etc. são números de controle do fornecedor, não
# parte do nome do produto. Eles saem ANTES de montar o nome/chave do grupo.
_NUMERO_CONTROLE_FINAL_RE = re.compile(r"(?:\s*[-–—]\s*\d{1,3})+\s*$")


def _decimal_medida(valor) -> Optional[Decimal]:
    """Converte "1,5"/"1.5" em Decimal; None quando o texto não é numérico."""
    try:
        return Decimal(str(valor).replace(",", "."))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _formatar_medida(valor: Decimal) -> str:
    """Formata a medida sem zeros inúteis (1.500 -> 1.5; 20.0 -> 20)."""
    texto = format(valor.normalize(), "f")
    if "." in texto:
        texto = texto.rstrip("0").rstrip(".")
    return texto or "0"


def extrair_volume(descricao) -> Optional[str]:
    """Extrai a medida de volume/peso da descrição, de forma canônica.

    A regra de negócio é OBRIGATÓRIA: descrições com medidas diferentes não
    podem cair no mesmo grupo, mesmo que o EAN seja igual. Exemplos:

    - "CHOPP 20L" -> "20L"; "CHOPP 30L" -> "30L";
    - "ARROZ 5KG" -> "5KG"; "1,5KG" -> "1.5KG"; "100G" -> "100G";
    - "12X350ML" -> "4.2L" (o multiplicador entra no total).

    A unidade é normalizada para a base (ML/G) e exibida na forma mais legível:
    >= 1000 ML vira L, >= 1000 G vira KG. Quando a descrição cita mais de uma
    medida (ex.: "SACO 200L 5KG"), as medidas canônicas são ordenadas e unidas
    por "+". Sem medida, devolve None (o item continua no grupo normal).
    """
    texto = str(descricao if descricao is not None else "").upper()
    medidas = []
    for encontro in _VOLUME_RE.finditer(texto):
        quantidade = _decimal_medida(encontro.group("quantidade"))
        if quantidade is None:
            continue
        multiplicador = _decimal_medida(encontro.group("multiplicador"))
        if multiplicador is not None:
            quantidade *= multiplicador
        unidade = encontro.group("unidade").upper()

        if unidade.startswith("MILILITRO") or unidade in {"ML"}:
            mililitros = quantidade
            if mililitros >= 1000:
                medidas.append(f"{_formatar_medida(mililitros / 1000)}L")
            else:
                medidas.append(f"{_formatar_medida(mililitros)}ML")
        elif unidade.startswith("LITRO") or unidade in {"L", "LT", "LTS"}:
            mililitros = quantidade * 1000
            if mililitros >= 1000:
                medidas.append(f"{_formatar_medida(mililitros / 1000)}L")
            else:
                medidas.append(f"{_formatar_medida(mililitros)}ML")
        elif unidade.startswith("MILIGRAMA") or unidade in {"MG", "MGS"}:
            gramas = quantidade / 1000
            if gramas < 1:
                medidas.append(f"{_formatar_medida(gramas * 1000)}MG")
            else:
                medidas.append(f"{_formatar_medida(gramas)}G")
        elif unidade.startswith(("QUILOGRAMA", "QUILO")) or unidade in {"KG", "KGS"}:
            gramas = quantidade * 1000
            if gramas >= 1000:
                medidas.append(f"{_formatar_medida(gramas / 1000)}KG")
            else:
                medidas.append(f"{_formatar_medida(gramas)}G")
        elif unidade.startswith("GRAMA") or unidade in {"G", "GR", "GRS"}:
            gramas = quantidade
            if gramas >= 1000:
                medidas.append(f"{_formatar_medida(gramas / 1000)}KG")
            else:
                medidas.append(f"{_formatar_medida(gramas)}G")
    if not medidas:
        return None
    return "+".join(sorted(set(medidas)))


@lru_cache(maxsize=16384)
def _aplicar_volume_na_chave(chave, descricao) -> str:
    """Acrescenta/garante o sufixo _VOL_<medida> na chave do grupo.

    É chamada na geração da chave base E depois das regras de-para. Assim nem
    uma regra "juntar" criada manualmente consegue fundir 20L com 30L: cada
    item recebe de volta o sufixo da SUA própria medida.
    """
    volume = extrair_volume(descricao)
    if not volume:
        return str(chave or "")
    base = _VOLUME_SUFIXO_RE.sub("", str(chave or "").rstrip("_"))
    return f"{base}_VOL_{volume}"


def _aplicar_embalagem_na_chave(chave, unidade_compra="", descricao="") -> str:
    """Acrescenta/garante o sufixo `_EMB_<token>` na chave do grupo.

    A embalagem é IDENTIDADE do produto, não fator de conversão: CX24 e UN são
    grupos diferentes. UN/KG/L não geram sufixo (preserva as chaves antigas);
    qualquer embalagem declarada separa o item.
    """
    token = aprendizado.embalagem_para_chave(unidade_compra, descricao)
    base = _EMB_SUFIXO_RE.sub("", str(chave or "").rstrip("_"))
    if not token:
        return base
    return f"{base}_EMB_{token}"


@lru_cache(maxsize=8192)
def _remover_numero_controle(descricao) -> str:
    """Remove sufixos de controle do fornecedor ("- 1", "- 2") ANTES de agrupar.

    O texto original continua intacto em `itens_nota.descricao_produto` (é o que
    aparece na exportação e na aprovação da Auditoria). Só a chave/nome do grupo
    usa a versão sem o número, para "POLENTA PALITO - 1" e "POLENTA PALITO - 2"
    caírem no mesmo produto.
    """
    original = str(descricao or "").strip()
    sem_controle = _NUMERO_CONTROLE_FINAL_RE.sub("", original).strip()
    return sem_controle or original


@lru_cache(maxsize=8192)
def _marca_para_chave(marca) -> Optional[str]:
    """Token alfanumérico da marca; None quando ela não foi identificada."""
    if marca is None or str(marca).strip().upper() in {"NAN", "<NA>", "NONE", "NULL"}:
        return None
    texto = _limpar_texto_relatorio(marca)
    if not texto or texto in MARCAS_NAO_IDENTIFICADAS:
        return None
    token = re.sub(r"[^A-Z0-9]", "", texto)
    if not token or token in {"NAOIDENTIFICADA", "SEMARCA"}:
        return None
    return token


@lru_cache(maxsize=16384)
def _aplicar_marca_na_chave(chave, marca) -> str:
    """Acrescenta/garante o sufixo `_MARCA_<token>` na chave do grupo.

    Marca nula ou "NÃO IDENTIFICADA" não gera sufixo: nesse caso o produto é
    agrupado pelo nome base (EAN ou duas primeiras palavras), como antes.
    """
    base = _MARCA_SUFIXO_RE.sub("", str(chave or "").rstrip("_"))
    token = _marca_para_chave(marca)
    if not token:
        return base
    return f"{base}_MARCA_{token}"


def _aplicar_contexto_na_chave(
    chave, descricao, marca=None, unidade_compra=""
) -> str:
    """Reaplica marca + volume + embalagem no FINAL da chave, após as regras.

    Produto + marca, volume/peso e embalagem são regras obrigatórias de negócio:
    nem uma regra "juntar" criada à mão pode fundir Arrico com El Chef, 20L com
    30L ou CX24 com UN.
    """
    base = str(chave or "").rstrip("_")
    base = _EMB_SUFIXO_RE.sub("", base).rstrip("_")
    base = _VOLUME_SUFIXO_RE.sub("", base).rstrip("_")
    base = _MARCA_SUFIXO_RE.sub("", base).rstrip("_")
    base = _aplicar_marca_na_chave(base, marca)
    base = _aplicar_volume_na_chave(base, descricao)
    return _aplicar_embalagem_na_chave(base, unidade_compra, descricao)


def _bases_legado_chave(chave) -> list:
    """Formas antigas da chave (sem marca, volume e/ou embalagem).

    Regras e aprovações gravadas antes das regras de marca/volume/embalagem
    usavam a chave sem esses sufixos. Tentar as formas antigas mantém o histórico
    válido sem afrouxar a regra nova: o retorno da `_chave_final_relatorio`
    sempre reaplica marca + volume + embalagem da descrição atual.
    """
    bruta = str(chave or "").rstrip("_")
    sem_emb = _EMB_SUFIXO_RE.sub("", bruta).rstrip("_")
    sem_volume = _VOLUME_SUFIXO_RE.sub("", bruta).rstrip("_")
    sem_marca = _MARCA_SUFIXO_RE.sub("", bruta).rstrip("_")
    variantes = [bruta, sem_emb, sem_volume, sem_marca]
    for base in (sem_emb, sem_volume, sem_marca):
        sem_volume_emb = _VOLUME_SUFIXO_RE.sub("", base).rstrip("_")
        sem_marca_emb = _MARCA_SUFIXO_RE.sub("", base).rstrip("_")
        sem_ambos = _MARCA_SUFIXO_RE.sub("", sem_volume_emb).rstrip("_")
        variantes.extend([sem_volume_emb, sem_marca_emb, sem_ambos])
    bases = []
    for base in variantes:
        if base and base not in bases:
            bases.append(base)
    return bases


@lru_cache(maxsize=16384)
def _chave_produto_relatorio(ean, descricao, marca=None, unidade_compra="") -> str:
    """Chave inteligente do produto — idêntica à de app.py (gerar_chave_produto).

    Casar a MESMA chave nas duas pontas é o que permite a coluna "Mercado" comparar
    exatamente o mesmo produto entre lojas e os relatórios enxergarem os agrupamentos
    aprovados na auditoria (regras juntar/mover, grupos NOVO_ etc.).

    Regras obrigatórias da chave:
    - PRODUTO + MARCA: "POLENTA PALITO ARRICO" e "POLENTA PALITO EL CHEF"
      geram grupos diferentes, mesmo com o mesmo EAN;
    - marca nula/"NÃO IDENTIFICADA" não gera sufixo e o nome base agrupa;
    - volume/peso como sufixo: "CHOPP 20L" e "CHOPP 30L" nunca se fundem;
    - embalagem como sufixo: "ORIGINAL 600ML CX C/24" e "... UN" nunca se fundem;
    - números de controle ("- 1", "- 2") são removidos antes do nome base.
    """
    descricao_base = _remover_numero_controle(descricao)
    desc = _limpar_texto_relatorio(descricao_base)
    extras = [termo for termo in TERMOS_EXCLUSIVOS_PRODUTO if termo in desc]
    subtipo_extra = "_".join(extras)
    if not _tem_ean_valido_relatorio(ean):
        palavras = [palavra for palavra in desc.split() if len(palavra) > 2]
        subtipo = "_".join(palavras[:2]) if palavras else "GERAL"
        chave = f"SEM_EAN_{subtipo}_{subtipo_extra}"
    else:
        chave = f"EAN_{str(ean).strip()}_{subtipo_extra}"
    chave = _aplicar_marca_na_chave(chave, marca)
    chave = _aplicar_volume_na_chave(chave, descricao_base)
    return _aplicar_embalagem_na_chave(chave, unidade_compra, descricao_base)


def _mapas_regras_relatorio(db: Session):
    """Separa as regras de grupo (juntar) das regras de descrição (mover)."""
    mapa_regras, mapa_descricoes = {}, {}
    for regra in db.query(RegraAgrupamento).all():
        origem = (regra.chave_origem or "").strip()
        destino = (regra.chave_destino or "").strip()
        if not origem or not destino:
            continue
        if origem.startswith("DESC_"):
            mapa_descricoes[origem] = destino
        else:
            mapa_regras[origem] = destino
    return mapa_regras, mapa_descricoes


def _resolver_chave_relatorio(chave, mapa_regras):
    """Aplica as regras de grupo em cadeia, com proteção contra ciclos.

    Regras gravadas antes das regras de marca/volume usavam a chave sem esses
    sufixos (e algumas terminavam em `_`). Se a chave nova não tiver uma regra
    exata, tentamos as formas antigas para a correção continuar valendo.
    """
    atual, visitados = chave, set()
    while atual not in visitados:
        # Marca o estado ATUAL antes de resolver a forma legada. Sem isso, uma
        # regra antiga sem `_VOL_`/`_MARCA_` que aponta de volta para a chave nova
        # fazia o laço alternar entre as duas para sempre.
        visitados.add(atual)
        candidato = atual
        if candidato not in mapa_regras:
            for base in _bases_legado_chave(atual)[1:]:
                for alternativa in (base, f"{base}_"):
                    if alternativa in mapa_regras:
                        candidato = alternativa
                        break
                if candidato in mapa_regras:
                    break
        if candidato not in mapa_regras:
            return atual
        visitados.add(candidato)
        atual = mapa_regras[candidato]
    return atual


def _chave_grupo_novo_relatorio(descricao) -> str:
    """Chave NOVO_... de um grupo criado para isolar uma única descrição.

    É idêntica à de app.py (`chave_grupo_novo`): a mesma descrição reaproveita
    a mesma chave e o hash curto evita colisão entre textos parecidos. Os sufixos
    de marca e volume são acrescentados por `_aplicar_contexto_na_chave` na chave final.
    """
    texto = str(descricao or "").strip()
    base = _limpar_texto_relatorio(texto)[:60].strip() or "PRODUTO"
    resumo = hashlib.sha1(texto.encode("utf-8")).hexdigest()[:6].upper()
    return f"NOVO_{base.replace(' ', '_')}_{resumo}"


def _chave_final_relatorio(
    chave_base,
    descricao,
    mapa_regras,
    mapa_descricoes,
    marca=None,
    unidade_compra="",
):
    """Chave do grupo já com as regras de grupo (juntar) e de descrição (mover).

    A marca, o volume/peso e a embalagem são reaplicados no FINAL, depois das
    regras: mesmo que uma regra "juntar" aponte Arrico para o grupo do El Chef
    (ou 20L para o 30L, ou CX24 para UN), cada descrição volta para a chave do
    seu próprio produto/medida/embalagem.
    """
    chave = _resolver_chave_relatorio(chave_base, mapa_regras)
    visitados = set()
    while True:
        candidatas = []
        # Compatibilidade: regras gravadas antes das regras de marca/volume
        # usavam a chave sem os sufixos. Elas continuam valendo; marca e volume
        # são reaplicados no retorno.
        for base in _bases_legado_chave(chave):
            for base_regra in (base, f"{base}_"):
                candidata = f"DESC_{base_regra}_{_limpar_texto_relatorio(descricao)}"
                if candidata not in candidatas:
                    candidatas.append(candidata)
        aplicada = None
        for chave_desc in candidatas:
            if chave_desc in mapa_descricoes and chave_desc not in visitados:
                aplicada = chave_desc
                break
        if aplicada:
            visitados.add(aplicada)
            chave = _resolver_chave_relatorio(mapa_descricoes[aplicada], mapa_regras)
            continue
        return _aplicar_contexto_na_chave(
            chave, descricao, marca, unidade_compra
        )


def _mapa_auditoria_relatorio(db: Session):
    """Chaves DESC_... da auditoria, separadas por estado e motivo.

    Devolve `(aprovadas, pendentes manuais, pendentes aberração, aberrações validadas)`:

    - `auditado=True` = aprovação definitiva. Quando o motivo é "aberrante" a chave entra
      também em `aberrações validadas`: o auditor confirmou que a variação brusca é real,
      então o item volta a contar como aumento/economia;
    - `auditado=False` + motivo "manual" = pendência que sai dos relatórios;
    - `auditado=False` + motivo "aberrante" = variação brusca aguardando validação
      (continua aparecendo na tabela, mas fora dos totais).
    Registros antigos sem motivo são tratados como manuais.
    """
    aprovadas, manuais, aberracoes, validadas = set(), set(), set(), set()
    for registro in db.query(ItemAuditado).all():
        chave = str(registro.chave_identificacao or "").strip()
        if not chave.startswith("DESC_"):
            continue
        motivo = str(registro.motivo or MOTIVO_PENDENCIA_MANUAL)
        if registro.auditado:
            aprovadas.add(chave)
            if motivo == MOTIVO_PENDENCIA_ABERRACAO:
                validadas.add(chave)
        elif motivo == MOTIVO_PENDENCIA_ABERRACAO:
            aberracoes.add(chave)
        else:
            manuais.add(chave)
    return aprovadas, manuais, aberracoes, validadas


def _grupos_produto_relatorio(
    db: Session, mapa_regras, mapa_descricoes, empresa_ids=None
):
    """Grupos de produto de TODO o histórico: {chave: {descricoes, ean, ncm, ...}}.

    A aba de Auditoria monta os grupos sobre todas as compras da base (não só a janela do
    relatório), então o backend precisa olhar o mesmo universo para chegar às mesmas
    pendências. `empresa_ids` (opcional) recorta o universo às lojas da conta que está
    simulando o alerta — sem ele o comportamento dos relatórios permanece global.

    A consulta é ordenada por id de propósito: a descrição padrão do grupo é a
    da compra mais antiga e, no empate de datas, vale a primeira que apareceu — igual ao
    `sort_values(..., kind="stable")` do front. O NCM é o primeiro não vazio encontrado no
    grupo e alimenta o raio-x exportado para Excel; a marca é a do primeiro item e forma,
    junto com o produto, a chave do agrupamento.
    """
    consulta = (
        db.query(
            ItemNota.id,
            ItemNota.ean,
            ItemNota.descricao_produto,
            ItemNota.ncm,
            ItemNota.marca,
            ItemNota.unidade_compra,
            NotaFiscal.data_emissao,
        )
        .join(NotaFiscal, ItemNota.nota_id == NotaFiscal.id)
        .order_by(ItemNota.id)
    )
    if empresa_ids is not None:
        if not empresa_ids:
            return {}
        consulta = consulta.filter(NotaFiscal.empresa_id.in_(empresa_ids))
    linhas = consulta.all()
    grupos = {}
    for indice, linha in enumerate(linhas):
        descricao = str(linha.descricao_produto or "")
        if not descricao.strip():
            continue
        marca = str(linha.marca or "").strip() or None
        if marca and _marca_para_chave(marca) is None:
            marca = None
        chave = _chave_final_relatorio(
            _chave_produto_relatorio(
                linha.ean, descricao, marca, linha.unidade_compra
            ),
            descricao,
            mapa_regras,
            mapa_descricoes,
            marca,
            linha.unidade_compra,
        )
        data = linha.data_emissao
        ordem = (data is None, data or date.min, indice)
        ncm = str(linha.ncm or "").strip()
        grupo = grupos.get(chave)
        if grupo is None:
            grupos[chave] = {
                "ean": linha.ean,
                "ncm": ncm,
                "marca": marca,
                "descricoes": [descricao],
                "descricao_padrao": descricao,
                "ordem_padrao": ordem,
            }
            continue
        if descricao not in grupo["descricoes"]:
            grupo["descricoes"].append(descricao)
        if not grupo["ncm"] and ncm:
            grupo["ncm"] = ncm
        if not grupo.get("marca") and marca:
            grupo["marca"] = marca
        if ordem < grupo["ordem_padrao"]:
            grupo["ordem_padrao"] = ordem
            grupo["descricao_padrao"] = descricao
    return grupos



def _descricoes_pendentes_auditoria(
    db: Session,
    mapa_regras,
    mapa_descricoes,
    aprovadas,
    empresa_ids=None,
    chaves_interesse=None,
):
    """Chaves DESC_... que a aba de Auditoria exibe como "Pendente de Aprovação".

    Mesmo critério do front (`montar_grupos` + `grupo_e_excecao`): agrupa todo o histórico
    pela chave final do produto, identifica os grupos de exceção e separa as descrições
    ainda não aprovadas em ItemAuditado.

    `empresa_ids` limita os grupos às lojas da conta e `chaves_interesse` evita comparar
    descrições de grupos que nem aparecem na janela do alerta. Ambos são opcionais: sem
    eles o resultado é o mesmo de antes (universo global), usado pelos relatórios.
    """
    if chaves_interesse is not None and not chaves_interesse:
        return set()
    pendentes = set()
    for chave, grupo in _grupos_produto_relatorio(
        db, mapa_regras, mapa_descricoes, empresa_ids=empresa_ids
    ).items():
        if chaves_interesse is not None and chave not in chaves_interesse:
            continue
        for descricao in regras_auditoria.descricoes_pendentes(
            chave,
            grupo["descricoes"],
            regras_auditoria.tem_ean_valido(grupo["ean"]),
            aprovadas,
            grupo["descricao_padrao"],
        ):
            # Compatibilidade: aprovações feitas antes do sufixo `_EMB_` (e das
            # regras de marca/volume) continuam valendo para a descrição.
            chaves_legadas = {
                regras_auditoria.chave_descricao(base, descricao)
                for base in _bases_legado_chave(chave)
            }
            if chaves_legadas & aprovadas:
                continue
            pendentes.add(regras_auditoria.chave_descricao(chave, descricao))
    return pendentes


def _inicio_periodo(data_ref: date, periodicidade: str) -> date:
    """Primeiro dia do período (a chave de agrupamento) para a periodicidade escolhida."""
    if periodicidade == "diaria":
        return data_ref
    if periodicidade == "semanal":
        return data_ref - timedelta(days=data_ref.weekday())
    if periodicidade == "quinzenal":
        return date(data_ref.year, data_ref.month, 1 if data_ref.day <= 15 else 16)
    if periodicidade == "bimestral":
        return date(data_ref.year, ((data_ref.month - 1) // 2) * 2 + 1, 1)
    if periodicidade == "trimestral":
        return date(data_ref.year, ((data_ref.month - 1) // 3) * 3 + 1, 1)
    return date(data_ref.year, data_ref.month, 1)


def _rotulo_periodo(inicio: date, periodicidade: str) -> str:
    """Rótulo humano do período (o que aparece na primeira coluna das tabelas)."""
    if periodicidade == "diaria":
        return f"{inicio:%d/%m/%Y}"
    if periodicidade == "semanal":
        return f"Semana de {inicio:%d/%m/%Y}"
    if periodicidade == "quinzenal":
        quinzena = "1ª" if inicio.day == 1 else "2ª"
        return f"{quinzena} quinzena de {inicio:%m/%Y}"
    if periodicidade == "bimestral":
        return f"{(inicio.month - 1) // 2 + 1}º bimestre de {inicio.year}"
    if periodicidade == "trimestral":
        return f"{(inicio.month - 1) // 3 + 1}º trimestre de {inicio.year}"
    return f"{inicio:%m/%Y}"


def _data_referencia_relatorios(db: Session):
    """Última data de emissão da base.

    A referência é a MAIOR data de emissão (e não "hoje") para os filtros de período
    continuarem úteis quando a importação das notas está alguns dias atrasada.
    """
    return db.query(func.max(NotaFiscal.data_emissao)).scalar()


def _carregar_itens_relatorio(
    db: Session,
    data_inicio=None,
    data_fim=None,
    estatisticas=None,
    empresa_ids=None,
    auditoria_empresa_ids=None,
    ordem_descendente=False,
):
    """Itens das notas no período, já com loja/fornecedor/data e a chave do grupo.

    Uma única consulta (nada de query por linha): é o que mantém os relatórios
    rápidos e o Streamlit livre para renderizar.

    `empresa_ids` filtra as notas das lojas no SQL. O simulador de alertas passa
    também `data_inicio`/`data_fim` (a janela escolhida) e `ordem_descendente=True`,
    de modo que o banco devolve só as compras recentes das lojas da conta — a
    "penúltima" compra é escolhida nessa lista já reduzida. `auditoria_empresa_ids`
    mantém o cálculo das pendências restrito às lojas da conta sem ampliar a
    consulta dos itens.

    Aplica as regras de integridade dos relatórios (ver bloco "Integridade dos relatórios"):
    - descrição pendente de aprovação na auditoria não entra nos cálculos (as pendências
      de aberração continuam visíveis na tabela de Impacto, mas nunca somam nos totais);
    - item bonificado/doação/brinde não entra na média de preço: CFOP 5910/6910 ou
      valor unitário R$ 0,00.
    `estatisticas` (dict opcional) devolve quantos itens foram ignorados por cada regra.
    """
    consulta = (
        db.query(
            ItemNota.id,
            ItemNota.ean,
            ItemNota.descricao_produto,
            ItemNota.valor_unitario,
            ItemNota.quantidade,
            ItemNota.valor_total,
            ItemNota.cfop,
            # NCM e marca alimentam o agrupamento Produto + Marca e a consulta
            # paralela de marcas substitutas (tooltip da coluna Mercado).
            ItemNota.ncm,
            ItemNota.marca,
            ItemNota.unidade_compra,
            # Memória de embalagem: o alerta por e-mail compara o preço na UNIDADE-BASE
            # (CX24 -> unidade) e calcula o consumo em unidades-base. `valor_unitario`
            # e `quantidade` continuam sendo o valor cru do XML.
            ItemNota.quantidade_base,
            ItemNota.valor_unitario_base,
            ItemNota.unidade_base,
            # Id da nota: o relatório diário/semanal/mensal trabalha por LOTE de notas
            # (ordem de importação), não por data de emissão.
            ItemNota.nota_id,
            NotaFiscal.data_emissao,
            Empresa.nome_loja,
            # CNPJ da loja: usado para recortar os alertas por loja (o nome fantasia
            # pode repetir entre CNPJs) e não entra em nenhum cálculo de preço.
            Empresa.cnpj,
            Fornecedor.nome,
        )
        .join(NotaFiscal, ItemNota.nota_id == NotaFiscal.id)
        .join(Empresa, NotaFiscal.empresa_id == Empresa.id)
        .join(Fornecedor, ItemNota.fornecedor_id == Fornecedor.id)
    )
    if empresa_ids is not None:
        if not empresa_ids:
            return []
        consulta = consulta.filter(NotaFiscal.empresa_id.in_(empresa_ids))
    if data_inicio is not None:
        consulta = consulta.filter(NotaFiscal.data_emissao >= data_inicio)
    if data_fim is not None:
        consulta = consulta.filter(NotaFiscal.data_emissao <= data_fim)
    # A ordem descendente é usada pelos alertas: a compra mais recente aparece
    # primeiro e o SQLite não precisa ordenar todo o histórico em Python.
    # Não há LIMIT fixo por produto porque a chave final do grupo depende das regras
    # da Auditoria (marca/volume/embalagem). O recorte por loja + janela já reduz a
    # lista ao período e `linhas_de_variacao` usa só a última e a penúltima compra.
    if ordem_descendente:
        consulta = consulta.order_by(
            NotaFiscal.data_emissao.desc(), ItemNota.id.desc()
        )
    else:
        consulta = consulta.order_by(ItemNota.id)
    linhas = consulta.all()

    mapa_regras, mapa_descricoes = _mapas_regras_relatorio(db)
    aprovadas, pendentes_manuais, pendentes_aberracoes, _validadas = _mapa_auditoria_relatorio(db)

    ignorados_bonificados = 0
    ignorados_cfop = 0
    registros = []
    chaves_interesse = set()
    # Primeira passada: só prepara a chave do grupo e o conjunto de produtos que
    # realmente aparecem nesta janela. As pendências da Auditoria passam a comparar
    # somente esses grupos (o que evita o O(n²) do histórico inteiro no alerta).
    for linha in linhas:
        if linha.data_emissao is None:
            continue
        valor = float(linha.valor_unitario or 0.0)
        if valor <= 0:
            # Bonificação/doação/brinde: não distorce a média de preço.
            ignorados_bonificados += 1
            continue
        if str(linha.cfop or "").strip() in CFOPS_BONIFICACAO:
            # CFOP 5910/6910 = remessa em bonificação/doação: o valor é apenas o custo
            # de referência, não o preço pago. Fora da média (regra do HEINEKEN 600ml).
            ignorados_cfop += 1
            continue
        marca = str(linha.marca or "").strip() or None
        if marca and _marca_para_chave(marca) is None:
            marca = None
        chave = _chave_final_relatorio(
            _chave_produto_relatorio(
                linha.ean,
                linha.descricao_produto,
                marca,
                linha.unidade_compra,
            ),
            linha.descricao_produto,
            mapa_regras,
            mapa_descricoes,
            marca,
            linha.unidade_compra,
        )
        chave_descricao = regras_auditoria.chave_descricao(chave, linha.descricao_produto)
        chaves_interesse.add(chave)
        registros.append((linha, valor, chave, chave_descricao))

    # Aberração pendente NÃO esconde o produto: ela aparece marcada na tabela de Impacto.
    pendentes = (pendentes_manuais - pendentes_aberracoes) | _descricoes_pendentes_auditoria(
        db,
        mapa_regras,
        mapa_descricoes,
        aprovadas,
        empresa_ids=(
            auditoria_empresa_ids
            if auditoria_empresa_ids is not None
            else empresa_ids
        ),
        chaves_interesse=chaves_interesse,
    )
    ignorados_pendentes = 0
    itens = []
    for linha, valor, chave, chave_descricao in registros:
        if chave_descricao in pendentes:
            ignorados_pendentes += 1
            continue
        itens.append(
            {
                "id": linha.id,
                "chave": chave,
                "chave_descricao": chave_descricao,
                "ean": str(linha.ean or ""),
                "descricao": str(linha.descricao_produto or ""),
                "data": linha.data_emissao,
                "loja": str(linha.nome_loja or "Sem loja"),
                "cnpj": re.sub(r"\D", "", str(linha.cnpj or "")),
                "fornecedor": str(linha.nome or ""),
                "valor": valor,
                "quantidade": float(linha.quantidade or 0.0),
                "total": float(linha.valor_total or 0.0),
                "cfop": str(linha.cfop or ""),
                "ncm": regras_marcas.ncm_normalizado(linha.ncm),
                "marca": str(linha.marca or "").strip() or None,
                "unidade_compra": str(linha.unidade_compra or "").strip().upper(),
                "unidade_base": str(linha.unidade_base or "").strip().upper(),
                "quantidade_base": float(linha.quantidade_base or 0.0),
                "valor_unitario_base": float(linha.valor_unitario_base or 0.0),
                "nota_id": linha.nota_id,
            }
        )
    if estatisticas is not None:
        estatisticas["pendentes_auditoria"] = ignorados_pendentes
        estatisticas["bonificados"] = ignorados_bonificados + ignorados_cfop
        estatisticas["bonificados_cfop"] = ignorados_cfop
        estatisticas["aberracoes_auditoria"] = len(pendentes_aberracoes)
    return itens


def _nome_produto_por_chave(itens):
    """Descrição mais frequente de cada grupo (o rótulo exibido nas tabelas)."""
    contagem = {}
    for item in itens:
        descricao = item["descricao"].strip() or "Produto sem descrição"
        registro = contagem.setdefault(item["chave"], {})
        registro[descricao] = registro.get(descricao, 0) + 1
    nomes = {}
    for chave, descricoes in contagem.items():
        nomes[chave] = sorted(descricoes.items(), key=lambda par: (-par[1], -len(par[0])))[0][0]
    return nomes


def _filtrar_produto_relatorio(itens, produto):
    """Restringe os itens ao produto informado (descrição exata, EAN ou chave do grupo)."""
    alvo = str(produto or "").strip()
    if not alvo:
        return itens, "Todos os produtos"
    alvo_limpo = _limpar_texto_relatorio(alvo)
    chaves = {
        item["chave"]
        for item in itens
        if item["chave"] == alvo
        or (item["ean"] and item["ean"].strip() == alvo)
        or item["descricao"].strip().upper() == alvo.upper()
        or _limpar_texto_relatorio(item["descricao"]) == alvo_limpo
    }
    if not chaves:
        return [], alvo
    filtrados = [item for item in itens if item["chave"] in chaves]
    nomes = _nome_produto_por_chave(filtrados)
    rotulo = ", ".join(sorted({nomes.get(chave, alvo) for chave in chaves}))
    return filtrados, rotulo


def _normalizar_lojas_relatorio(valor, itens):
    """Lojas do usuário. Sem parâmetro (ou "Minhas Lojas (Todas)") = todas as lojas do banco.

    A seleção do usuário é preservada mesmo quando a loja não tem compra no período:
    assim a coluna dela continua aparecendo (vazia) no comparativo.
    """
    candidatas = [valor] if isinstance(valor, str) else list(valor or [])
    pedidas = []
    for loja in candidatas:
        nome = str(loja or "").strip()
        if nome and nome.lower() not in {existente.lower() for existente in pedidas}:
            pedidas.append(nome)
    lojas_disponiveis = sorted({item["loja"] for item in itens})
    if not pedidas or {nome.lower() for nome in pedidas} & LOJAS_TODAS_ALIASES:
        return lojas_disponiveis
    nomes_canonicos = {nome.lower(): nome for nome in lojas_disponiveis}
    return [nomes_canonicos.get(nome.lower(), nome) for nome in pedidas]


def _media_preco(itens):
    """Preço unitário médio (aritmético) — None quando não há compra no período."""
    if not itens:
        return None
    return round(sum(item["valor"] for item in itens) / len(itens), 4)


def _detalhar_compras(itens):
    """Compras (data, fornecedor e valor) que formam a média — só das lojas do usuário."""
    return [
        {
            "data": item["data"].isoformat(),
            "fornecedor": item["fornecedor"] or "Fornecedor não informado",
            "valor": round(item["valor"], 4),
            "quantidade": round(item["quantidade"], 4),
        }
        for item in sorted(itens, key=lambda item: (item["data"], item["id"]))
    ]


def _grupos_para_marcas(itens, limite=LIMITE_PRODUTOS_MARCAS):
    """Descrições, marcas e NCM de cada grupo (chave) — insumo da consulta de marcas.

    `descricoes` mapeia cada descrição do grupo para a sua marca (None quando o NCM não
    permitiu inferir): é o que permite comparar as descrições SEM o nome da marca.

    O teto (`limite`) mantém a consulta paralela barata em relatórios com milhares de
    produtos: os primeiros grupos (ordem de aparição) é que recebem marcas substitutas.
    """
    grupos = {}
    for item in itens:
        registro = grupos.setdefault(
            item["chave"], {"descricoes": {}, "marcas": set(), "ncm": ""}
        )
        descricao = str(item["descricao"] or "").strip()
        marca = str(item.get("marca") or "").strip().upper() or None
        if descricao:
            if registro["descricoes"].get(descricao) is None:
                registro["descricoes"][descricao] = marca
        if marca:
            registro["marcas"].add(marca)
        if not registro["ncm"]:
            registro["ncm"] = str(item.get("ncm") or "")
    return {chave: grupos[chave] for chave in list(grupos)[:limite]}


def _marcas_substitutas(db: Session, itens, data_inicio=None, data_fim=None) -> dict:
    """CONSULTA PARALELA: outras marcas do mesmo NCM com descrição parecida (> 80%).

    Devolve ``{chave do produto: [{marca, preco_medio, registros, descricao, similaridade}]}``.

    Regras (nenhuma delas altera a Média de Mercado, que continua sendo `_media_preco` do
    EAN/descrição exata do produto — este resultado vai SÓ para o tooltip do front):

    - mesmo NCM do produto;
    - descrição do MESMO PRODUTO acima de `SIMILARIDADE_MINIMA_MARCA` (80%) e texto
      diferente do nosso — a comparação tira o nome da marca dos dois lados
      (`marcas.similaridade_produto`), senão "YOKI" x "MATHEUS" derrubaria a similaridade
      de um item curto e a substituição nunca apareceria;
    - marca diferente das marcas já presentes no nosso grupo;
    - preço unitário > 0 e sem CFOP de bonificação/doação (mesma regra dos relatórios).

    As linhas candidatas são agrupadas por (descrição, marca) antes da comparação: assim
    a similaridade é calculada uma vez por texto, e não por item da nota.
    """
    grupos = _grupos_para_marcas(itens)
    if not grupos:
        return {}
    ncms = {grupo["ncm"] for grupo in grupos.values()}
    ncms = {ncm for ncm in ncms if ncm and ncm != regras_marcas.NCM_VAZIO}
    if not ncms:
        return {}

    consulta = (
        db.query(
            ItemNota.ncm,
            ItemNota.descricao_produto,
            ItemNota.marca,
            ItemNota.valor_unitario,
        )
        .join(NotaFiscal, ItemNota.nota_id == NotaFiscal.id)
        .filter(ItemNota.ncm.in_(sorted(ncms)))
        .filter(ItemNota.valor_unitario > 0)
        .filter(or_(ItemNota.cfop.is_(None), ~ItemNota.cfop.in_(CFOPS_BONIFICACAO)))
    )
    if data_inicio is not None:
        consulta = consulta.filter(NotaFiscal.data_emissao >= data_inicio)
    if data_fim is not None:
        consulta = consulta.filter(NotaFiscal.data_emissao <= data_fim)

    por_ncm = {}
    for linha in consulta.all():
        marca = str(linha.marca or "").strip().upper()
        descricao = str(linha.descricao_produto or "").strip()
        if not marca or not descricao:
            continue
        registro = por_ncm.setdefault(regras_marcas.ncm_normalizado(linha.ncm), {})
        registro.setdefault((descricao, marca), []).append(float(linha.valor_unitario or 0.0))

    resultado = {}
    for chave, grupo in grupos.items():
        candidatos = por_ncm.get(grupo["ncm"])
        if not candidatos:
            continue
        nossas = [
            (
                descricao,
                marca,
                set(regras_marcas.tokens(descricao)),
                regras_marcas.normalizar(descricao),
            )
            for descricao, marca in grupo["descricoes"].items()
            if str(descricao or "").strip()
        ]
        if not nossas:
            continue
        textos_nossos = {registro[3] for registro in nossas}
        acumulado = {}
        for (descricao, marca), valores in candidatos.items():
            if marca in grupo["marcas"]:
                continue  # é a mesma marca do nosso produto
            if regras_marcas.normalizar(descricao) in textos_nossos:
                continue  # mesma descrição = mesmo produto (não é substituição)
            tokens_candidato = set(regras_marcas.tokens(descricao))
            similar = 0.0
            for texto, marca_nossa, tokens_nossos, _normalizada in nossas:
                if not (tokens_candidato & tokens_nossos):
                    continue  # nenhuma palavra em comum: nem vale calcular a similaridade
                # Compara o PRODUTO (sem o nome da marca): "MANDIOCA ... YOKI" x
                # "MANDIOCA ... MATHEUS" é o mesmo item, só muda a marca.
                similar = max(
                    similar,
                    regras_marcas.similaridade_produto(descricao, marca, texto, marca_nossa),
                )
            if similar <= SIMILARIDADE_MINIMA_MARCA:
                continue
            registro = acumulado.setdefault(
                marca, {"valores": [], "descricao": descricao, "similaridade": similar}
            )
            registro["valores"].extend(valores)
            registro["similaridade"] = max(registro["similaridade"], similar)

        if not acumulado:
            continue
        lista = [
            {
                "marca": marca,
                "preco_medio": round(sum(dados["valores"]) / len(dados["valores"]), 4),
                "registros": len(dados["valores"]),
                "descricao": dados["descricao"],
                "similaridade": round(dados["similaridade"], 3),
            }
            for marca, dados in acumulado.items()
        ]
        lista.sort(key=lambda item: (-item["registros"], item["preco_medio"]))
        resultado[chave] = lista[:LIMITE_MARCAS_POR_PRODUTO]
    return resultado


# ──────────────────────────────────────────────────────────────────────────────
# Exportação da base COMPLETA (raio-x): todos os grupos com 2+ descrições únicas.
# A varredura usa o mesmo agrupamento dos relatórios (chave inteligente + regras
# juntar/mover), então o Excel mostra exatamente o que a Auditoria vê — inclusive
# os grupos que não cabem na tela ou que já estão aprovados.
# ──────────────────────────────────────────────────────────────────────────────
COLUNAS_EXPORTACAO_GRUPOS = [
    "Produto Principal",
    "EAN",
    "NCM",
    "Lista de Descrições",
]
NOME_PLANILHA_GRUPOS = "Grupos com variações"


def _linhas_grupos_com_variacoes(db: Session):
    """Linhas do raio-x: só grupos com 2+ descrições únicas, na ordem do Excel.

    Devolve uma lista de dicts com as colunas de `COLUNAS_EXPORTACAO_GRUPOS`.
    EAN inválido vira "SEM EAN" (legível para auditoria) e a lista de descrições
    é separada por "; " (todas as variações do grupo, sem repetir).
    """
    mapa_regras, mapa_descricoes = _mapas_regras_relatorio(db)
    grupos = _grupos_produto_relatorio(db, mapa_regras, mapa_descricoes)

    linhas = []
    for grupo in grupos.values():
        descricoes, vistos = [], set()
        for descricao in grupo.get("descricoes") or []:
            texto = str(descricao or "").strip()
            chave = texto.casefold()
            if not texto or chave in vistos:
                continue
            vistos.add(chave)
            descricoes.append(texto)
        if len(descricoes) < 2:
            continue

        ean = str(grupo.get("ean") or "").strip()
        if not _tem_ean_valido_relatorio(ean):
            ean = "SEM EAN"
        ncm = str(grupo.get("ncm") or "").strip() or "00000000"
        principal = str(grupo.get("descricao_padrao") or descricoes[0]).strip()
        linhas.append(
            {
                "Produto Principal": principal,
                "EAN": ean,
                "NCM": ncm,
                "Lista de Descrições": "; ".join(descricoes),
            }
        )

    linhas.sort(key=lambda linha: (linha["Produto Principal"].casefold(), linha["EAN"]))
    return linhas


def _gerar_excel_grupos_completos(linhas):
    """Gera o .xlsx do raio-x com pandas + openpyxl e devolve os bytes.

    Cabeçalho em negrito com filtro automático e painel congelado, colunas com
    largura confortável e quebra de linha na descrição principal/lista. O arquivo
    é montado em memória (BytesIO) — nada é gravado no disco do servidor.
    """
    dataframe = pd.DataFrame(linhas, columns=COLUNAS_EXPORTACAO_GRUPOS)
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        dataframe.to_excel(writer, index=False, sheet_name=NOME_PLANILHA_GRUPOS)
        planilha = writer.sheets[NOME_PLANILHA_GRUPOS]

        preenchimento_cabecalho = PatternFill("solid", fgColor="1F4E78")
        fonte_cabecalho = Font(bold=True, color="FFFFFF")
        for celula in planilha[1]:
            celula.fill = preenchimento_cabecalho
            celula.font = fonte_cabecalho
            celula.alignment = Alignment(horizontal="center", vertical="center")

        larguras = {"A": 55, "B": 18, "C": 14, "D": 110}
        for coluna, largura in larguras.items():
            planilha.column_dimensions[coluna].width = largura

        for linha in planilha.iter_rows(min_row=2):
            for celula in linha:
                celula.alignment = Alignment(vertical="top", wrap_text=True)
            linha[1].alignment = Alignment(horizontal="center", vertical="top")
            linha[1].number_format = "@"  # EAN como texto (preserva zeros à esquerda)
            linha[2].alignment = Alignment(horizontal="center", vertical="top")
            linha[2].number_format = "@"  # NCM como texto (04061010, 00000000...)

        planilha.freeze_panes = "A2"
        planilha.auto_filter.ref = planilha.dimensions
        planilha.sheet_view.showGridLines = True

    return buffer.getvalue()


@app.get("/api/relatorios/exportar-grupos-completos")
def exportar_grupos_completos(
    admin: Usuario = Depends(auth.get_admin_user_2fa),
    db: Session = Depends(get_db),
):
    """Baixa um Excel com TODOS os grupos que têm 2+ descrições únicas.

    Varre a base inteira (não só a janela/filtro da tela), agrupa pela chave
    inteligente e devolve as colunas Produto Principal, EAN, NCM e Lista de
    Descrições. Os cabeçalhos `X-Total-Grupos` e `X-Total-Descricoes` permitem
    ao front mostrar o resumo sem abrir o arquivo.
    """
    linhas = _linhas_grupos_com_variacoes(db)
    conteudo = _gerar_excel_grupos_completos(linhas)
    total_descricoes = sum(
        len([d for d in linha["Lista de Descrições"].split("; ") if d.strip()])
        for linha in linhas
    )
    nome_arquivo = f"grupos_com_variacoes_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    return StreamingResponse(
        BytesIO(conteudo),
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": f'attachment; filename="{nome_arquivo}"',
            "X-Total-Grupos": str(len(linhas)),
            "X-Total-Descricoes": str(total_descricoes),
            "Access-Control-Expose-Headers": (
                "Content-Disposition, X-Total-Grupos, X-Total-Descricoes"
            ),
        },
    )



# ──────────────────────────────────────────────────────────────────────────────
# Exportação MASTER do catálogo: TODAS as descrições da base, sem filtro de
# variações, agrupadas por PRODUTO + MARCA. É a listagem completa para conferir
# o catálogo (inclusive itens com uma única descrição, que não aparecem no raio-x
# de grupos com variações).
# ──────────────────────────────────────────────────────────────────────────────
COLUNAS_EXPORTACAO_CATALOGO = [
    "Produto (Marca)",
    "Marca",
    "EAN",
    "NCM",
    "Descrição Original",
]
NOME_PLANILHA_CATALOGO = "Catálogo completo"


def _titulo_catalogo(texto) -> str:
    """Título legível ("POLENTA PALITO" -> "Polenta Palito", "EL CHEF" -> "El Chef")."""
    return " ".join(parte.capitalize() for parte in str(texto or "").split())


_CONECTORES_MARCA = {"EL", "LA", "LOS", "LAS", "DE", "DEL", "DA", "DO", "DAS", "DOS"}


def _marca_exibicao(descricao, marca) -> str:
    """Marca como aparece na descrição, preservando o conector (EL CHEF, LA VIRGINIA).

    A inferência guarda só o token variável ("CHEF"); para a coluna legível, se a
    descrição traz "EL CHEF", o rótulo mostra a marca completa.
    """
    texto = str(marca or "").strip()
    if not texto:
        return ""
    tokens_descricao = re.findall(r"[A-Z0-9]+", regras_marcas.normalizar(descricao))
    tokens_marca = re.findall(r"[A-Z0-9]+", regras_marcas.normalizar(texto))
    if not tokens_descricao or not tokens_marca:
        return texto
    tamanho = len(tokens_marca)
    for indice in range(len(tokens_descricao) - tamanho + 1):
        if tokens_descricao[indice : indice + tamanho] == tokens_marca:
            if indice > 0 and tokens_descricao[indice - 1] in _CONECTORES_MARCA:
                return f"{tokens_descricao[indice - 1]} {texto}"
            break
    return texto


def _nome_base_catalogo(descricao, marca) -> str:
    """Nome do produto SEM a marca e SEM números de controle/embalagem.

    Usa `marcas.conteudo` (remove peso/quantidade/embalagem) e depois retira o
    token da marca. Ex.: "POLENTA PALITO ARRICO 1 X 10KG" -> "POLENTA PALITO".
    Quando a marca não foi identificada, o nome base (sem os números de controle)
    continua sendo a identidade do grupo.
    """
    texto = _remover_numero_controle(descricao)
    base = regras_marcas.conteudo(texto) or regras_marcas.normalizar(texto)
    if marca:
        # Remove a marca token a token: "EL CHEF" pode aparecer como "CHEF" na
        # descrição (o "EL" é curto e o extrator de marca o descarta).
        tokens_marca = regras_marcas.tokens(marca) or [marca]
        for token in tokens_marca:
            base = regras_marcas.sem_marca(base, token)
    base = re.sub(r"\s+", " ", str(base or "")).strip()
    return base or "PRODUTO SEM NOME"


def _rotulo_produto_marca(descricao, marca) -> str:
    """Rótulo da coluna Produto (Marca), ex.: "Polenta Palito (Arrico)"."""
    nome = _titulo_catalogo(_nome_base_catalogo(descricao, marca))
    marca_exibicao = _marca_exibicao(descricao, marca)
    if _marca_para_chave(marca_exibicao) is None:
        return nome
    return f"{nome} ({_titulo_catalogo(marca_exibicao)})"


def _linhas_catalogo_completo(db: Session):
    """Linhas do catálogo MASTER: todas as descrições, agrupadas por produto + marca.

    Diferente de `_linhas_grupos_com_variacoes`, nenhum grupo é descartado: até
    produtos com uma única descrição entram no arquivo. Cada linha representa
    uma descrição ORIGINAL (sem alterar o texto da NFe), repetindo o rótulo
    ``Produto (Marca)`` para o Excel ficar fácil de filtrar/ordenar.
    """
    mapa_regras, mapa_descricoes = _mapas_regras_relatorio(db)
    grupos = _grupos_produto_relatorio(db, mapa_regras, mapa_descricoes)

    linhas = []
    for grupo in grupos.values():
        marca = str(grupo.get("marca") or "").strip() or None
        principal = str(grupo.get("descricao_padrao") or "").strip()
        if not principal:
            descricoes_grupo = [
                str(descricao or "").strip()
                for descricao in (grupo.get("descricoes") or [])
                if str(descricao or "").strip()
            ]
            principal = descricoes_grupo[0] if descricoes_grupo else ""
        rotulo = _rotulo_produto_marca(principal, marca)
        # Coluna própria para o filtro no Excel: o rótulo "Produto (Marca)" junta os
        # dois dados, mas o catálogo precisa da marca isolada (ou SEM MARCA).
        marca_exibicao = _marca_exibicao(principal, marca) or "SEM MARCA"

        ean = str(grupo.get("ean") or "").strip()
        if not _tem_ean_valido_relatorio(ean):
            ean = "SEM EAN"
        ncm = str(grupo.get("ncm") or "").strip() or "00000000"

        descricoes, vistos = [], set()
        for descricao in grupo.get("descricoes") or []:
            texto = str(descricao or "").strip()
            chave = texto.casefold()
            if not texto or chave in vistos:
                continue
            vistos.add(chave)
            descricoes.append(texto)
        if not descricoes:
            descricoes = [principal] if principal else ["PRODUTO SEM DESCRIÇÃO"]

        for descricao in descricoes:
            linhas.append(
                {
                    "Produto (Marca)": rotulo,
                    "Marca": marca_exibicao,
                    "EAN": ean,
                    "NCM": ncm,
                    "Descrição Original": descricao,
                }
            )

    linhas.sort(
        key=lambda linha: (
            linha["Produto (Marca)"].casefold(),
            linha["EAN"],
            linha["Descrição Original"].casefold(),
        )
    )
    return linhas


def _gerar_excel_catalogo_completo(linhas):
    """Gera o .xlsx do catálogo MASTER em memória e devolve os bytes."""
    dataframe = pd.DataFrame(linhas, columns=COLUNAS_EXPORTACAO_CATALOGO)
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        dataframe.to_excel(writer, index=False, sheet_name=NOME_PLANILHA_CATALOGO)
        planilha = writer.sheets[NOME_PLANILHA_CATALOGO]

        preenchimento_cabecalho = PatternFill("solid", fgColor="1F4E78")
        fonte_cabecalho = Font(bold=True, color="FFFFFF")
        for celula in planilha[1]:
            celula.fill = preenchimento_cabecalho
            celula.font = fonte_cabecalho
            celula.alignment = Alignment(horizontal="center", vertical="center")

        larguras = {"A": 55, "B": 20, "C": 18, "D": 14, "E": 90}
        for coluna, largura in larguras.items():
            planilha.column_dimensions[coluna].width = largura

        for linha in planilha.iter_rows(min_row=2):
            for celula in linha:
                celula.alignment = Alignment(vertical="top", wrap_text=True)
            linha[2].alignment = Alignment(horizontal="center", vertical="top")
            linha[2].number_format = "@"  # EAN como texto
            linha[3].alignment = Alignment(horizontal="center", vertical="top")
            linha[3].number_format = "@"  # NCM como texto

        planilha.freeze_panes = "A2"
        planilha.auto_filter.ref = planilha.dimensions
        planilha.sheet_view.showGridLines = True

    return buffer.getvalue()


@app.get("/api/relatorios/exportar-base-completa")
def exportar_base_completa(
    admin: Usuario = Depends(auth.get_admin_user_2fa),
    db: Session = Depends(get_db),
):
    """Baixa o catálogo MASTER em Excel: todas as descrições da base.

    Agrupa por PRODUTO + MARCA (a mesma chave inteligente dos relatórios) e
    devolve as colunas ``Produto (Marca)``, ``Marca``, ``EAN``, ``NCM`` e
    ``Descrição Original``. Nenhum item é descartado por ter uma única
    descrição. Os cabeçalhos ``X-Total-Produtos`` e ``X-Total-Descricoes``
    resumem o arquivo sem precisar abri-lo.
    """
    linhas = _linhas_catalogo_completo(db)
    conteudo = _gerar_excel_catalogo_completo(linhas)
    total_produtos = len({linha["Produto (Marca)"] for linha in linhas})
    nome_arquivo = f"catalogo_completo_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    return StreamingResponse(
        BytesIO(conteudo),
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": f'attachment; filename="{nome_arquivo}"',
            "X-Total-Produtos": str(total_produtos),
            "X-Total-Descricoes": str(len(linhas)),
            "Access-Control-Expose-Headers": (
                "Content-Disposition, X-Total-Produtos, X-Total-Descricoes"
            ),
        },
    )



@app.get("/api/relatorio/evolucao")
def relatorio_evolucao(
    loja: Optional[str] = Query(None, description="Loja do usuário: as compras detalhadas saem só dela"),
    dias: int = Query(30, description="Janela analisada em dias: 7, 15, 30, 90, 180 ou 360"),
    periodicidade: str = Query(
        PERIODICIDADE_PADRAO,
        description="diaria, semanal, quinzenal, mensal, bimestral ou trimestral",
    ),
    produto: Optional[str] = Query(None, description="Descrição, EAN ou chave do grupo (vazio = todos)"),
    db: Session = Depends(get_db),
):
    """Evolução do preço médio do item: loja do usuário x mercado, período a período.

    A média do mercado usa TODAS as empresas da base (mesmo item e mesmo período),
    mas volta apenas como número. As compras detalhadas (tooltip do front) vêm só
    das lojas do usuário.
    """
    dias = _validar_dias(dias)
    periodicidade = _validar_periodicidade(periodicidade)
    loja_informada = str(loja or "").strip()
    referencia = _data_referencia_relatorios(db)
    if referencia is None:
        return {
            "loja": loja_informada or "Todas as Lojas",
            "lojas_detalhadas": [],
            "produto": produto or "Todos os produtos",
            "dias": dias,
            "periodicidade": periodicidade,
            "periodicidade_label": PERIODICIDADES[periodicidade],
            "data_referencia": None,
            "data_inicio": None,
            "data_fim": None,
            "periodos": [],
            "resumo": {},
            "integridade": {"pendentes_auditoria": 0, "bonificados": 0},
            "aviso": "Nenhuma nota fiscal importada ainda.",
        }

    data_inicio = referencia - timedelta(days=dias)
    estatisticas = {}
    itens = _carregar_itens_relatorio(db, data_inicio, referencia, estatisticas)
    itens, produto_nome = _filtrar_produto_relatorio(itens, produto)
    lojas_usuario = _normalizar_lojas_relatorio(loja, itens)
    lojas_set = set(lojas_usuario)

    periodos = {}
    for item in itens:
        inicio = _inicio_periodo(item["data"], periodicidade)
        grupos = periodos.setdefault(inicio, {"loja": [], "mercado": [], "chaves": set()})
        grupos["mercado"].append(item)
        grupos["chaves"].add(item["chave"])
        if item["loja"] in lojas_set:
            grupos["loja"].append(item)

    # Consulta PARALELA de marcas substitutas (só tooltip): não participa das médias.
    marcas_por_chave = _marcas_substitutas(db, itens, data_inicio, referencia)

    linhas = []
    for inicio in sorted(periodos):
        da_loja = periodos[inicio]["loja"]
        do_mercado = periodos[inicio]["mercado"]
        chaves_periodo = periodos[inicio]["chaves"]
        linhas.append(
            {
                "periodo": inicio.isoformat(),
                "label": _rotulo_periodo(inicio, periodicidade),
                "loja_preco_medio": _media_preco(da_loja),
                "loja_registros": len(da_loja),
                "loja_quantidade": round(sum(item["quantidade"] for item in da_loja), 4),
                "loja_total": round(sum(item["total"] for item in da_loja), 2),
                "mercado_preco_medio": _media_preco(do_mercado),
                "mercado_registros": len(do_mercado),
                # Marcas substitutas (mesmo NCM + descrição parecida): SOMENTE tooltip.
                # Só faz sentido quando o período tem um único produto — com vários, a
                # linha é uma média do período e a lista misturaria produtos.
                "mercado_marcas": (
                    marcas_por_chave.get(next(iter(chaves_periodo)), [])
                    if len(chaves_periodo) == 1
                    else []
                ),
                # Só as compras da loja do usuário (LGPD): o mercado nunca traz detalhe.
                "compras": _detalhar_compras(da_loja),
            }
        )

    # Variação período a período da série da loja (para o gráfico e a tabela).
    anterior = None
    for linha in linhas:
        preco = linha["loja_preco_medio"]
        linha["variacao_pct"] = (
            round((preco / anterior - 1) * 100, 2) if (preco is not None and anterior) else None
        )
        if preco is not None:
            anterior = preco

    compras_loja = [item for item in itens if item["loja"] in lojas_set]
    loja_media = _media_preco(compras_loja)
    mercado_media = _media_preco(itens)
    return {
        "loja": loja_informada or "Todas as Lojas",
        "lojas_detalhadas": lojas_usuario,
        "produto": produto_nome,
        "dias": dias,
        "periodicidade": periodicidade,
        "periodicidade_label": PERIODICIDADES[periodicidade],
        "data_referencia": referencia.isoformat(),
        "data_inicio": data_inicio.isoformat(),
        "data_fim": referencia.isoformat(),
        "periodos": linhas,
        "resumo": {
            "loja_preco_medio": loja_media,
            "mercado_preco_medio": mercado_media,
            "diferenca_pct": (
                round((loja_media / mercado_media - 1) * 100, 2)
                if (loja_media and mercado_media)
                else None
            ),
            "loja_registros": len(compras_loja),
            "mercado_registros": len(itens),
            "loja_total": round(sum(item["total"] for item in compras_loja), 2),
            "loja_quantidade": round(sum(item["quantidade"] for item in compras_loja), 4),
        },
        # Itens que ficaram fora do cálculo (auditoria pendente / bonificação).
        "integridade": estatisticas,
        "aviso": None if itens else "Nenhuma compra encontrada para os filtros informados.",
    }


@app.get("/api/relatorio/comparativo")
def relatorio_comparativo(
    lojas: Optional[List[str]] = Query(
        None, description="Lojas do usuário a comparar (repita o parâmetro: ?lojas=A&lojas=B)"
    ),
    dias: int = Query(30, description="Janela analisada em dias: 7, 15, 30, 90, 180 ou 360"),
    periodicidade: str = Query(
        PERIODICIDADE_PADRAO,
        description="diaria, semanal, quinzenal, mensal, bimestral ou trimestral",
    ),
    apenas_divergentes: bool = Query(
        False, description="True = só produtos com preço diferente entre as lojas no mesmo período"
    ),
    produto: Optional[str] = Query(None, description="Descrição, EAN ou chave do grupo (vazio = todos)"),
    limite: int = Query(LIMITE_LINHAS_PADRAO, ge=1, le=20000, description="Máximo de linhas devolvidas"),
    db: Session = Depends(get_db),
):
    """Comparativo entre lojas na mesma periodicidade, com a coluna "Mercado" no fim.

    Cada linha é um produto em um período, com o preço médio de cada loja escolhida e
    a média de TODAS as empresas do banco (mercado, só número). O detalhe das compras
    vai apenas para as lojas informadas.
    """
    dias = _validar_dias(dias)
    periodicidade = _validar_periodicidade(periodicidade)
    referencia = _data_referencia_relatorios(db)
    if referencia is None:
        return {
            "lojas": [],
            "produto": produto or "Todos os produtos",
            "dias": dias,
            "periodicidade": periodicidade,
            "periodicidade_label": PERIODICIDADES[periodicidade],
            "data_referencia": None,
            "data_inicio": None,
            "data_fim": None,
            "linhas": [],
            "total_linhas": 0,
            "truncado": False,
            "resumo": {},
            "integridade": {"pendentes_auditoria": 0, "bonificados": 0},
            "aviso": "Nenhuma nota fiscal importada ainda.",
        }

    data_inicio = referencia - timedelta(days=dias)
    estatisticas = {}
    itens = _carregar_itens_relatorio(db, data_inicio, referencia, estatisticas)
    itens, produto_nome = _filtrar_produto_relatorio(itens, produto)
    lojas_usuario = _normalizar_lojas_relatorio(lojas, itens)
    lojas_set = set(lojas_usuario)

    grupos = {}
    for item in itens:
        chave_grupo = (item["chave"], _inicio_periodo(item["data"], periodicidade))
        registro = grupos.setdefault(chave_grupo, {"mercado": [], "por_loja": {}})
        registro["mercado"].append(item)
        if item["loja"] in lojas_set:
            registro["por_loja"].setdefault(item["loja"], []).append(item)

    nomes = _nome_produto_por_chave(itens)
    # Consulta PARALELA de marcas substitutas (só tooltip): não participa das médias.
    marcas_por_chave = _marcas_substitutas(db, itens, data_inicio, referencia)
    linhas = []
    for (chave, inicio), registro in grupos.items():
        precos = {nome: _media_preco(lista) for nome, lista in registro["por_loja"].items()}
        valores = [valor for valor in precos.values() if valor is not None]
        divergente = len(valores) >= 2 and max(valores) > min(valores) * (1 + TOLERANCIA_DIVERGENCIA)
        if apenas_divergentes and not divergente:
            continue
        menor = min(valores) if valores else None
        maior = max(valores) if valores else None
        linhas.append(
            {
                "chave": chave,
                "produto": nomes.get(chave, chave),
                "periodo": inicio.isoformat(),
                "label": _rotulo_periodo(inicio, periodicidade),
                "precos": {nome: precos.get(nome) for nome in lojas_usuario},
                "mercado_preco_medio": _media_preco(registro["mercado"]),
                "mercado_registros": len(registro["mercado"]),
                # Marcas substitutas (mesmo NCM + descrição parecida): SOMENTE tooltip.
                "mercado_marcas": marcas_por_chave.get(chave, []),
                "divergencia_pct": (
                    round((maior / menor - 1) * 100, 2) if (menor and maior) else None
                ),
                "divergente": divergente,
                # Detalhe só das lojas do usuário (nunca do mercado).
                "compras": {
                    nome: _detalhar_compras(lista)
                    for nome, lista in registro["por_loja"].items()
                },
            }
        )

    linhas.sort(key=lambda linha: (linha["produto"].upper(), linha["periodo"]))
    total_linhas = len(linhas)
    truncado = total_linhas > limite
    # O resumo considera TODAS as linhas do período (a tabela pode vir truncada).
    resumo = {
        "produtos": len({linha["chave"] for linha in linhas}),
        "divergentes": sum(1 for linha in linhas if linha["divergente"]),
        "lojas_com_dados": [
            nome for nome in lojas_usuario
            if any(linha["precos"].get(nome) is not None for linha in linhas)
        ],
    }
    linhas = linhas[:limite]
    return {
        "lojas": lojas_usuario,
        "produto": produto_nome,
        "dias": dias,
        "periodicidade": periodicidade,
        "periodicidade_label": PERIODICIDADES[periodicidade],
        "data_referencia": referencia.isoformat(),
        "data_inicio": data_inicio.isoformat(),
        "data_fim": referencia.isoformat(),
        "linhas": linhas,
        "total_linhas": total_linhas,
        "truncado": truncado,
        "resumo": resumo,
        # Itens que ficaram fora do cálculo (auditoria pendente / bonificação).
        "integridade": estatisticas,
        "aviso": None if itens else "Nenhuma compra encontrada para os filtros informados.",
    }


def _classificar_aberracoes(linhas, limite, validadas=None):
    """Separa as aberrações (variação de 50% ou mais) e ordena a exibição.

    Aberração é indício de erro de unidade/cadastro: NÃO é economia nem aumento (os dois
    valores ficam zerados), recebe o alerta "Verificar Variação Brusca" e vai para o FIM da
    lista. Devolve (linhas_para_exibir, normais, aberracoes); os totais do relatório usam
    apenas `normais`.

    `validadas` são as chaves (DESC_...) que o auditor confirmou: a variação é real, então
    o item volta a contar como aumento/economia e recebe `validada=True` (selo na tabela).
    """
    validadas = validadas or set()
    for linha in linhas:
        if str(linha.get("chave_descricao") or "") in validadas:
            linha["aberrante"] = False
            linha["alerta"] = None
            linha["validada"] = True
            continue
        linha["aberrante"] = (
            abs(float(linha.get("variacao_pct") or 0.0)) >= LIMIAR_ABERRACAO_PCT
        )
        linha["alerta"] = ALERTA_ABERRACAO if linha["aberrante"] else None
        if linha["aberrante"]:
            linha["aumento"] = 0.0
            linha["economia"] = 0.0
    normais = sorted(
        (linha for linha in linhas if not linha["aberrante"]),
        key=lambda linha: abs(linha["impacto_mensal"]),
        reverse=True,
    )
    aberracoes = sorted(
        (linha for linha in linhas if linha["aberrante"]),
        key=lambda linha: abs(linha["impacto_mensal"]),
        reverse=True,
    )
    if len(linhas) <= limite:
        return normais + aberracoes, normais, aberracoes
    # As aberrações nunca somem no corte: reserva até 10% das vagas para elas.
    vagas_aberracoes = min(len(aberracoes), max(1, limite // 10))
    exibicao = normais[: limite - vagas_aberracoes] + aberracoes[:vagas_aberracoes]
    return exibicao, normais, aberracoes


def _registrar_aberracoes_auditoria(db: Session, aberracoes):
    """Envia as aberrações para a auditoria (pendência automática, motivo "aberrante").

    É o "vai automaticamente para a Auditoria" da regra de variação brusca: grava a chave
    da descrição como pendente para o auditor validar. Idempotente e sem efeitos
    colaterais nos já validados:
    - chave aprovada (auditado=True) não é rebaixada para pendente;
    - chave já pendente não é duplicada (a coluna é única).
    Devolve quantas chaves NOVAS foram criadas.
    """
    chaves = {str(linha.get("chave_descricao") or "").strip() for linha in aberracoes or []}
    chaves.discard("")
    if not chaves:
        return 0
    existentes = {
        registro.chave_identificacao
        for registro in db.query(ItemAuditado)
        .filter(ItemAuditado.chave_identificacao.in_(chaves))
        .all()
    }
    novas = 0
    for chave in sorted(chaves - existentes):
        db.add(
            ItemAuditado(
                chave_identificacao=chave,
                auditado=False,
                motivo=MOTIVO_PENDENCIA_ABERRACAO,
            )
        )
        novas += 1
    if novas:
        db.commit()
    return novas


def _contar_pendentes_aberracoes(db: Session):
    """Quantas chaves estão aguardando validação na Auditoria por variação brusca."""
    return (
        db.query(ItemAuditado)
        .filter(
            ItemAuditado.auditado == False,  # noqa: E712 (SQLAlchemy: comparação, não booleano)
            ItemAuditado.motivo == MOTIVO_PENDENCIA_ABERRACAO,
        )
        .count()
    )


@app.get("/api/relatorio/impacto")
def relatorio_impacto(
    loja: Optional[str] = Query(None, description="Loja do usuário (vazio = todas as lojas do usuário)"),
    produto: Optional[str] = Query(None, description="Descrição, EAN ou chave do grupo (vazio = todos)"),
    meses_consumo: int = Query(3, ge=1, le=12, description="Janela do consumo médio, em meses"),
    limite: int = Query(500, ge=1, le=5000, description="Máximo de produtos devolvidos"),
    db: Session = Depends(get_db),
):
    """Impacto financeiro da variação de preço entre a última e a penúltima compra.

    impacto_mensal = (preço atual − preço anterior) × consumo médio mensal do produto
    (quantidade comprada nos últimos `meses_consumo` meses ÷ meses_consumo).
    Positivo = aumento de custo; negativo = economia.

    Integridade: itens pendentes na auditoria e bonificados (CFOP 5910/6910 ou R$ 0,00)
    ficam fora (ver `_carregar_itens_relatorio`); variação de LIMIAR_ABERRACAO_PCT ou mais
    não conta como aumento nem economia — volta marcada com `aberrante`/`alerta` no fim da
    lista e é registrada automaticamente na Auditoria (motivo "aberrante").
    """
    referencia = _data_referencia_relatorios(db)
    if referencia is None:
        return {
            "loja": "Todas as Lojas",
            "lojas_detalhadas": [],
            "produto": produto or "Todos os produtos",
            "meses_consumo": meses_consumo,
            "referencia": None,
            "inicio_consumo": None,
            "linhas": [],
            "total_linhas": 0,
            "truncado": False,
            "totais": {"aumento": 0.0, "economia": 0.0, "impacto_liquido": 0.0, "produtos_comparados": 0},
            "aberracoes": {"quantidade": 0, "limite_pct": LIMIAR_ABERRACAO_PCT, "alerta": ALERTA_ABERRACAO},
            "integridade": {"pendentes_auditoria": 0, "bonificados": 0},
            "aviso": "Nenhuma nota fiscal importada ainda.",
        }

    # A comparação precisa da penúltima compra, então olha todo o histórico do produto.
    estatisticas = {}
    itens = _carregar_itens_relatorio(db, estatisticas=estatisticas)
    itens, produto_nome = _filtrar_produto_relatorio(itens, produto)
    lojas_usuario = _normalizar_lojas_relatorio(loja, itens)
    lojas_set = set(lojas_usuario)
    itens = [item for item in itens if item["loja"] in lojas_set]

    # Consumo médio: média mensal de quantidade comprada na janela de `meses_consumo`.
    inicio_consumo = referencia - timedelta(days=30 * meses_consumo)
    por_chave = {}
    for item in itens:
        por_chave.setdefault(item["chave"], []).append(item)

    nomes = _nome_produto_por_chave(itens)
    linhas = []
    for chave, compras in por_chave.items():
        compras = sorted(compras, key=lambda item: (item["data"], item["id"]))
        if len(compras) < 2:
            continue
        atual, anterior = compras[-1], compras[-2]
        if anterior["valor"] <= 0 or atual["valor"] <= 0:
            continue
        consumo = sum(item["quantidade"] for item in compras if item["data"] >= inicio_consumo)
        consumo_medio = consumo / meses_consumo
        impacto = (atual["valor"] - anterior["valor"]) * consumo_medio
        linhas.append(
            {
                "chave": chave,
                # Chave da descrição na auditoria (é ela que "Enviar para auditoria" grava).
                "chave_descricao": atual.get("chave_descricao")
                or regras_auditoria.chave_descricao(chave, atual["descricao"]),
                "produto": nomes.get(chave, chave),
                "loja": atual["loja"],
                # Item da ÚLTIMA compra: o botão "Ver DANFE" da Auditoria abre o XML
                # original desse item (endpoint /api/nota/{item_id}).
                "item_id": atual["id"],
                "preco_atual": round(atual["valor"], 4),
                "preco_anterior": round(anterior["valor"], 4),
                "variacao_pct": round((atual["valor"] / anterior["valor"] - 1) * 100, 2),
                "data_atual": atual["data"].isoformat(),
                "data_anterior": anterior["data"].isoformat(),
                "fornecedor_atual": atual["fornecedor"] or "Fornecedor não informado",
                "fornecedor_anterior": anterior["fornecedor"] or "Fornecedor não informado",
                "consumo_medio": round(consumo_medio, 4),
                "impacto_mensal": round(impacto, 2),
                "aumento": round(max(impacto, 0.0), 2),
                "economia": round(max(-impacto, 0.0), 2),
            }
        )

    # Variações já validadas na Auditoria voltam a contar normalmente (selo "validada").
    # ItemAuditado é uma tabela pequena: reler aqui evita acoplar o carregamento dos itens
    # à classificação (que é só de exibição).
    _aprovadas, _manuais, _aberracoes_pendentes, aberracoes_validadas = _mapa_auditoria_relatorio(db)
    linhas, normais, aberracoes = _classificar_aberracoes(linhas, limite, aberracoes_validadas)
    # Regra da variação brusca: as aberrações vão automaticamente para a Auditoria
    # (pendência com motivo "aberrante") — o auditor valida e libera.
    enviadas_auditoria = _registrar_aberracoes_auditoria(db, aberracoes)
    pendentes_aberracoes = _contar_pendentes_aberracoes(db)
    total_linhas = len(normais) + len(aberracoes)
    truncado = total_linhas > limite
    # Os totais consideram TODOS os produtos comparados (a tabela pode vir truncada) e
    # NUNCA as aberrações pendentes: elas só entram depois de validadas na Auditoria.
    totais = {
        "aumento": round(sum(linha["aumento"] for linha in normais), 2),
        "economia": round(sum(linha["economia"] for linha in normais), 2),
        "impacto_liquido": round(sum(linha["impacto_mensal"] for linha in normais), 2),
        "produtos_comparados": len(normais),
        "aberracoes": len(aberracoes),
        "aberracoes_impacto": round(sum(abs(linha["impacto_mensal"]) for linha in aberracoes), 2),
    }
    loja_informada = str(loja or "").strip()
    return {
        "loja": loja_informada or "Todas as Lojas",
        "lojas_detalhadas": lojas_usuario,
        "produto": produto_nome,
        "meses_consumo": meses_consumo,
        "referencia": referencia.isoformat(),
        "inicio_consumo": inicio_consumo.isoformat(),
        "linhas": linhas,
        "total_linhas": total_linhas,
        "truncado": truncado,
        "totais": totais,
        "aberracoes": {
            "quantidade": len(aberracoes),
            "limite_pct": LIMIAR_ABERRACAO_PCT,
            "alerta": ALERTA_ABERRACAO,
            # Chaves novas gravadas na Auditoria nesta chamada (as demais já estavam lá).
            "enviadas_auditoria": enviadas_auditoria,
            "pendentes_auditoria": pendentes_aberracoes,
        },
        # Itens que ficaram fora do cálculo (auditoria pendente / bonificação).
        "integridade": estatisticas,
        "aviso": None if linhas else "Nenhum produto com pelo menos duas compras registradas.",
    }

