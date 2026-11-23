"""Promove uma conta a administrador do sistema (acesso à Auditoria).

A flag `usuarios.is_admin` nasce FALSA para todas as contas — inclusive as criadas antes
da Auditoria existir. Só um comando local (com acesso ao banco) promove alguém, para que
ninguém consiga se tornar administrador pela API/web. É também o comando de recuperação
do 2FA: quem perdeu o celular/autenticador roda `--resetar-2fa` e, no próximo login, o
QR Code aparece na tela para cadastrar o aparelho novo (o login do admin é fail-closed:
sem 2FA não entra).

Uso:
    python criar_admin.py                          # lista as contas, o perfil e o 2FA de cada uma
    python criar_admin.py rodrigo@exemplo.com      # promove a conta a administrador
    python criar_admin.py rodrigo@exemplo.com --remover   # volta a conta para usuário comum
    python criar_admin.py rodrigo@exemplo.com --resetar-2fa  # apaga o segredo (recadastra o QR)
"""

import sys

from sqlalchemy import func

from backend.database import SessionLocal, init_db
from backend.models import Usuario


def _perfil(usuario) -> str:
    """Coluna de perfil do comando de listagem: admin + situação do 2FA."""
    papel = "ADMIN  " if usuario.is_admin else "usuário"
    if getattr(usuario, "totp_secret", None):
        autenticacao = "2FA ativo" if getattr(usuario, "totp_ativo", False) else "2FA pendente"
    else:
        autenticacao = "sem 2FA"
    return f"{papel}  {autenticacao:<12}"


def _listar_contas(db) -> int:
    usuarios = db.query(Usuario).order_by(Usuario.id).all()
    if not usuarios:
        print("Nenhuma conta cadastrada ainda. Crie a conta pela tela de login do app.")
        return 0
    print("Contas cadastradas:")
    for usuario in usuarios:
        print(f"  {_perfil(usuario)}  id={usuario.id:<4} {usuario.email}")
    print(
        "\nUso: python criar_admin.py <e-mail> [--remover] [--resetar-2fa]\n"
        "     2FA ativo = código de 6 dígitos exigido no LOGIN e na Auditoria;"
        " 2FA pendente = QR gerado e ainda não confirmado."
    )
    return 0


def main(argv) -> int:
    init_db()
    emails = [argumento for argumento in argv if not argumento.startswith("-")]
    remover = any(argumento in ("--remover", "--rebaixar", "-r") for argumento in argv)
    resetar_2fa = "--resetar-2fa" in argv

    db = SessionLocal()
    try:
        if not emails:
            return _listar_contas(db)

        email = emails[0].strip().lower()
        usuario = db.query(Usuario).filter(func.lower(Usuario.email) == email).first()
        if not usuario:
            print(f"Não encontrei a conta {email!r}. Use `python criar_admin.py` para listar.")
            return 1

        if resetar_2fa:
            # Recuperação: sem segredo e com totp_ativo=False o próximo LOGIN do admin já
            # mostra o QR Code na tela (o login é fail-closed até o 2FA ser configurado).
            usuario.totp_secret = None
            usuario.totp_ativo = False
            db.commit()
            print(
                f"2FA removido da conta {usuario.email}. No próximo login o QR Code aparece "
                "na tela para cadastrar o autenticador de novo."
            )
            return 0

        usuario.is_admin = not remover
        db.commit()
        db.refresh(usuario)
        if usuario.is_admin:
            print(f"Conta {usuario.email} agora é ADMINISTRADORA (Auditoria liberada).")
        else:
            print(f"Conta {usuario.email} voltou a ser usuária comum (Auditoria bloqueada).")
        print("Faça login de novo no app para o menu ser atualizado.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
