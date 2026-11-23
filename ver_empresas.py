from backend.database import SessionLocal
from backend.models import Empresa

db = SessionLocal()
empresas = db.query(Empresa).all()

print("--- EMPRESAS CADASTRADAS NO BANCO ---")
if not empresas:
    print("Nenhuma empresa cadastrada no banco de dados!")
else:
    for e in empresas:
        print(f"ID: {e.id} | CNPJ: '{e.cnpj}' (Total de dígitos: {len(e.cnpj)}) | Nome: {e.nome_loja}")
db.close()