"""Popula a tabela `marcas_master` com o catálogo mestre de marcas do varejo.

A tabela é a primeira fonte da cascata de `ia_consenso.obter_marca_inteligente`:
antes de chamar a DeepSeek, a função procura as marcas conhecidas por token na
descrição do item. O script é idempotente — pode ser executado várias vezes; nomes
já existentes apenas têm a categoria sincronizada.

Uso (na raiz do projeto):
    python popular_marcas_master.py
    python popular_marcas_master.py --dry-run   # mostra o resumo sem gravar

A lista abaixo é a fornecida pela operação. Ela tem 204 nomes únicos (o pedido
mencionava 205; a diferença é apenas a contagem do texto original).
"""

import argparse

from backend.database import SessionLocal, init_db
from backend.models import MarcaMaster


MARCAS_POR_CATEGORIA = {
    "Alimentos": [
        "Sadia",
        "Seara",
        "Perdigão",
        "Aurora",
        "Frimesa",
        "Friboi",
        "Swift",
        "Minerva",
        "Marfrig",
        "Bassi",
        "Montana",
        "Coqueiro",
        "Gomes da Costa",
        "Itambé",
        "Italac",
        "Piracanjuba",
        "Parmalat",
        "Batavo",
        "Danone",
        "Nestlé",
        "Garoto",
        "Lacta",
        "Camanducaia",
        "Tirolez",
        "Scala",
        "Bom Destino",
        "President",
        "Qualy",
        "Doriana",
        "Delícia",
        "Claybom",
        "Becel",
        "Camil",
        "Kicaldo",
        "Tio João",
        "Urbano",
        "Namorado",
        "Yoki",
        "Quaker",
        "Nissin",
        "Maggi",
        "Arisco",
        "Hellmann's",
        "Heinz",
        "Cica",
        "Etti",
        "Tarantella",
        "Pomarola",
        "Predilecta",
        "Quero",
        "Kitano",
        "Kenko",
        "Hemmer",
        "Beira Alta",
        "Gallo",
        "Torre Belém",
        "Vila Piva",
        "Cepera",
        "Liza",
        "Soya",
        "Vitaliv",
        "Sinha",
        "Coamo",
        "Farm Frites",
        "McCain",
        "Bem Brasil",
        "Finest",
        "Croc Crispy",
        "Madalena",
        "Arrico",
        "El Chef",
        "Matheus",
        "Naturovos",
        "La Sereníssima",
        "Ovomaltine",
        "Royal",
        "Dr. Oetker",
    ],
    "Bebidas": [
        "Skol",
        "Brahma",
        "Antarctica",
        "Heineken",
        "Budweiser",
        "Amstel",
        "Stella Artois",
        "Itaipava",
        "Original",
        "Corona",
        "Spaten",
        "Eisenbahn",
        "Colorado",
        "Lagunitas",
        "Patricia",
        "Nortena",
        "Crystal",
        "Petra",
        "Coca-Cola",
        "Fanta",
        "Sprite",
        "Kuat",
        "Itubaína",
        "Schweppes",
        "Sukita",
        "Dolly",
        "Pepsi",
        "Guaraná Antarctica",
        "H2OH!",
        "Powerade",
        "Gatorade",
        "Red Bull",
        "Monster",
        "Del Valle",
        "Maguary",
        "Ades",
        "Sufresh",
        "Prat's",
        "Lindoya",
        "Bonafont",
        "Absolut",
        "Smirnoff",
        "Gin Gordon's",
        "Gin Tanqueray",
        "Gin Seagers",
        "Cachaça 51",
        "Cachaça Velho Barreiro",
        "Cachaça Sagatiba",
        "Cachaça Seleta",
        "Whisky Johnnie Walker",
        "Whisky Jack Daniel's",
        "Tequila José Cuervo",
        "Licor 43",
        "Licor Baileys",
        "Licor Cointreau",
        "Vermouth Cinzano",
        "Aperitivo Campari",
        "Saquê Azuma",
        "Xarope Monin",
        "Xarope Milani",
    ],
    "Embalagens": [
        "Copobras",
        "Coposul",
        "Copocentro",
        "Minaplast",
        "Pratyc",
        "Wyda",
        "Ricapel",
        "Neobag",
        "Sirius Paper",
        "FNS Copos e Embalagens",
        "Profesta",
        "Plazapel",
        "Strawplast",
        "Totalplast",
        "Trevo",
        "Snob",
        "Kitchen",
        "IBRAS",
        "FONPLAST",
        "KEROCOPO",
        "COPOPLAST",
        "Verocopo",
        "COPOMAS",
        "ROSSO",
    ],
    "Limpeza": [
        "Ypê",
        "OMO",
        "Comfort",
        "Brilhante",
        "Cif",
        "Limpol",
        "Minuano",
        "Agifacil",
        "Barbarex",
        "Alto Limp",
        "Clean Up",
        "Aylag",
        "Warkike",
        "Proquimia",
        "X Professional",
        "Kimberly-Clark Professional",
        "ASSIM (Flora)",
        "Sentax",
        "Romax",
        "Alumil",
        "Pedrex",
        "Start",
        "Scotch-Brite",
        "Bombril",
        "Assolan",
        "Qlustro",
        "Medix",
        "Vabene",
        "Descarpack",
        "Talge",
        "Sanro",
        "Danny",
        "Inoven",
        "Elite",
        "Personal",
        "Topper",
        "Impapper",
        "Colibri",
        "Santepel",
        "Poema",
        "Paper Mais",
        "Bralimpia",
        "Bettanin",
    ],
}


def _lista_consolidada():
    """(nome, categoria) de todas as marcas, sem repetir nome (caixa ignorada)."""
    vistos = set()
    itens = []
    for categoria, nomes in MARCAS_POR_CATEGORIA.items():
        for nome in nomes:
            chave = str(nome or "").strip().casefold()
            if not chave or chave in vistos:
                continue
            vistos.add(chave)
            itens.append((str(nome).strip(), categoria))
    return itens


def sincronizar_marcas_master(db=None, criar_tabelas=True):
    """Insere/atualiza o catálogo mestre. Devolve o resumo da operação.

    `db` é opcional para testes (sessão SQLAlchemy já aberta). Por padrão o script
    garante as tabelas com `init_db()` e abre a sessão padrão do projeto.
    """
    if criar_tabelas:
        init_db()

    propria_sessao = db is None
    sessao = db or SessionLocal()
    try:
        existentes = {
            marca.nome_marca: marca for marca in sessao.query(MarcaMaster).all()
        }
        inseridas = atualizadas = 0
        for nome, categoria in _lista_consolidada():
            registro = existentes.get(nome)
            if registro is None:
                sessao.add(MarcaMaster(nome_marca=nome, categoria=categoria))
                inseridas += 1
            elif registro.categoria != categoria:
                registro.categoria = categoria
                atualizadas += 1
        sessao.commit()
        return {
            "inseridas": inseridas,
            "atualizadas": atualizadas,
            "total_lista": len(_lista_consolidada()),
            "total_banco": sessao.query(MarcaMaster).count(),
        }
    finally:
        if propria_sessao:
            sessao.close()


def main():
    parser = argparse.ArgumentParser(description="Popula a tabela marcas_master.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Mostra o resumo da lista sem gravar no banco.",
    )
    args = parser.parse_args()

    if args.dry_run:
        itens = _lista_consolidada()
        por_categoria = {}
        for _, categoria in itens:
            por_categoria[categoria] = por_categoria.get(categoria, 0) + 1
        print(f"Catálogo mestre: {len(itens)} marcas únicas.")
        for categoria, quantidade in por_categoria.items():
            print(f"  - {categoria}: {quantidade}")
        return 0

    resumo = sincronizar_marcas_master()
    print("✅ Catálogo mestre de marcas sincronizado.")
    print(f"   Inseridas: {resumo['inseridas']}")
    print(f"   Atualizadas: {resumo['atualizadas']}")
    print(f"   Total na lista: {resumo['total_lista']}")
    print(f"   Total no banco: {resumo['total_banco']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
