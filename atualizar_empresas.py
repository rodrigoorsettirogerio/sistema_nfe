"""Correção dos cadastros das lojas do grupo (script de manutenção, roda uma vez).

Regra de negócio dos nomes:
- 07.269.638/0001-71 -> **Portucho Matriz** (São Paulo);
- 32.341.007/0001-63 -> **Portucho & Deck** (Cotia);
- 50.951.160/0001-94 -> **Moça Bar** (loja fechada, mantida para os dados históricos).

O CNPJ 50.951.160/0001-94 estava gravado como "Deck Burger": o nome correto é "Moça Bar".
A razão social desse CNPJ na Receita (BrasilAPI) é MOCA BAR E LANCHONETE LTDA — é ela que
o cadastro novo grava (o `/api/empresas/cadastrar` sempre usa o Nome Fantasia da Receita).

Uso:
    python atualizar_empresas.py              # aplica as correções
    python atualizar_empresas.py --dry-run    # só mostra o que mudaria (não grava)
"""

import sys

from backend.database import SessionLocal, init_db
from backend.models import Empresa

# CNPJ (só dígitos) -> dados do cadastro que o grupo usa no dia a dia.
LOJAS = {
    "07269638000171": {
        "nome_loja": "Portucho Matriz",
        "razao_social": "Portucho Comércio de Alimentos",
    },
    "32341007000163": {
        "nome_loja": "Portucho & Deck",
        "razao_social": "Portucho & Deck Burger",
    },
    "50951160000194": {
        "nome_loja": "Moça Bar",
        "razao_social": "Moca Bar e Lanchonete Ltda",
    },
}


def main(argv) -> int:
    simular = any(argumento in ("--dry-run", "--simular", "-n") for argumento in argv)
    init_db()
    db = SessionLocal()
    try:
        for cnpj, dados in LOJAS.items():
            empresa = db.query(Empresa).filter(Empresa.cnpj == cnpj).first()
            if empresa is None:
                print(f"  CNPJ {cnpj}: loja não encontrada no banco (nada a fazer).")
                continue
            antes = f"{empresa.nome_loja!r} / {empresa.razao_social!r}"
            empresa.nome_loja = dados["nome_loja"]
            empresa.razao_social = dados["razao_social"]
            print(
                f"  CNPJ {cnpj}: {antes} -> "
                f"{empresa.nome_loja!r} / {empresa.razao_social!r}"
            )
        if simular:
            db.rollback()
            print("\n--dry-run: nada foi gravado.")
        else:
            db.commit()
            print("\nEmpresas atualizadas com sucesso.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
