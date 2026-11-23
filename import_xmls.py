import os
import glob
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from backend.database import SessionLocal
from backend.marcas import recalcular_marcas
from backend.models import (
    Empresa,
    Fornecedor,
    ItemNota,
    MapeamentoFornecedorProduto,
    NotaFiscal,
)
from backend.aprendizado_produtos import (
    aplicar_embalagem_aprendida,
    aplicar_identidade,
    normalizar_cnpj,
    normalizar_codigo_produto,
    normalizar_quantidades,
    interpretar_embalagem,
    registrar_aprendizado,
)

# ──────────────────────────────────────────────────────────────────────────────
# Bonificação / doação / brinde
# ──────────────────────────────────────────────────────────────────────────────
# CFOPs de remessa em bonificação, doação ou brinde (dentro e fora do estado).
CFOPS_BONIFICACAO = {"5910", "6910"}
# Termos que identificam brinde/doação no texto do item (sem acento e com acento).
TERMOS_BONIFICACAO = ("BONIFIC", "BRINDE", "DOACAO", "DOAÇÃO", "DOAÃ")


def _texto_item(elemento, tag, ns):
    """Texto de uma tag do XML (com ou sem namespace), já sem espaços."""
    if elemento is None:
        return ""
    # Sem `or` entre os `find`: um Element sem filhos é "falso" no ElementTree, então a
    # cadeia de `or` descartaria justamente as tags folha (vUnCom, vProd, CFOP...).
    for caminho in (f"nfe:{tag}", f"{{http://www.portalfiscal.inf.br/nfe}}{tag}", tag):
        achado = elemento.find(caminho, ns)
        if achado is not None and achado.text:
            return achado.text.strip()
    return ""


def _numero_item(elemento, tag, ns):
    texto = _texto_item(elemento, tag, ns)
    try:
        return float(texto)
    except ValueError:
        return 0.0


def item_bonificado(prod, det, ns):
    """True quando o item é bonificação/doação/brinde (não entra na média de preço).

    Qualquer um dos critérios basta:
    - valor unitário R$ 0,00 (o mais comum: item veio de brinde);
    - CFOP de remessa em bonificação/doação (5910/6910);
    - desconto igual ao valor do produto (brinde com 100% de desconto);
    - descrição ou informação adicional citando bonificação/brinde/doação.
    """
    if prod is None:
        return True
    if _numero_item(prod, "vUnCom", ns) <= 0:
        return True
    if _texto_item(prod, "CFOP", ns) in CFOPS_BONIFICACAO:
        return True
    valor_produto = _numero_item(prod, "vProd", ns)
    desconto = _numero_item(prod, "vDesc", ns)
    if valor_produto > 0 and desconto >= valor_produto:
        return True
    texto = f"{_texto_item(prod, 'xProd', ns)} {_texto_item(det, 'infAdProd', ns)}".upper()
    return any(termo in texto for termo in TERMOS_BONIFICACAO)


def _empresa_por_destinatario(db, infNFe, ns, empresa_padrao):
    """Loja (empresa) dona da nota: o CNPJ do DESTINATÁRIO do XML.

    Multilojas: cada CNPJ do grupo é uma loja. A nota de cada XML é vinculada à empresa
    do destinatário — é isso que faz o filtro de loja e o comparativo entre lojas
    mostrarem as compras certas. Sem `dest/CNPJ` legível a nota fica com a empresa padrão
    (nada é perdido). Um destinatário novo é cadastrado na hora usando a razão social.
    """
    dest = infNFe.find('nfe:dest', ns)
    if dest is None:
        dest = infNFe.find('{http://www.portalfiscal.inf.br/nfe}dest')
    if dest is None:
        dest = infNFe.find('dest')  # XML sem namespace (testes/emitentes antigos)
    if dest is None:
        return empresa_padrao
    cnpj = re.sub(r"\D", "", _texto_item(dest, "CNPJ", ns))
    if not cnpj:
        return empresa_padrao
    empresa = db.query(Empresa).filter(Empresa.cnpj == cnpj).first()
    if empresa is not None:
        return empresa
    nome = _texto_item(dest, "xNome", ns) or f"Loja {cnpj}"
    empresa = Empresa(
        cnpj=cnpj,
        razao_social=nome,
        nome_loja=nome,
        municipio=_texto_item(dest, "xMun", ns),
    )
    db.add(empresa)
    db.commit()
    db.refresh(empresa)
    print(f"  + loja cadastrada pelo destinatário da nota: {empresa.nome_loja} ({cnpj})")
    return empresa


def importar_com_ean_e_ncm(
    *,
    pastas=None,
    limpar=True,
    cnpj_padrao=None,
    apenas_novos=True,
    silencioso=False,
):
    """Importa os XMLs para o banco.

    O comportamento padrão é o do script de linha de comando: apaga as notas/itens
    antigos e varre ``xmls/**``. A automação após o cadastro do certificado usa a
    mesma rotina em modo incremental:

    - ``pastas``: diretório(s) a varrer. Sem ele, usa ``xmls/**/*.xml``;
    - ``limpar``: quando False, preserva as notas já existentes;
    - ``apenas_novos``: ignora chaves de acesso já gravadas (evita duplicidade e
      permite repetir o download sem reprocessar tudo);
    - ``cnpj_padrao``: loja usada para notas sem destinatário identificado.
    """
    db = SessionLocal()

    def _log(*partes):
        if not silencioso:
            print(*partes)

    # Garante a criação das tabelas (e das colunas novas, ex.: itens_nota.cfop).
    from backend.database import init_db

    init_db()

    if limpar:
        _log("Limpando dados antigos...")
        try:
            db.query(ItemNota).delete()
            db.query(NotaFiscal).delete()
            db.commit()
        except Exception:
            db.rollback()

    empresa = None
    cnpj_padrao_limpo = re.sub(r"\D", "", str(cnpj_padrao or ""))
    if cnpj_padrao_limpo:
        empresa = (
            db.query(Empresa).filter(Empresa.cnpj == cnpj_padrao_limpo).first()
        )
    if empresa is None:
        empresa = db.query(Empresa).first()
    if not empresa:
        empresa = Empresa(
            cnpj="07269638000171",
            razao_social="Portucho Comércio de Alimentos",
            nome_loja="Portucho Matriz",
            municipio="São Paulo"
        )
        db.add(empresa)
        db.commit()
        db.refresh(empresa)

    _log(
        "Empresa padrão (notas sem destinatário identificado): "
        f"{empresa.nome_loja} (ID: {empresa.id})"
    )

    if pastas is None:
        caminho_busca = os.path.join("xmls", "**", "*.xml")
        arquivos = glob.glob(caminho_busca, recursive=True)
    else:
        if isinstance(pastas, (str, bytes, os.PathLike)):
            pastas = [pastas]
        arquivos = []
        for pasta in pastas:
            caminho_busca = os.path.join(os.fspath(pasta), "**", "*.xml")
            arquivos.extend(glob.glob(caminho_busca, recursive=True))
        arquivos = sorted(set(arquivos))
    _log(f"Encontrados {len(arquivos)} arquivos XML nas pastas.")

    importadas = 0
    erros = 0
    bonificados = 0
    ignoradas = 0
    # Memória de aprendizado: dicionário em memória para não fazer uma consulta por item.
    # As chaves são (CNPJ do fornecedor, cProd) e os valores são os mapeamentos já salvos.
    mapa_aprendizado = {
        (mapeamento.cnpj_fornecedor, mapeamento.c_prod): mapeamento
        for mapeamento in db.query(MapeamentoFornecedorProduto).all()
    }
    mapeamentos_criados = 0
    mapeamentos_usados = 0
    embalagens_normalizadas = 0
    ns = {'nfe': 'http://www.portalfiscal.inf.br/nfe'}

    for arq in arquivos:
        try:
            tree = ET.parse(arq)
            root = tree.getroot()
            
            infNFe = root.find('.//nfe:infNFe', ns)
            if infNFe is None:
                infNFe = root.find('.//{http://www.portalfiscal.inf.br/nfe}infNFe')
            
            if infNFe is None:
                erros += 1
                continue
            
            chave = infNFe.attrib.get('Id', '').replace('NFe', '').strip()
            if not chave:
                erros += 1
                continue

            if apenas_novos:
                ja_importada = (
                    db.query(NotaFiscal.id)
                    .filter(NotaFiscal.chave_acesso == chave)
                    .first()
                )
                if ja_importada is not None:
                    ignoradas += 1
                    continue

            emit = infNFe.find('nfe:emit', ns)
            if emit is None:
                emit = infNFe.find('{http://www.portalfiscal.inf.br/nfe}emit')

            cnpj_emit = "00000000000000"
            nome_emit = "Fornecedor Desconhecido"

            if emit is not None:
                c_cnpj = emit.find('nfe:CNPJ', ns) or emit.find('{http://www.portalfiscal.inf.br/nfe}CNPJ')
                c_nome = emit.find('nfe:xNome', ns) or emit.find('{http://www.portalfiscal.inf.br/nfe}xNome')
                if c_cnpj is not None and c_cnpj.text:
                    cnpj_emit = c_cnpj.text.strip()
                if c_nome is not None and c_nome.text:
                    nome_emit = c_nome.text.strip()

            fornecedor = db.query(Fornecedor).filter(Fornecedor.cnpj == cnpj_emit).first()
            if not fornecedor:
                fornecedor = Fornecedor(cnpj=cnpj_emit, nome=nome_emit)
                db.add(fornecedor)
                db.commit()
                db.refresh(fornecedor)

            ide = infNFe.find('nfe:ide', ns) or infNFe.find('{http://www.portalfiscal.inf.br/nfe}ide')
            
            numero_nota = "0"
            serie = "1"
            data_emissao = datetime.strptime("2026-01-01", "%Y-%m-%d").date()

            if ide is not None:
                nNF = ide.find('nfe:nNF', ns) or ide.find('{http://www.portalfiscal.inf.br/nfe}nNF')
                serie_tag = ide.find('nfe:serie', ns) or ide.find('{http://www.portalfiscal.inf.br/nfe}serie')
                dhEmi = ide.find('nfe:dhEmi', ns) or ide.find('{http://www.portalfiscal.inf.br/nfe}dhEmi')
                dEmi = ide.find('nfe:dEmi', ns) or ide.find('{http://www.portalfiscal.inf.br/nfe}dEmi')

                if nNF is not None and nNF.text:
                    numero_nota = nNF.text.strip()
                if serie_tag is not None and serie_tag.text:
                    serie = serie_tag.text.strip()
                
                data_text = dhEmi if dhEmi is not None else dEmi
                if data_text is not None and data_text.text:
                    try:
                        data_emissao = datetime.strptime(data_text.text.strip()[:10], "%Y-%m-%d").date()
                    except:
                        pass

            total_tag = infNFe.find('.//nfe:vNF', ns) or infNFe.find('.//{http://www.portalfiscal.inf.br/nfe}vNF')
            valor_total_nota = float(total_tag.text.strip()) if total_tag is not None and total_tag.text else 0.0

            # Cada nota pertence à loja (CNPJ) que aparece como destinatária no XML.
            empresa_nota = _empresa_por_destinatario(db, infNFe, ns, empresa)

            nova_nota = NotaFiscal(
                chave_acesso=chave,
                numero_nota=numero_nota,
                serie=serie,
                data_emissao=data_emissao,
                empresa_id=empresa_nota.id,
                valor_total=valor_total_nota,
                caminho_xml=arq
            )
            db.add(nova_nota)
            db.commit()
            db.refresh(nova_nota)

            det_items = infNFe.findall('nfe:det', ns)
            if not det_items:
                det_items = infNFe.findall('{http://www.portalfiscal.inf.br/nfe}det')

            for det in det_items:
                prod = det.find('nfe:prod', ns) or det.find('{http://www.portalfiscal.inf.br/nfe}prod')
                
                v_prod_tag = det.find('.//nfe:vProd', ns) or det.find('.//{http://www.portalfiscal.inf.br/nfe}vProd')
                v_prod_total = float(v_prod_tag.text.strip()) if v_prod_tag is not None and v_prod_tag.text else 0.0

                if prod is not None:
                    # Bonificação/doação/brinde não entra na base de preços (regra de negócio):
                    # o item não tem preço real, então distorceria a média dos relatórios.
                    if item_bonificado(prod, det, ns):
                        bonificados += 1
                        continue

                    cEAN = prod.find('nfe:cEAN', ns) or prod.find('{http://www.portalfiscal.inf.br/nfe}cEAN')
                    xProd = prod.find('nfe:xProd', ns) or prod.find('{http://www.portalfiscal.inf.br/nfe}xProd')
                    qCom = prod.find('nfe:qCom', ns) or prod.find('{http://www.portalfiscal.inf.br/nfe}qCom')
                    vUnCom = prod.find('nfe:vUnCom', ns) or prod.find('{http://www.portalfiscal.inf.br/nfe}vUnCom')
                    c_ncm = prod.find('nfe:NCM', ns) or prod.find('{http://www.portalfiscal.inf.br/nfe}NCM')

                    raw_ean = cEAN.text.strip() if cEAN is not None and cEAN.text else ""
                    if raw_ean and raw_ean.upper() not in ["SEM GTIN", "SEM_EAN", ""]:
                        ean = raw_ean.lstrip('0')
                        if not ean:
                            ean = "0"
                    else:
                        ean = "SEM_EAN"

                    desc = xProd.text.strip() if xProd is not None and xProd.text else ""
                    qtd = float(qCom.text.strip()) if qCom is not None and qCom.text else 0.0
                    v_un = float(vUnCom.text.strip()) if vUnCom is not None and vUnCom.text else 0.0
                    ncm = c_ncm.text.strip() if c_ncm is not None and c_ncm.text else "00000000"
                    # CFOP do item: 5102/5405 etc. = compra normal; 5910/6910 = bonificação.
                    cfop = _texto_item(prod, "CFOP", ns) or "0000"

                    # ── Memória de aprendizado / embalagem ──────────────────────────
                    # cProd = código do produto NO fornecedor. É a chave estável do
                    # de-para: se ele já foi aprendido, o EAN/descrição/NCM canônicos
                    # substituem o que veio no XML (inclusive "SEM GTIN").
                    c_prod = _texto_item(prod, "cProd", ns)
                    u_com = _texto_item(prod, "uCom", ns)
                    chave_aprendizado = None
                    mapeamento = None
                    if c_prod:
                        chave_aprendizado = (
                            normalizar_cnpj(cnpj_emit),
                            normalizar_codigo_produto(c_prod),
                        )
                        mapeamento = mapa_aprendizado.get(chave_aprendizado)

                    ean_xml, desc_xml, ncm_xml = ean, desc, ncm
                    if mapeamento is not None:
                        mapeamentos_usados += 1
                        ean, desc, ncm = aplicar_identidade(mapeamento, ean, desc, ncm)

                    # A embalagem é lida da descrição canônica (mais completa) e pode
                    # ser completada pelo fator já aprendido para o mesmo cProd.
                    embalagem = interpretar_embalagem(u_com, desc)
                    embalagem = aplicar_embalagem_aprendida(embalagem, mapeamento)
                    quantidade_base, valor_unitario_base = normalizar_quantidades(
                        qtd, v_un, embalagem
                    )
                    if embalagem.get("confirmada") and float(
                        embalagem.get("qtd_por_embalagem") or 1.0
                    ) > 1.0:
                        embalagens_normalizadas += 1

                    if chave_aprendizado:
                        mapeamento, criado, _avisos = registrar_aprendizado(
                            db,
                            chave_aprendizado[0],
                            chave_aprendizado[1],
                            ean=ean_xml,
                            descricao=desc_xml,
                            ncm=ncm_xml,
                            embalagem=embalagem,
                            mapeamento=mapeamento,
                            commit=False,
                        )
                        mapa_aprendizado[chave_aprendizado] = mapeamento
                        if criado:
                            mapeamentos_criados += 1

                    # `marca` fica NULL aqui e é preenchida no fim da importação por
                    # recalcular_marcas(): a extração usa o corpus de descrições do NCM.
                    item = ItemNota(
                        nota_id=nova_nota.id,
                        fornecedor_id=fornecedor.id,
                        ean=ean,
                        descricao_produto=desc,
                        quantidade=qtd,
                        valor_unitario=v_un,
                        valor_total=v_prod_total,
                        ncm=ncm,  # <--- AGORA O NCM É SALVO CORRETAMENTE NO BANCO!
                        cfop=cfop,
                        c_prod=c_prod or None,
                        unidade_compra=u_com or None,
                        qtd_por_embalagem=float(
                            embalagem.get("qtd_por_embalagem") or 1.0
                        ),
                        unidade_base=str(embalagem.get("unidade_base") or "UN"),
                        quantidade_base=quantidade_base,
                        valor_unitario_base=valor_unitario_base,
                    )
                    db.add(item)
            db.commit()
            importadas += 1
        except Exception as e:
            db.rollback()
            erros += 1
            
    _log(
        f"Importação concluída! Notas importadas: {importadas} (Erros: {erros}) | "
        f"Chaves já existentes ignoradas: {ignoradas} | "
        f"Itens de bonificação/doação ignorados: {bonificados}"
    )
    _log(
        f"Memória de aprendizado: {mapeamentos_usados} itens reconhecidos | "
        f"{mapeamentos_criados} produtos novos aprendidos | "
        f"{embalagens_normalizadas} itens com embalagem normalizada"
    )

    # ── Marca de cada item (NLP leve, ver backend/marcas.py) ─────────────────────
    # A marca é a palavra que VARIA entre as descrições do mesmo NCM ("... FINA YOKI" x
    # "... FINA MATHEUS"). A conta roda DEPOIS da importação porque precisa do corpus
    # completo do NCM; o resultado alimenta só o tooltip de marcas substitutas — nenhum
    # preço médio depende dessa coluna.
    # `apenas_cache=True`: o importador NÃO chama a DeepSeek (import não deve gastar
    # tokens). Ele reaproveita o que já está em `marcas_ia`, preserva as marcas vindas da
    # IA e usa a heurística local para as descrições ainda inéditas. O botão da tela roda
    # a análise das inéditas depois.
    resumo_marcas = recalcular_marcas(db, apenas_cache=True)
    _log(
        f"Marcas identificadas: {resumo_marcas['com_marca']} itens | "
        f"{resumo_marcas['ncms_com_marca']} NCMs analisados | "
        f"{resumo_marcas['atualizados']} registros gravados | "
        f"{resumo_marcas['cache_hits']} descrições vieram da memória da IA"
    )
    if resumo_marcas.get("erro_ia"):
        _log(f"AVISO da memória de marcas: {resumo_marcas['erro_ia']}")

    db.close()
    return {
        "arquivos": len(arquivos),
        "importadas": importadas,
        "ignoradas": ignoradas,
        "erros": erros,
        "bonificados": bonificados,
        "mapeamentos_usados": mapeamentos_usados,
        "mapeamentos_criados": mapeamentos_criados,
        "embalagens_normalizadas": embalagens_normalizadas,
        "marcas": resumo_marcas,
    }

if __name__ == "__main__":
    importar_com_ean_e_ncm()