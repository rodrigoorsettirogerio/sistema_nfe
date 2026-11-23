"""Preenche `itens_nota.marca` na base que já estava importada.

A marca não vem no XML: ela é inferida pela variação das descrições do MESMO NCM
("PALHA DE MANDIOCA FINA YOKI" x "... MATHEUS" → YOKI / MATHEUS). O importador faz essa
conta sozinho no fim de `python import_xmls.py`; este script aplica a mesma rotina na base
existente (idempotente — pode rodar quantas vezes quiser).

A marca entra na chave inteligente dos relatórios (Produto + Marca): Arrico e
El Chef não compartilham a mesma média. O script só recalcula `itens_nota.marca`
— nenhum valor de compra é alterado; a média de cada grupo continua sendo a
média exata das suas próprias compras. A marca também alimenta o tooltip de
marcas substitutas.

Uso (na raiz do projeto):
    python backfill_marcas.py
"""

from collections import Counter

from backend.database import SessionLocal, init_db
from backend.marcas import recalcular_marcas
from backend.models import ItemNota


def main():
    init_db()  # garante a coluna itens_nota.marca (migração aditiva)
    db = SessionLocal()
    try:
        antes = db.query(ItemNota).filter(ItemNota.marca.isnot(None)).count()
        # `usar_ia=True`: cache primeiro e DeepSeek só para descrição ainda não analisada.
        # O resultado fica em `marcas_ia`, então rodar de novo não gasta tokens repetidos.
        resumo = recalcular_marcas(db, usar_ia=True)
        print(
            f"Itens na base: {resumo['itens']} | com marca: {resumo['com_marca']} "
            f"(antes: {antes}) | NCMs analisados: {resumo['ncms_com_marca']} | "
            f"atualizados: {resumo['atualizados']}"
        )
        print(
            f"Descrições únicas: {resumo['descricoes_unicas']} | consultas à IA: "
            f"{resumo['consultas_ia']} | economizadas pelo cache: {resumo['cache_hits']} | "
            f"sem marca: {resumo['sem_marca']}"
        )
        if resumo.get("erro_ia"):
            print(f"AVISO da IA (o restante usou cache/heurística): {resumo['erro_ia']}")

        # Amostra: as marcas mais frequentes e um exemplo de descrição de cada uma.
        marcas = (
            db.query(ItemNota.marca, ItemNota.descricao_produto)
            .filter(ItemNota.marca.isnot(None))
            .all()
        )
        contagem = Counter(marca for marca, _ in marcas)
        exemplo = {}
        for marca, descricao in marcas:
            exemplo.setdefault(marca, descricao)
        if contagem:
            print("\nMarcas mais frequentes:")
            for marca, quantidade in contagem.most_common(10):
                print(f"  {marca:<14} {quantidade:>5} itens | ex.: {exemplo[marca]}")
        else:
            print("\nNenhuma marca identificada (NCMs com uma única descrição não variam).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
