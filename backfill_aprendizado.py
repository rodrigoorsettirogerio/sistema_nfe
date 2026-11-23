"""Popula a memória de aprendizado e as embalagens da base que já estava importada.

O importador (`python import_xmls.py`) já faz esse trabalho em cada nota nova. Este
script aplica o MESMO motor (`backend/aprendizado_produtos.sincronizar_aprendizado`)
na base existente:

- recalcula `itens_nota.qtd_por_embalagem` / `unidade_base` / `quantidade_base` /
  `valor_unitario_base` a partir da uCom + descrição de cada item;
- cria/atualiza o de-para (fornecedor + cProd) com o EAN/descrição/NCM/embalagem
  canônicos aprendidos;
- na base importada ANTES do de-para, relê os XMLs guardados em
  `notas_fiscais.caminho_xml` para recuperar o cProd/uCom de cada item (sem essa
  chave não há como aprender o produto do fornecedor).

É idempotente e NUNCA sobrescreve um mapeamento de origem "manual" (corrigido na
Auditoria). Os valores crus do XML (`quantidade`, `valor_unitario`) não são tocados.

Uso (na raiz do projeto):
    python backfill_aprendizado.py
"""

from collections import Counter

from backend.aprendizado_produtos import sincronizar_aprendizado
from backend.database import SessionLocal, init_db
from backend.models import ItemNota, MapeamentoFornecedorProduto


def main():
    init_db()  # garante as colunas novas (migração aditiva)
    db = SessionLocal()
    try:
        antes_embalagem = (
            db.query(ItemNota).filter(ItemNota.qtd_por_embalagem > 1.0).count()
        )
        antes_mapeamentos = db.query(MapeamentoFornecedorProduto).count()
        resumo = sincronizar_aprendizado(db)
        depois_embalagem = (
            db.query(ItemNota).filter(ItemNota.qtd_por_embalagem > 1.0).count()
        )
        depois_mapeamentos = db.query(MapeamentoFornecedorProduto).count()
        print(
            f"Itens na base: {resumo['itens']} | com embalagem identificada: "
            f"{depois_embalagem} (antes: {antes_embalagem}) | "
            f"sem cProd (não alimentam o de-para): {resumo['sem_codigo_fornecedor']}"
        )
        print(
            f"De-para (fornecedor + cProd): {depois_mapeamentos} registros "
            f"(antes: {antes_mapeamentos}) | criados: {resumo['mapeamentos_criados']} "
            f"| atualizados: {resumo['mapeamentos_atualizados']}"
        )
        print(
            f"XMLs relidos: {resumo['xmls_lidos']} "
            f"(não encontrados: {resumo['xmls_nao_encontrados']}) | "
            f"itens com cProd/uCom recuperados do XML: {resumo['itens_codigo_recuperado']}"
        )

        # Amostra: os fatores de embalagem mais comuns, com um exemplo de descrição.
        amostra = (
            db.query(
                ItemNota.unidade_compra,
                ItemNota.qtd_por_embalagem,
                ItemNota.unidade_base,
                ItemNota.descricao_produto,
            )
            .filter(ItemNota.qtd_por_embalagem > 1.0)
            .all()
        )
        contagem = Counter(
            (u_com or "?", fator, unidade or "UN")
            for u_com, fator, unidade, _ in amostra
        )
        exemplo = {}
        for u_com, fator, unidade, descricao in amostra:
            exemplo.setdefault((u_com or "?", fator, unidade or "UN"), descricao)
        if contagem:
            print("\nEmbalagens identificadas (uCom + fator):")
            for chave, quantidade in contagem.most_common(10):
                u_com, fator, unidade = chave
                print(
                    f"  {u_com:<4} x {fator:>6g} {unidade:<3} {quantidade:>5} itens | "
                    f"ex.: {exemplo[chave]}"
                )
        else:
            print(
                "\nNenhuma embalagem identificada: as descrições não trazem o fator "
                "(ex.: \"CX 24\", \"12X300ML\", \"CX 15KG\")."
            )
    finally:
        db.close()


if __name__ == "__main__":
    main()
