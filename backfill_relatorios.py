"""Preenche dados derivados do XML nas notas/itens que já estavam no banco.

Duas migrações de dados, ambas idempotentes (podem rodar quantas vezes quiser):

1. `itens_nota.cfop` — CFOP de cada item, lido do XML da nota. É o que permite excluir
   bonificação/doação/brinde (CFOP 5910/6910) dos relatórios de preço médio: o item não
   tem preço real de compra e distorce a média (caso HEINEKEN 600ml).
2. `notas_fiscais.empresa_id` — loja (CNPJ destinatário) dona da nota. As notas
   importadas antes da regra multilojas ficaram todas na primeira empresa, o que
   impedia o filtro por loja e o comparativo entre lojas de funcionar.

Uso (na raiz do projeto):
    python backfill_relatorios.py
"""

import glob
import os
import re
import xml.etree.ElementTree as ET

from backend.aprendizado_produtos import limpar_entidades_texto
from backend.database import PROJECT_ROOT, SessionLocal, init_db
from backend.models import Empresa, ItemNota, NotaFiscal

NS = {"nfe": "http://www.portalfiscal.inf.br/nfe"}


def _texto(elemento, tag):
    """Texto de uma tag do item (com ou sem namespace), já sem espaços."""
    if elemento is None:
        return ""
    for caminho in (f"nfe:{tag}", f"{{{NS['nfe']}}}{tag}", tag):
        achado = elemento.find(caminho, NS)
        if achado is not None and achado.text:
            return achado.text.strip()
    return ""


def _arquivo_da_nota(caminho_xml, chave_acesso):
    """Caminho absoluto do XML da nota (tenta a coluna, a pasta xmls e a chave)."""
    candidatos = []
    if caminho_xml:
        candidatos.append(os.path.join(PROJECT_ROOT, caminho_xml))
        candidatos.append(caminho_xml)
        candidatos.append(os.path.join(PROJECT_ROOT, "xmls", os.path.basename(caminho_xml)))
    for candidato in candidatos:
        if candidato and os.path.exists(candidato):
            return candidato
    if chave_acesso:
        achados = glob.glob(
            os.path.join(PROJECT_ROOT, "xmls", "**", f"*{chave_acesso}*.xml"), recursive=True
        )
        if achados:
            return achados[0]
    return None


def _inf_nfe(caminho):
    """Elemento <infNFe> do XML da nota (None quando o arquivo não é uma NFe).

    O XML do processo é `nfeProc > NFe > infNFe`, por isso a busca é por descendente
    (`.//`), e não por filho direto.
    """
    raiz = ET.parse(caminho).getroot()
    inf = raiz.find(".//nfe:infNFe", NS)
    if inf is None:
        inf = raiz.find(f".//{{{NS['nfe']}}}infNFe")
    return inf


def _cfops_do_xml(caminho):
    """Mapas (descricao+valor) -> cfop e descricao -> cfop de uma nota."""
    raiz = ET.parse(caminho).getroot()
    por_chave, por_descricao = {}, {}
    for det in raiz.iter(f"{{{NS['nfe']}}}det"):
        prod = det.find("nfe:prod", NS)
        if prod is None:
            prod = det.find("prod")
        if prod is None:
            continue
        cfop = _texto(prod, "CFOP")
        # O XML pode vir com entidade escapada ("&amp;"/"&amp;amp;"): a nota é gravada
        # com o texto limpo, então a chave de comparação também precisa ser limpa.
        descricao = limpar_entidades_texto(_texto(prod, "xProd")).strip().upper()
        if not cfop or not descricao:
            continue
        try:
            valor = round(float(_texto(prod, "vUnCom") or 0.0), 4)
        except ValueError:
            valor = 0.0
        por_chave[(descricao, valor)] = cfop
        # Só usa a descrição como atalho quando ela tem um único CFOP na nota.
        anterior = por_descricao.get(descricao)
        por_descricao[descricao] = cfop if anterior in (None, cfop) else ""
    return por_chave, por_descricao


def preencher_cfop(verboso=True):
    """Preenche `itens_nota.cfop` a partir dos XMLs. Devolve (atualizados, sem_xml)."""
    init_db()
    db = SessionLocal()
    atualizados = 0
    sem_xml = 0
    sem_correspondencia = 0
    try:
        for nota in db.query(NotaFiscal).all():
            itens = (
                db.query(ItemNota)
                .filter(
                    ItemNota.nota_id == nota.id,
                    (ItemNota.cfop.is_(None)) | (ItemNota.cfop == ""),
                )
                .all()
            )
            if not itens:
                continue
            caminho = _arquivo_da_nota(nota.caminho_xml, nota.chave_acesso)
            if not caminho:
                sem_xml += len(itens)
                continue
            try:
                por_chave, por_descricao = _cfops_do_xml(caminho)
            except Exception as erro:  # XML corrompido: segue para a próxima nota
                sem_xml += len(itens)
                if verboso:
                    print(f"  ! XML ilegível ({os.path.basename(caminho)}): {erro}")
                continue
            for item in itens:
                descricao = str(item.descricao_produto or "").strip().upper()
                valor = round(float(item.valor_unitario or 0.0), 4)
                cfop = por_chave.get((descricao, valor)) or por_descricao.get(descricao)
                if not cfop:
                    sem_correspondencia += 1
                    continue
                item.cfop = cfop
                atualizados += 1
            db.commit()
    finally:
        db.close()
    if verboso:
        print(
            f"Itens com CFOP preenchido: {atualizados} | "
            f"sem XML localizado: {sem_xml} | sem correspondência no XML: {sem_correspondencia}"
        )
    return atualizados, sem_xml


def vincular_notas_as_empresas(verboso=True):
    """Vincula cada nota à loja (empresa) do CNPJ DESTINATÁRIO do XML.

    As notas importadas antes da regra multilojas ficaram todas na primeira empresa.
    Destinatário sem cadastro é criado na hora (razão social do XML como nome da loja).
    Devolve (notas_alteradas, notas_sem_xml).
    """
    init_db()
    db = SessionLocal()
    alteradas = 0
    sem_xml = 0
    sem_destinatario = 0
    try:
        for nota in db.query(NotaFiscal).all():
            caminho = _arquivo_da_nota(nota.caminho_xml, nota.chave_acesso)
            if not caminho:
                sem_xml += 1
                continue
            try:
                inf = _inf_nfe(caminho)
            except Exception:
                sem_xml += 1
                continue
            dest = None
            if inf is not None:
                dest = inf.find("nfe:dest", NS)
                if dest is None:
                    dest = inf.find(f"{{{NS['nfe']}}}dest")
            cnpj = re.sub(r"\D", "", _texto(dest, "CNPJ"))
            if not cnpj:
                sem_destinatario += 1
                continue
            empresa = db.query(Empresa).filter(Empresa.cnpj == cnpj).first()
            if empresa is None:
                nome = _texto(dest, "xNome") or f"Loja {cnpj}"
                empresa = Empresa(
                    cnpj=cnpj,
                    razao_social=nome,
                    nome_loja=nome,
                    municipio=_texto(dest, "xMun"),
                )
                db.add(empresa)
                db.commit()
                db.refresh(empresa)
                if verboso:
                    print(f"  + loja cadastrada: {nome} ({cnpj})")
            if nota.empresa_id != empresa.id:
                nota.empresa_id = empresa.id
                alteradas += 1
        db.commit()
    finally:
        db.close()
    if verboso:
        print(
            f"Notas vinculadas à loja correta: {alteradas} | sem XML: {sem_xml} | "
            f"sem destinatário: {sem_destinatario}"
        )
        for nome, cnpj, quantidade in _notas_por_loja():
            print(f"   {nome} ({cnpj}): {quantidade} notas")
    return alteradas, sem_xml


def _notas_por_loja():
    """[(nome_loja, cnpj, quantidade de notas)] de todas as empresas cadastradas."""
    from sqlalchemy import func

    db = SessionLocal()
    try:
        linhas = (
            db.query(Empresa.nome_loja, Empresa.cnpj, func.count(NotaFiscal.id))
            .outerjoin(NotaFiscal, NotaFiscal.empresa_id == Empresa.id)
            .group_by(Empresa.id)
            .order_by(Empresa.id)
            .all()
        )
        return [(str(nome), str(cnpj), int(quantidade)) for nome, cnpj, quantidade in linhas]
    finally:
        db.close()


def main():
    print("1) CFOP dos itens (bonificação 5910/6910 fora dos relatórios)")
    preencher_cfop()
    print("\n2) Loja (CNPJ destinatário) de cada nota")
    vincular_notas_as_empresas()


if __name__ == "__main__":
    main()
