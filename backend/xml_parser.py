import xml.etree.ElementTree as ET
import sqlite3
from difflib import SequenceMatcher

def processar_e_salvar_xml(caminho_xml, db_path="../sistema_nfe_pro.db"):
    conexao = sqlite3.connect(db_path)
    cursor = conexao.cursor()
    
    try:
        tree = ET.parse(caminho_xml)
        root = tree.getroot()
        namespace = {'nfe': 'http://www.portalfiscal.inf.br/nfe'}
        
        # Dados da Nota e Fornecedor
        emitente = root.find('.//nfe:emit', namespace)
        cnpj_emitente = emitente.find('nfe:CNPJ', namespace).text if emitente is not None and emitente.find('nfe:CNPJ', namespace) is not None else ""
        nome_emitente = emitente.find('nfe:xNome', namespace).text if emitente is not None and emitente.find('nfe:xNome', namespace) is not None else ""
        
        ide = root.find('.//nfe:ide', namespace)
        data_emissao = ide.find('nfe:dhEmi', namespace).text[:10] if ide is not None and ide.find('nfe:dhEmi', namespace) is not None else ""
        
        cursor.execute("INSERT OR IGNORE INTO fornecedores (cnpj, nome) VALUES (?, ?)", (cnpj_emitente, nome_emitente))
        conexao.commit()
        
        print(f"\n📄 Processando Nota de: {nome_emitente} | Data: {data_emissao}")
        print("-" * 70)
        
        itens_inseridos = 0
        
        for det in root.findall('.//nfe:det', namespace):
            prod = det.find('nfe:prod', namespace)
            
            ean = prod.find('nfe:cEAN', namespace).text if prod.find('nfe:cEAN', namespace) is not None else ""
            c_prod = prod.find('nfe:cProd', namespace).text if prod.find('nfe:cProd', namespace) is not None else ""
            descricao_nota = prod.find('nfe:xProd', namespace).text
            ncm_novo = prod.find('nfe:NCM', namespace).text if prod.find('nfe:NCM', namespace) is not None else ""
            u_com = prod.find('nfe:uCom', namespace).text if prod.find('nfe:uCom', namespace) is not None else ""
            quantidade = float(prod.find('nfe:qCom', namespace).text)
            valor_unitario = float(prod.find('nfe:vUnCom', namespace).text)
            
            ean_final = None
            descricao_final = descricao_nota

            # --- CAMADA 1: TENTATIVA VIA CÓDIGO DE BARRAS (EAN) ---
            if ean and ean != 'SEM GTIN' and len(ean) > 7:
                ean_final = ean
                cursor.execute("SELECT descricao, ncm FROM produtos WHERE ean = ?", (ean_final,))
                prod_db = cursor.fetchone()
                
                if not prod_db:
                    cursor.execute("INSERT OR IGNORE INTO produtos (ean, descricao, ncm) VALUES (?, ?, ?)", (ean_final, descricao_nota, ncm_novo))
                    conexao.commit()
                else:
                    descricao_final = prod_db[0]
            
            # --- CAMADA 2: INTELIGÊNCIA DE cProd + NCM + UNIDADE DE MEDIDA ---
            else:
                # Verifica mapeamento existente considerando o fornecedor e o código interno
                cursor.execute("""
                    SELECT ean_padrao, descricao_padrao, ncm_gravado, u_com_gravada 
                    FROM mapeamento_fornecedor_produtos 
                    WHERE cnpj_fornecedor = ? AND c_prod = ?
                """, (cnpj_emitente, c_prod))
                mapeamento = cursor.fetchone()
                
                if mapeamento:
                    ean_padrao, descricao_final, ncm_gravado, u_com_gravada = mapeamento
                    ean_final = ean_padrao
                    
                    # Validação de segurança: Se o NCM mudou drasticamente na nota, aciona alerta de divergência
                    if ncm_gravado and ncm_novo and ncm_gravado != ncm_novo:
                        print(f"⚠️ [ALERTA RETAGUARDA] cProd '{c_prod}' alterou o NCM de {ncm_gravado} para {ncm_novo} ('{descricao_nota}').")
                else:
                    # Faz o matching semântico no catálogo por similaridade de nome
                    cursor.execute("SELECT ean, descricao FROM produtos")
                    catalogo = cursor.fetchall()
                    
                    melhor_match = None
                    maior_similaridade = 0.0
                    
                    for p_ean, p_desc in catalogo:
                        sim = SequenceMatcher(None, descricao_nota.lower(), p_desc.lower()).ratio()
                        if sim > maior_similaridade:
                            maior_similaridade = sim
                            melhor_match = (p_ean, p_desc)
                    
                    if melhor_match and maior_similaridade >= 0.50:
                        ean_final, descricao_final = melhor_match
                    else:
                        ean_final = f"INT-{cnpj_emitente[:4]}-{c_prod}"
                        descricao_final = descricao_nota
                        cursor.execute("INSERT OR IGNORE INTO produtos (ean, descricao, ncm) VALUES (?, ?, ?)", (ean_final, descricao_nota, ncm_novo))
                    
                    # Salva o aprendizado completo na retaguarda (incluindo NCM e Unidade de Medida)
                    cursor.execute("""
                        INSERT OR IGNORE INTO mapeamento_fornecedor_produtos 
                        (cnpj_fornecedor, c_prod, ean_padrao, descricao_padrao, ncm_gravado, u_com_gravada)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (cnpj_emitente, c_prod, ean_final, descricao_final, ncm_novo, u_com))
                    conexao.commit()

            # Salva o item na tabela de compras
            cursor.execute("""
                INSERT INTO compras (ean, cnpj_fornecedor, data_emissao, quantidade, valor_unitario)
                VALUES (?, ?, ?, ?, ?)
            """, (ean_final, cnpj_emitente, data_emissao, quantidade, valor_unitario))
            conexao.commit()
            
            itens_inseridos += 1
            print(f"[✔️ Normalizado cProd] {c_prod} ({u_com}) -> {descricao_final} | R$ {valor_unitario:.2f}")
            
        print("-" * 70)
        print(f"Processamento concluído. {itens_inseridos} itens normalizados e salvos.\n")

    except Exception as e:
        print(f"❌ Erro ao processar o XML: {e}")
    finally:
        conexao.close()