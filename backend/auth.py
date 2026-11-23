"""Autenticação do SaaS: hash de senha (passlib), token JWT e dependências do FastAPI.

- **Senha**: passlib com bcrypt (hash lento e com salt) — a senha em texto nunca é
  gravada. O `pbkdf2_sha256` fica no contexto como alternativa caso algum hash antigo
  tenha sido gerado sem o backend do bcrypt disponível.
- **Sessão**: token JWT assinado (HS256) com validade configurável (padrão: 12 horas).
  O front guarda o token e envia em `Authorization: Bearer <token>`.
- **2FA (TOTP)**: o administrador entra com senha + código de 6 dígitos. `/api/auth/login`
  valida a senha e, quando a conta exige 2FA, responde com um **desafio**
  (`tipo="2fa_desafio"`, 10 min) em vez da sessão: a sessão só nasce em
  `/api/auth/2fa/verify`, depois do código do autenticador (`pyotp`). O segredo fica em
  `usuarios.totp_secret` e o QR Code é gerado no backend (`qrcode`). O verify também
  devolve um token JWT curto (`tipo="2fa"`) que o front envia em `X-2FA-Token` nas rotas
  de auditoria — dependência `get_admin_user_2fa`.
- **Segredo**: variável de ambiente `NFE_SECRET_KEY`. Sem ela, um segredo aleatório é
  gerado uma única vez e salvo em `.secret_key` na raiz do projeto, para os tokens
  continuarem válidos depois de reiniciar a API.
"""

import base64
import hashlib
import hmac
import io
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import NamedTuple, Optional

import jwt
import pyotp
import qrcode
from fastapi import Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from backend.database import PROJECT_ROOT, get_db
from backend.models import Usuario

ALGORITMO = "HS256"
VALIDADE_HORAS = int(os.getenv("NFE_TOKEN_HORAS", "12"))
SENHA_MINIMA = 6
NOME_MINIMO = 3
ARQUIVO_SEGREDO = os.path.join(PROJECT_ROOT, ".secret_key")
TAMANHO_MINIMO_SEGREDO = 32
MENSAGEM_SESSAO_EXPIRADA = "Sessão expirada ou inválida. Entre novamente."

# ── Autenticação em dois fatores (TOTP) ────────────────────────────────────────
TOTP_EMISSOR = "Inteligencia de Custos"
TOTP_DIGITOS = 6
# ±1 janela de 30s no pyotp: tolera o relógio do celular alguns segundos atrasado.
TOTP_JANELA = 1
VALIDADE_2FA_HORAS = int(os.getenv("NFE_2FA_HORAS", "2"))
TIPO_TOKEN_2FA = "2fa"
CABECALHO_2FA = "X-2FA-Token"
CABECALHO_2FA_REQUERIDO = "X-2FA-Required"
MENSAGEM_2FA_NAO_CONFIGURADO = (
    "A Auditoria exige autenticação em dois fatores (2FA). Configure o QR Code na tela "
    "de Auditoria e confirme um código de 6 dígitos."
)
MENSAGEM_2FA_NECESSARIO = (
    "Informe o código de 6 dígitos do autenticador para acessar a Auditoria."
)

# bcrypt primeiro (hash padrão novo); pbkdf2_sha256 só valida hashes antigos.
_senhas = CryptContext(schemes=["bcrypt", "pbkdf2_sha256"], deprecated="auto")

# Lido uma vez por processo: evita abrir o arquivo a cada requisição.
_segredo_cache: Optional[str] = None
_segredo_processo = secrets.token_urlsafe(48)


def segredo() -> str:
    """Chave usada para assinar os tokens (env NFE_SECRET_KEY ou arquivo local)."""
    global _segredo_cache
    if _segredo_cache:
        return _segredo_cache

    da_ambiente = (os.getenv("NFE_SECRET_KEY") or "").strip()
    if len(da_ambiente) >= TAMANHO_MINIMO_SEGREDO:
        _segredo_cache = da_ambiente
        return _segredo_cache

    try:
        if os.path.exists(ARQUIVO_SEGREDO):
            with open(ARQUIVO_SEGREDO, encoding="utf-8") as arquivo:
                guardado = arquivo.read().strip()
            if len(guardado) >= TAMANHO_MINIMO_SEGREDO:
                _segredo_cache = guardado
                return _segredo_cache
        novo = secrets.token_urlsafe(48)
        with open(ARQUIVO_SEGREDO, "w", encoding="utf-8") as arquivo:
            arquivo.write(novo)
        _segredo_cache = novo
        return _segredo_cache
    except OSError:
        # Pasta sem permissão de escrita: usa um segredo só deste processo
        # (os tokens deixam de valer quando a API reiniciar).
        _segredo_cache = _segredo_processo
        return _segredo_cache

# ──────────────────────────────────────────────────────────────────────────────
# Senha
# ──────────────────────────────────────────────────────────────────────────────
def gerar_hash_senha(senha: str) -> str:
    """Hash do passlib (bcrypt) para gravar em `usuarios.senha_hash`."""
    return _senhas.hash(str(senha or ""))


def verificar_senha(senha: str, senha_hash: str) -> bool:
    """True quando a senha informada corresponde ao hash gravado."""
    if not senha or not senha_hash:
        return False
    try:
        return bool(_senhas.verify(str(senha), str(senha_hash)))
    except (ValueError, TypeError):
        # Hash corrompido/em formato desconhecido: trata como senha errada.
        return False


def normalizar_email(email) -> str:
    """E-mail sempre em minúsculas e sem espaços (a comparação no banco é por ele)."""
    return str(email or "").strip().lower()


def validar_cadastro(nome, email, senha) -> tuple:
    """Valida os dados de cadastro. Devolve (nome, email) normalizados.

    Levanta HTTPException 400 com a mensagem que o front mostra ao usuário.
    """
    nome_limpo = " ".join(str(nome or "").split())
    email_limpo = normalizar_email(email)
    if len(nome_limpo) < NOME_MINIMO:
        raise HTTPException(status_code=400, detail="Informe o seu nome completo.")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", email_limpo):
        raise HTTPException(status_code=400, detail="Informe um e-mail válido.")
    if len(str(senha or "")) < SENHA_MINIMA:
        raise HTTPException(
            status_code=400,
            detail=f"A senha precisa ter pelo menos {SENHA_MINIMA} caracteres.",
        )
    return nome_limpo, email_limpo


# ──────────────────────────────────────────────────────────────────────────────
# Código de confirmação de e-mail (cadastro)
# ──────────────────────────────────────────────────────────────────────────────
# Validade do código em minutos (NFE_CODIGO_EMAIL_MINUTOS) e limite de erros por
# código. Depois do limite o cliente pede outro em /api/auth/reenviar-codigo.
VALIDADE_CODIGO_EMAIL_MINUTOS = int(os.getenv("NFE_CODIGO_EMAIL_MINUTOS", "15"))
TENTATIVAS_CODIGO_EMAIL = 5


def gerar_codigo_email() -> str:
    """Código numérico de 6 dígitos gerado com `secrets` (uso único)."""
    return f"{secrets.randbelow(10 ** 6):06d}"


def hash_codigo_email(codigo) -> str:
    """SHA-256 do código + segredo da aplicação (o código nunca vai ao banco em texto).

    O segredo é o mesmo `NFE_SECRET_KEY` que assina os tokens (`segredo()`), então um
    dump do banco sozinho não permite adivinhar códigos válidos.
    """
    base = f"{segredo()}:{re.sub(r'[^0-9]', '', str(codigo or ''))}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def codigo_email_valido(codigo, codigo_hash) -> bool:
    """Compara o código informado com o hash gravado em tempo constante."""
    return hmac.compare_digest(hash_codigo_email(codigo), str(codigo_hash or ""))


# ──────────────────────────────────────────────────────────────────────────────
# Token JWT
# ──────────────────────────────────────────────────────────────────────────────
def criar_token(usuario: Usuario) -> tuple:
    """Token de acesso do usuário. Devolve (token, data/hora de expiração UTC)."""
    agora = datetime.now(timezone.utc)
    expira = agora + timedelta(hours=VALIDADE_HORAS)
    payload = {
        "sub": str(usuario.id),
        "email": usuario.email,
        "nome": usuario.nome,
        "iat": agora,
        "exp": expira,
    }
    return jwt.encode(payload, segredo(), algorithm=ALGORITMO), expira


def ler_token(token: str) -> dict:
    """Conteúdo do token. Levanta 401 quando o token é inválido ou está expirado."""
    try:
        return jwt.decode(token, segredo(), algorithms=[ALGORITMO])
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=401,
            detail="Sua sessão expirou. Entre novamente.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=401,
            detail=MENSAGEM_SESSAO_EXPIRADA,
            headers={"WWW-Authenticate": "Bearer"},
        )


# ──────────────────────────────────────────────────────────────────────────────
# Autenticação em dois fatores (TOTP)
# ──────────────────────────────────────────────────────────────────────────────
def gerar_segredo_totp() -> str:
    """Segredo base32 novo (160 bits) para o autenticador do administrador."""
    return pyotp.random_base32()


def url_otpauth(segredo: str, email: str) -> str:
    """URL `otpauth://totp/...` que o aplicativo autenticador lê pelo QR Code."""
    return pyotp.TOTP(str(segredo)).provisioning_uri(
        name=normalizar_email(email), issuer_name=TOTP_EMISSOR
    )


def gerar_qr_code_data_uri(otpauth_url: str) -> str:
    """QR Code em PNG, como data URI (`data:image/png;base64,...`) pronto para o front."""
    imagem = qrcode.make(otpauth_url)
    buffer = io.BytesIO()
    imagem.save(buffer, format="PNG")
    codificado = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{codificado}"


def normalizar_codigo_totp(codigo) -> str:
    """Só os dígitos do código (o usuário pode digitar "123 456" ou "123-456")."""
    return re.sub(r"\D", "", str(codigo or ""))


def codigo_totp_valido(segredo: str, codigo) -> bool:
    """True quando o código de 6 dígitos confere com o segredo (janela de ±30s)."""
    codigo = normalizar_codigo_totp(codigo)
    if not segredo or len(codigo) != TOTP_DIGITOS:
        return False
    try:
        return bool(pyotp.TOTP(str(segredo)).verify(codigo, valid_window=TOTP_JANELA))
    except Exception:
        # Segredo corrompido em base32: trata como código inválido (nunca libera).
        return False


def usuario_com_2fa(usuario: Usuario) -> bool:
    """True quando o 2FA está ATIVO (segredo gerado e confirmado com um código)."""
    return bool(
        getattr(usuario, "totp_secret", None) and getattr(usuario, "totp_ativo", False)
    )


def criar_token_2fa(usuario: Usuario) -> tuple:
    """Token curto que comprova a verificação 2FA. Devolve (token, expiração UTC).

    É um JWT separado do token de sessão (`tipo="2fa"`): sozinho ele não abre nenhuma
    rota de usuário — `usuario_atual` recusa esse tipo — e serve só como 2º fator nas
    rotas de auditoria (`X-2FA-Token`).
    """
    agora = datetime.now(timezone.utc)
    expira = agora + timedelta(hours=VALIDADE_2FA_HORAS)
    payload = {
        "sub": str(usuario.id),
        "email": usuario.email,
        "tipo": TIPO_TOKEN_2FA,
        "iat": agora,
        "exp": expira,
    }
    return jwt.encode(payload, segredo(), algorithm=ALGORITMO), expira


def ler_token_2fa(token: str) -> dict:
    """Conteúdo do token 2FA. 401 quando expirado, inválido ou de outro tipo."""
    try:
        dados = jwt.decode(str(token or ""), segredo(), algorithms=[ALGORITMO])
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=401,
            detail="Verificação 2FA expirada. Informe um novo código do autenticador.",
            headers={CABECALHO_2FA_REQUERIDO: "codigo"},
        )
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=401,
            detail="Verificação 2FA inválida. Informe o código novamente.",
            headers={CABECALHO_2FA_REQUERIDO: "codigo"},
        )
    if str(dados.get("tipo") or "") != TIPO_TOKEN_2FA:
        raise HTTPException(
            status_code=401,
            detail="Este token não é uma verificação 2FA válida.",
            headers={CABECALHO_2FA_REQUERIDO: "codigo"},
        )
    return dados


# ──────────────────────────────────────────────────────────────────────────────
# Desafio do login (2ª etapa do 2FA)
# ──────────────────────────────────────────────────────────────────────────────
# A senha sozinha NÃO abre sessão para administrador: /api/auth/login responde 401/403
# com um `desafio` (JWT curto, tipo "2fa_desafio") e o front só consegue chamar
# /api/auth/2fa/setup e /api/auth/2fa/verify com ele. A sessão nasce no verify, depois do
# código de 6 dígitos — o login inteiro fica bloqueado sem o segundo fator.
TIPO_TOKEN_DESAFIO = "2fa_desafio"
VALIDADE_DESAFIO_MINUTOS = int(os.getenv("NFE_2FA_DESAFIO_MINUTOS", "10"))
MENSAGEM_2FA_OBRIGATORIO_LOGIN = (
    "Esta conta de administrador exige o código de 6 dígitos do autenticador. "
    "Informe o código para concluir o login."
)
MENSAGEM_2FA_INVALIDO_LOGIN = (
    "Código de verificação inválido ou expirado. Use o código atual do aplicativo autenticador."
)
MENSAGEM_2FA_SEM_CADASTRO_LOGIN = (
    "O login de administrador exige autenticação em dois fatores. Escaneie o QR Code no "
    "aplicativo autenticador e confirme o primeiro código para concluir o login."
)
MENSAGEM_DESAFIO_EXPIRADO = (
    "A verificação em dois fatores demorou demais. Informe e-mail e senha novamente."
)


def criar_token_desafio(usuario: Usuario) -> tuple:
    """Token do desafio de login. Devolve (token, expiração UTC).

    Vale só para as rotas /api/auth/2fa/* (setup e verify): `usuario_atual` recusa este
    tipo, então ele nunca abre uma rota de dados.
    """
    agora = datetime.now(timezone.utc)
    expira = agora + timedelta(minutes=VALIDADE_DESAFIO_MINUTOS)
    payload = {
        "sub": str(usuario.id),
        "email": usuario.email,
        "tipo": TIPO_TOKEN_DESAFIO,
        "iat": agora,
        "exp": expira,
    }
    return jwt.encode(payload, segredo(), algorithm=ALGORITMO), expira


def ler_token_desafio(token: str) -> dict:
    """Conteúdo do token de desafio. 401 quando expirado, inválido ou de outro tipo."""
    try:
        dados = jwt.decode(str(token or ""), segredo(), algorithms=[ALGORITMO])
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=401,
            detail=MENSAGEM_DESAFIO_EXPIRADO,
            headers={CABECALHO_2FA_REQUERIDO: "codigo"},
        )
    if str(dados.get("tipo") or "") != TIPO_TOKEN_DESAFIO:
        raise HTTPException(
            status_code=401,
            detail=MENSAGEM_DESAFIO_EXPIRADO,
            headers={CABECALHO_2FA_REQUERIDO: "codigo"},
        )
    return dados


def desafio_login(usuario: Usuario, motivo: str) -> HTTPException:
    """HTTPException do login que exige o 2FA, com o desafio para o front continuar.

    `motivo` vai no corpo (`motivo`) para a tela distinguir os três casos sem adivinhar
    pelo texto: "codigo_ausente" (senha certa, falta o código), "codigo_invalido" e
    "sem_cadastro" (admin que ainda não configurou o autenticador — mostra o QR Code).
    """
    token, expira = criar_token_desafio(usuario)
    mensagens = {
        "codigo_ausente": (401, MENSAGEM_2FA_OBRIGATORIO_LOGIN, "codigo"),
        "codigo_invalido": (401, MENSAGEM_2FA_INVALIDO_LOGIN, "codigo"),
        "sem_cadastro": (403, MENSAGEM_2FA_SEM_CADASTRO_LOGIN, "setup"),
    }
    codigo_status, mensagem, etapa = mensagens.get(motivo, mensagens["codigo_ausente"])
    return HTTPException(
        status_code=codigo_status,
        detail={
            "motivo": motivo,
            "mensagem": mensagem,
            "codigo_2fa_necessario": etapa == "codigo",
            "configuracao_2fa_necessaria": etapa == "setup",
            # O front guarda este desafio e chama /api/auth/2fa/setup|verify com ele.
            "desafio": token,
            "expira_em": expira.isoformat(),
            "usuario": descricao_usuario(usuario),
        },
        headers={CABECALHO_2FA_REQUERIDO: etapa},
    )


# ──────────────────────────────────────────────────────────────────────────────
# Dependências do FastAPI
# ──────────────────────────────────────────────────────────────────────────────
_portador = HTTPBearer(
    auto_error=False,
    description="Token JWT devolvido por /api/auth/login (Authorization: Bearer <token>)",
)


def _resolver_usuario(dados: dict, db: Session) -> Usuario:
    """Conta dona do token (401 quando o `sub` não existe mais)."""
    try:
        usuario_id = int(dados.get("sub"))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=401,
            detail=MENSAGEM_SESSAO_EXPIRADA,
            headers={"WWW-Authenticate": "Bearer"},
        )
    usuario = db.query(Usuario).filter(Usuario.id == usuario_id).first()
    if not usuario:
        raise HTTPException(
            status_code=401,
            detail="Conta não encontrada. Entre novamente.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return usuario


def _usuario_do_token(token: str, db: Session) -> Usuario:
    """Usuário dono de um token de SESSÃO (recusa os tokens de 2FA e de desafio)."""
    dados = ler_token(token)
    tipo = str(dados.get("tipo") or "")
    if tipo == TIPO_TOKEN_2FA:
        # O token de 2FA comprova só o segundo fator: nunca vale como sessão.
        raise HTTPException(
            status_code=401,
            detail="Este token é apenas a verificação 2FA. Entre novamente com e-mail e senha.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if tipo == TIPO_TOKEN_DESAFIO:
        # O desafio só serve para /api/auth/2fa/*: não abre nenhuma rota de dados.
        raise HTTPException(
            status_code=401,
            detail=(
                "O login ainda não foi concluído: informe o código de 6 dígitos do "
                "autenticador."
            ),
            headers={"WWW-Authenticate": "Bearer"},
        )
    return _resolver_usuario(dados, db)


class Sessao2FA(NamedTuple):
    """Quem chamou /api/auth/2fa/*: sessão normal ou o desafio do login em andamento."""

    usuario: Usuario
    via_desafio: bool = False


def autenticacao_2fa(
    credenciais: Optional[HTTPAuthorizationCredentials] = Depends(_portador),
    db: Session = Depends(get_db),
) -> Sessao2FA:
    """Sessão do usuário OU o desafio emitido no login — dependência das rotas de 2FA.

    O desafio existe porque o login do administrador é FAIL-CLOSED: `/api/auth/login` não
    devolve token de sessão antes do código de 6 dígitos. Para escanear o QR Code no
    primeiro acesso e confirmar o código, o front usa o desafio (vale 10 minutos e só
    nestas rotas).
    """
    token = credenciais.credentials if credenciais else None
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Faça login para acessar este recurso.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    dados = ler_token(token)
    if str(dados.get("tipo") or "") == TIPO_TOKEN_DESAFIO:
        return Sessao2FA(_resolver_usuario(dados, db), via_desafio=True)
    return Sessao2FA(_usuario_do_token(token, db))


def usuario_atual(
    credenciais: Optional[HTTPAuthorizationCredentials] = Depends(_portador),
    db: Session = Depends(get_db),
) -> Usuario:
    """Usuário logado. Rota protegida: sem token válido responde 401."""
    token = credenciais.credentials if credenciais else None
    if not token:
        raise HTTPException(
            status_code=401,
            detail="Faça login para acessar este recurso.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return _usuario_do_token(token, db)


def descricao_usuario(usuario: Usuario) -> dict:
    """Dados públicos do usuário (sem o hash da senha) para o front."""
    return {
        "id": usuario.id,
        "nome": usuario.nome,
        "email": usuario.email,
        # O front usa esta flag só para esconder o menu; a autorização real é feita
        # pela dependência `get_admin_user` em cada rota de auditoria.
        "is_admin": bool(getattr(usuario, "is_admin", False)),
        # 2FA ativo: o front pede o código de 6 dígitos no login e antes da Auditoria.
        "totp_ativo": usuario_com_2fa(usuario),
        # E-mail confirmado por código (cadastro). Contas antigas entram como True —
        # ver a migração em database._garantir_colunas_novas.
        "email_verificado": bool(getattr(usuario, "email_verificado", True)),
    }


def get_admin_user(usuario: Usuario = Depends(usuario_atual)) -> Usuario:
    """Usuário logado E administrador — dependência das rotas de auditoria.

    Sem token válido a resposta é 401 (herdada de `usuario_atual`); com token de uma
    conta comum é **403**: a rota nem chega a consultar os dados de auditoria.
    """
    if not bool(getattr(usuario, "is_admin", False)):
        raise HTTPException(
            status_code=403,
            detail="Área restrita aos administradores do sistema.",
        )
    return usuario


def get_admin_user_2fa(
    admin: Usuario = Depends(get_admin_user),
    token_2fa: Optional[str] = Header(None, alias=CABECALHO_2FA),
) -> Usuario:
    """Administrador com o segundo fator confirmado — dependência da Auditoria.

    Ordem das barreiras (todas no backend, nunca só na tela):
    1. token de sessão válido e conta `is_admin` (herdado de `get_admin_user`);
    2. 2FA **ativo** na conta — sem isso a resposta é 403 e a tela mostra o QR Code;
    3. `X-2FA-Token` válido (emitido por /api/auth/2fa/verify, vale por 2h) **da mesma
       conta** — sem ele a resposta é 401.

    Assim um token de sessão roubado, sozinho, não abre nenhuma rota de auditoria.
    """
    if not usuario_com_2fa(admin):
        raise HTTPException(
            status_code=403,
            detail=MENSAGEM_2FA_NAO_CONFIGURADO,
            headers={CABECALHO_2FA_REQUERIDO: "setup"},
        )
    if not token_2fa:
        raise HTTPException(
            status_code=401,
            detail=MENSAGEM_2FA_NECESSARIO,
            headers={
                CABECALHO_2FA_REQUERIDO: "codigo",
                "WWW-Authenticate": CABECALHO_2FA,
            },
        )
    dados = ler_token_2fa(token_2fa)
    if str(dados.get("sub")) != str(admin.id):
        raise HTTPException(
            status_code=401,
            detail="A verificação 2FA pertence a outra conta. Informe o código novamente.",
            headers={CABECALHO_2FA_REQUERIDO: "codigo"},
        )
    return admin

