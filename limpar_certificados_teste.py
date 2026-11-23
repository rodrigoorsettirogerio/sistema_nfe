"""Limpeza dos certificados de TESTE das lojas (mantém o histórico do Moça Bar).

Os certificados das lojas do grupo foram preenchidos à mão ("preenchimento-manual",
validade 11/11/2027) só para validar a tela de vínculo — não são certificados reais. A
regra do cliente é:

- apagar o certificado FICTÍCIO do **Portucho Matriz** (07.269.638/0001-71) e do
  **Portucho & Deck** (32.341.007/0001-63);
- **preservar** o certificado do **Moça Bar** (50.951.160/0001-94): é a loja fechada que
  continua no banco para os testes com dados históricos.

O que é apagado: apenas `empresas.certificado_titular`, `certificado_validade` e
`certificado_enviado_em`. A loja, o dono e todas as notas fiscais continuam intactos.
O mesmo efeito do botão "🗑️ Excluir Certificado" da tela "Minhas Lojas".

Uso:
    python limpar_certificados_teste.py                     # limpa (preserva o Moça Bar)
    python limpar_certificados_teste.py --dry-run           # só mostra o que faria
    python limpar_certificados_teste.py --manter 07269638000171
"""

import sys

from backend.database import SessionLocal, init_db
from backend.models import Empresa

# Loja preservada por padrão: Moça Bar (dados históricos dos testes).
CNPJ_PRESERVADO = "50951160000194"


def _certificado(empresa) -> str:
    """Resumo do certificado da loja para o log."""
    if not (empresa.certificado_titular or empresa.certificado_validade):
        return "sem certificado"
    return (
        f"titular={empresa.certificado_titular!r} "
        f"validade={empresa.certificado_validade!r} "
        f"enviado_em={empresa.certificado_enviado_em!r}"
    )


def main(argv) -> int:
    simular = any(argumento in ("--dry-run", "--simular", "-n") for argumento in argv)
    preservado = CNPJ_PRESERVADO
    if "--manter" in argv:
        indice = argv.index("--manter")
        if indice + 1 < len(argv):
            preservado = "".join(
                caractere for caractere in argv[indice + 1] if caractere.isdigit()
            )
    print(f"Preservando o certificado do CNPJ {preservado} (Moça Bar).")
    init_db()
    db = SessionLocal()
    try:
        apagados = 0
        for empresa in db.query(Empresa).order_by(Empresa.id).all():
            cnpj = "".join(caractere for caractere in str(empresa.cnpj or "") if caractere.isdigit())
            if cnpj == preservado:
                print(f"  CNPJ {cnpj}: preservado ({_certificado(empresa)})")
                continue
            if not (empresa.certificado_titular or empresa.certificado_validade):
                print(f"  CNPJ {cnpj}: já estava sem certificado")
                continue
            print(f"  CNPJ {cnpj}: apagando {_certificado(empresa)}")
            empresa.certificado_titular = None
            empresa.certificado_validade = None
            empresa.certificado_enviado_em = None
            apagados += 1
        if simular:
            db.rollback()
            print(f"\n--dry-run: nada foi gravado ({apagados} certificado(s) seriam apagados).")
        else:
            db.commit()
            print(f"\n{apagados} certificado(s) apagado(s). Lojas e notas intactas.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
