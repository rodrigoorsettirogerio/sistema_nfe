from backend.database import SessionLocal
from backend.models import Usuario, Empresa

db = SessionLocal()
email_maira = 'sac@portucho.com.br'
u = db.query(Usuario).filter(Usuario.email == email_maira).first()

if u:
    print(f"ID do usuário Maira: {u.id}")
else:
    print("Usuário Maira não encontrado.")

print("\n--- Lojas vinculadas à Maira ---")
if u:
    empresas_maira = db.query(Empresa).filter(Empresa.usuario_id == u.id).all()
    if empresas_maira:
        for e in empresas_maira:
            print(f" - CNPJ: {e.cnpj} | ID Dono: {e.usuario_id}")
    else:
        print("Nenhuma loja vinculada a ela.")

print("\n--- TODAS as lojas no banco ---")
todas = db.query(Empresa).all()
for e in todas:
    print(f" - CNPJ: {e.cnpj} | ID Dono: {e.usuario_id}")

db.close()