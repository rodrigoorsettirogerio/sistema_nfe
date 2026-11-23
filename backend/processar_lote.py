import os
from database import init_db
from xml_parser import processar_e_salvar_xml

db_path = "../sistema_nfe_pro.db"

# Remove o banco antigo corrompido para garantir que a estrutura nova seja criada limpa
if os.path.exists(db_path):
    try:
        os.remove(db_path)
        print("🗑️ Banco de dados antigo removido para recriar com a estrutura atualizada.")
    except Exception as e:
        print(f"⚠️ Aviso ao remover banco antigo: {e}")

# Inicializa o banco e cria todas as tabelas e colunas
print("🔧 Inicializando o banco de dados e criando tabelas...")
init_db()

# Caminho para a pasta de XMLs na raiz
pasta_xmls = "../xmls"

if not os.path.exists(pasta_xmls):
    print(f"⚠️ A pasta '{pasta_xmls}' não foi encontrada.")
else:
    arquivos = [f for f in os.listdir(pasta_xmls) if f.lower().endswith('.xml')]
    print(f"📁 Encontrados {len(arquivos)} arquivos XML na pasta. Iniciando processamento...\n")
    
    for arquivo in arquivos:
        caminho_completo = os.path.join(pasta_xmls, arquivo)
        try:
            processar_e_salvar_xml(caminho_completo, db_path=db_path)
        except Exception as e:
            print(f"❌ Erro crítico ao processar {arquivo}: {e}")

    print("\n🎉 Processamento do lote concluído com sucesso!")