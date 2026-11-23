from xml_parser import processar_e_salvar_xml
import os

# Coloque aqui o nome exato do arquivo XML que você colocou na pasta
arquivo_teste = "exemplo_nota.xml" 

if os.path.exists(arquivo_teste):
    print("🚀 Iniciando teste de processamento e validação...")
    processar_e_salvar_xml(arquivo_teste)
else:
    print(f"⚠️ O arquivo '{arquivo_teste}' não foi encontrado na pasta backend. Insira um XML lá para testarmos!")