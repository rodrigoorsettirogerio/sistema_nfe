"""Extração da MARCA a partir da descrição do item da NFe (NLP leve, só biblioteca padrão).

A NF-e não tem campo de marca: `xProd` é texto livre do fornecedor. A marca é inferida
pela VARIAÇÃO das descrições do MESMO NCM — a intuição é simples: no grupo
"PALHA DE MANDIOCA FINA YOKI", "PALHA DE MANDIOCA FINA MATHEUS" e
"PALHA DE MANDIOCA FINA DECK" as palavras do produto/sabor (PALHA, MANDIOCA, FINA) se
repetem e só YOKI/MATHEUS/DECK mudam; essas são as marcas.

Passos de `extrair_marca`:

1. normaliza (maiúsculas, sem acento) e tokeniza a descrição;
2. descarta números, unidades/embalagem (KG, G, CX, UN, TOLETE, SHRINK...) e palavras de
   tipo/qualidade (CERVEJA, SUCO, DIET, CONGELADO...);
3. `termos_genericos` devolve os tokens que nomeiam o produto no NCM (>= 50% das
   descrições, ou >= 25% quando o NCM tem muitas variações): eles saem da disputa;
4. a marca é o PRIMEIRO token que sobra — em "CERVEJA NORTENA 4,8% GF 960ML" sobra
   NORTENA; em "GUARANA CHP ANTARCTICA DIET LATA 350ML" (com GUARANA/CHP/DIET genéricos
   no NCM) sobra ANTARCTICA.

`recalcular_marcas` só grava a marca quando a descrição tem uma **irmã** — outra
descrição do mesmo NCM que continua a mesma depois de tirar a marca
(`similaridade_produto` > 80%). Assim "PALHA ... YOKI" x "PALHA ... MATHEUS" vale, mas
"ALFACE CRESPA" x "ALFACE AMERICANA" (variedades diferentes) não vira marca.

Limites conhecidos (documentados de propósito): NCM com uma única descrição não tem
variação possível, então a marca fica NULL; a heurística não conhece um catálogo de
marcas, apenas o contraste entre descrições do mesmo NCM.

**A marca é usada em DUAS frentes**: (1) na chave inteligente dos relatórios
(Produto + Marca), para que Arrico e El Chef não compartilhem a mesma média de
preço; e (2) no tooltip de marcas substitutas do mesmo NCM. A marca NÃO entra na
média como valor: cada grupo continua com a média exata das suas próprias compras
do EAN/descrição.
"""

import math
import re
import unicodedata
from typing import Iterable, Optional

# ──────────────────────────────────────────────────────────────────────────────
# Vocabulário ignorado na hora de achar a marca
# ──────────────────────────────────────────────────────────────────────────────
# Unidades, embalagens e códigos de apresentação que aparecem em quase todo item.
UNIDADES = {
    "KG", "KGS", "G", "GR", "GRS", "GRAMA", "GRAMAS", "MG", "L", "LT", "LTS", "LITRO",
    "LITROS", "ML", "MLS", "CX", "CXA", "CXS", "CAIXA", "CAIXAS", "UN", "UND", "UNID",
    "UNIDADE", "UNIDADES", "PC", "PCS", "PCT", "PCTE", "PACOTE", "PACOTES", "FD", "FDX",
    "FARDO", "TOLETE", "TOL", "DZ", "DUZIA", "SC", "SACARIA", "EMB", "EMBALAGEM", "POTE",
    "LATA", "LATAS", "VIDRO", "PET", "GARRAFA", "GFA", "BANDEJA", "BND", "DISPLAY", "DP",
    "FRASCO", "REFIL", "GALAO", "BOMBONA", "SACHE", "SHRINK", "SHR", "FARDE", "FARDINHO",
    "RETRATIL", "NPAL", "PIL", "LONG", "NECK", "SIX", "PACK", "TTO", "BT", "BTL", "BDJ",
    "BAN", "BANDEIJAS", "GELO",
}
# Palavras de tipo de produto / qualidade / estado: nunca são a marca.
QUALIFICADORES = {
    "TIPO", "CLASSE", "CLASS", "SABOR", "TRADICIONAL", "ESPECIAL", "PREMIUM", "EXTRA",
    "SUPER", "REFORCADO", "REFORCADA", "REFORC", "RESFRIADO", "RESFRIADA", "RESFRI",
    "CONGELADO", "CONGELADA", "CONG", "GELADO", "GELADA", "FATIADO", "FATIADA",
    "PEDACO", "PEDACOS", "INTEIRO", "INTEIRA", "MOIDO", "MOIDA", "FRESCO", "FRESCA",
    "DIET", "ZERO", "LIGHT", "DESCAFEINADO", "INTEGRAL", "TOSTADO", "TORRADO", "FINO",
    "FINA", "GROSSO", "GROSSA", "MEDIO", "MEDIA", "PEQUENO", "PEQUENA", "GRANDE",
    "PORCAO", "KIT", "SORTIDO", "SORTIDA", "PROMOCAO", "OFERTA", "DESCONTO",
    "CHP", "NATURAL", "VERDE", "VERMELHO", "VERMELHA", "AMARELO", "AMARELA", "BRANCO",
    "BRANCA", "PRETO", "PRETA", "AZUL", "ROXO", "ROXA", "TINTO", "TINTA", "NEGRO",
    "REFINADO", "REFINADA", "PELE", "DET", "LIQ", "LIQUIDO", "LIQUIDA", "POSTA", "POSTAS",
    "SUPERIOR", "PRIME", "GOURMET", "RESERVA", "COLONIAL", "CASEIRO", "ARTESANAL", "OUT",
    "RETORNAVEL", "RETORNAVEIS", "DESC", "DES", "CHURRASCO", "GARRAFEIRA", "BOM", "REF",
    "DE", "DA", "DO", "DAS", "DOS", "E", "COM", "SEM", "PARA", "POR", "EM", "AO", "A",
    "O", "OS", "AS", "NO", "NA", "NUM", "UMA", "UM",
    # tipos de produto mais comuns (a marca vem depois deles)
    "ARROZ", "FEIJAO", "ACUCAR", "CAFE", "LEITE", "OLEO", "FARINHA", "MACARRAO",
    "BISCOITO", "PAO", "QUEIJO", "MANTEIGA", "CARNE", "FRANGO", "PEIXE", "BOVINO",
    "BOVINA", "SUINO", "SUINA", "VACUNO", "CERVEJA", "CERV", "CHOPP", "CHOPE", "VINHO",
    "ESPUMANTE", "SUCO", "REFRIGERANTE", "AGUA", "TONICA", "GUARANA", "ENERGETICO",
    "SACO", "BOBINA", "COPO", "GUARDANAPO", "PAPEL", "DETERGENTE", "SABAO", "ALCOOL",
    "MOLHO", "TEMPERO", "SAL", "OVO", "OVOS", "PRESUNTO", "MORTADELA", "SALAME",
    "LINGUICA", "BACON", "MUSSARELA", "MOZZARELLA", "PARMESAO", "PROVOLONE", "RICOTA",
    "CREME", "IOGURTE", "SORVETE", "CHOCOLATE", "DOCE", "BOLO", "TORTA", "HAMBURGUER",
    "PICANHA", "ALCATRA", "COSTELA", "MAMINHA", "PATINHO", "LOMBO", "FILE", "FRALDA",
    "MEDALHAO", "ENTRANHA", "DIAPHRAGMA", "ANCHO", "COLITA", "CUADRIL", "BEEF", "OJO",
    "SELECAO", "BLEND", "TIRA", "HUESO", "SIN", "BOV",
    # Estado/origem/corte e abreviações que apareciam como "marca" na inferência
    # local: STEAK/ORIGEM/CAMPO descrevem o produto, KAL/PLAST são abreviação de
    # embalagem e DIAFRAGMA/ENTRANHA são cortes (os dois últimos já estão acima).
    # COOPERBOI saiu daqui: é MARCA de carne (Cooperboi), não corte — como
    # qualificador, "CARNE MOIDA COOPERBOI BLEND" ficava sem marca nenhuma.
    "STEAK", "ORIGEM", "KAL", "CAMPO", "PLAST",
}

# Restos de entidade HTML/XML: quando o emissor escapa o texto, "&amp;" chega como
# "AMP" depois da normalização (o "&" vira separador). Nunca é marca.
RESTOS_DE_ENTIDADE = {"AMP", "LT", "GT", "QUOT", "APOS", "NBSP"}
# Siglas de marca realmente conhecidas na base. Toda outra sigla de 2-3 letras é
# abreviação de adjetivo/embalagem ("FGO", "PLAST") e não individualiza o item: a
# marca fica NULL em vez de virar lixo na chave do relatório.
SIGLAS_DE_MARCA = {"YPE", "FTA", "FSW"}

# NCM "vazio" (o importador usa 00000000 quando o XML não traz o campo).
NCM_VAZIO = "00000000"
# Fração das descrições do NCM em que um token precisa aparecer para ser considerado
# "nome do produto" (e não marca).
FRACAO_TERMOS_GENERICOS = 0.5
# Um token que aparece em MUITAS descrições do NCM também é nome de produto ("CRESPA",
# "OSSO", "PRETO"), mesmo ficando abaixo da metade: marca é o que individualiza o item.
FRACAO_TERMOS_FREQUENTES = 0.25
MINIMO_DESCRICOES_FREQUENTES = 3
# Duas descrições "irmãs" (mesmo NCM e > 80% parecidas SEM a marca) são a família de
# substituição do produto — é exatamente onde a marca é o que muda. Sem irmã, a marca não
# é inferível.
SIMILARIDADE_MINIMA_SUBSTITUTA = 0.80


def normalizar(texto) -> str:
    """Maiúsculas, sem acento e com espaços simples (base de tokens e similaridade)."""
    decomposto = unicodedata.normalize("NFKD", str(texto or ""))
    sem_acento = "".join(c for c in decomposto if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", sem_acento.upper()).strip()


# "POLENTA PALITO - 1", "- 2" etc. são números de controle do fornecedor. Eles
# não identificam o produto e distorcem a frequência dos tokens (a mesma
# descrição repetida 4x faria a marca parecer "termo genérico"). A marca é
# inferida sobre a descrição SEM esse sufixo.
_NUMERO_CONTROLE_FINAL_RE = re.compile(r"(?:\s*[-–—]\s*\d{1,3})+\s*$")


def remover_numero_controle(descricao) -> str:
    """Remove sufixos "- 1", "- 2"... do final da descrição (texto original intacto)."""
    original = str(descricao or "").strip()
    sem_controle = _NUMERO_CONTROLE_FINAL_RE.sub("", original).strip()
    return sem_controle or original


def tokens(descricao) -> list:
    """Palavras "de conteúdo" da descrição (sem números, unidades e qualificadores)."""
    saida = []
    for bruto in re.split(r"[^A-Z0-9]+", normalizar(descricao)):
        if not bruto or bruto.isdigit() or len(bruto) < 3:
            continue
        if any(caractere.isdigit() for caractere in bruto):
            # 1KG, 12X350, R13, H2OH: medidas e códigos de embalagem não são marca.
            continue
        if bruto in UNIDADES or bruto in QUALIFICADORES:
            continue
        saida.append(bruto)
    return saida


def termos_genericos(descricoes: Iterable[str]) -> set:
    """Tokens que nomeiam o produto (e não a marca) dentro de um NCM.

    Duas regras, ambas medidas sobre as descrições DISTINTAS do NCM:

    - **genéricos**: aparecem em >= 50% das descrições ("ALFACE", "HIDROPONICA",
      "MANDIOCA");
    - **frequentes**: aparecem em >= 25% (com pelo menos 3 descrições) — pega o nome do
      produto quando o NCM tem muitas variações ("CRESPA", "OSSO", "PRETO"). Marca é o
      que individualiza o item, então não pode ser uma palavra tão comum.

    Com menos de duas descrições não existe variação para comparar, então nada é genérico.
    """
    conjuntos = [
        set(tokens(descricao)) for descricao in descricoes or [] if str(descricao or "").strip()
    ]
    conjuntos = [conjunto for conjunto in conjuntos if conjunto]
    total = len(conjuntos)
    if total < 2:
        return set()
    contagem = {}
    for conjunto in conjuntos:
        for token in conjunto:
            contagem[token] = contagem.get(token, 0) + 1

    minimo_generico = max(2, math.ceil(total * FRACAO_TERMOS_GENERICOS))
    minimo_frequente = max(
        MINIMO_DESCRICOES_FREQUENTES, math.ceil(total * FRACAO_TERMOS_FREQUENTES)
    )
    return {
        token
        for token, quantidade in contagem.items()
        if quantidade >= minimo_generico or quantidade >= minimo_frequente
    }


def _token_sem_valor_de_marca(token: str) -> bool:
    """True para token que nunca individualiza a marca da descrição.

    Três grupos: resto de entidade HTML ("&amp;" → AMP), unidade/embalagem/corte/
    qualificador (já saem em `tokens`, mas a checagem é feita aqui também para o
    caso de o vocabulário crescer só em um dos lados) e sigla desconhecida de 2-3
    letras — sem catálogo não há como afirmar que é fabricante, e como marca ela só
    polui a chave do relatório.
    """
    if token in RESTOS_DE_ENTIDADE or token in UNIDADES or token in QUALIFICADORES:
        return True
    return len(token) <= 3 and token not in SIGLAS_DE_MARCA


def extrair_marca(descricao, genericos: Iterable[str] = ()) -> Optional[str]:
    """Marca inferida da descrição (None quando não sobra nenhum token próprio).

    O nome do produto pode estar antes ou depois da marca — por isso o critério é "o que
    VARIA entre as descrições do mesmo NCM": os termos genéricos do NCM saem da lista e a
    marca é o primeiro token que sobra. Restos de entidade ("AMP"), unidades/cortes e
    siglas desconhecidas de 2-3 letras também são descartados (`_token_sem_valor_de_marca`):
    é melhor devolver None do que gravar um pedaço de adjetivo como marca.
    """
    genericos = {str(token).upper() for token in genericos or ()}
    for token in tokens(descricao):
        if token not in genericos and not _token_sem_valor_de_marca(token):
            return token
    return None


def conteudo(descricao) -> str:
    """Descrição reduzida às palavras de conteúdo (sem peso, embalagem e qualificadores).

    É a "identidade" do produto: "PALHA DE MANDIOCA FINA YOKI 1KG" e
    "... MATHEUS 500G" viram "PALHA DE MANDIOCA FINA YOKI"/"... MATHEUS" — o peso e a
    embalagem não podem esconder que é o mesmo item de outra marca.
    """
    return " ".join(tokens(descricao))


def similaridade(descricao_a, descricao_b) -> float:
    """Similaridade textual 0..1 entre duas descrições (0 = nada a ver, 1 = igual).

    Usa o MAIOR valor entre duas comparações:
    - texto normalizado completo (bom para descrições curtas, ex.: "FEIJAO CARIOCA CAMIL"
      x "FEIJAO CARIOCA KICALDO");
    - texto só com as palavras de conteúdo (`conteudo`), que ignora peso/embalagem
      ("PALHA DE MANDIOCA FINA YOKI 1KG" x "... MATHEUS 500G" passa de 80%).

    É o critério de "descrição parecida" das marcas substitutas do relatório.
    """
    from difflib import SequenceMatcher

    texto_a, texto_b = normalizar(descricao_a), normalizar(descricao_b)
    if not texto_a or not texto_b:
        return 0.0
    if texto_a == texto_b:
        return 1.0
    bruto = SequenceMatcher(None, texto_a, texto_b).ratio()
    reduzido_a, reduzido_b = conteudo(descricao_a), conteudo(descricao_b)
    if not reduzido_a or not reduzido_b:
        return bruto
    return max(bruto, SequenceMatcher(None, reduzido_a, reduzido_b).ratio())


def ncm_normalizado(ncm) -> str:
    """NCM só com dígitos (a NFe pode trazer pontuação); 00000000 quando vazio."""
    digitos = re.sub(r"\D", "", str(ncm or ""))
    return digitos or NCM_VAZIO


def sem_marca(descricao, marca) -> str:
    """Descrição normalizada SEM o token da marca — o "nome do produto" que resta.

    É o texto usado para decidir se duas descrições são o mesmo item de marcas
    diferentes: sem tirar a marca, "YOKI" x "MATHEUS" derruba a similaridade de um item
    curto para menos de 80% e a substituição nunca apareceria.
    """
    texto = normalizar(descricao)
    token = normalizar(marca)
    if not token:
        return texto
    limpo = re.sub(rf"(?<![A-Z0-9]){re.escape(token)}(?![A-Z0-9])", " ", texto)
    return re.sub(r"\s+", " ", limpo).strip()


def similaridade_produto(descricao_a, marca_a, descricao_b, marca_b) -> float:
    """Similaridade do PRODUTO: as duas descrições são comparadas sem a marca.

    - "PALHA DE MANDIOCA FINA YOKI 1KG" x "... MATHEUS 500G" → as duas viram
      "PALHA DE MANDIOCA FINA" → 1.0 (mesmo item, outra marca);
    - "ALFACE AMERICANA" x "ALFACE CRESPA HIDROPONICA" → "ALFACE" x "ALFACE HIDROPONICA"
      → abaixo de 80% (são produtos diferentes, não marcas do mesmo item).
    """
    texto_a = sem_marca(descricao_a, marca_a)
    texto_b = sem_marca(descricao_b, marca_b)
    if not texto_a or not texto_b:
        # Descrição reduzida a nada (só a marca) não dá evidência de que é o mesmo item:
        # "BETERRABA" x "CHUCHU" em hortifrúti não é substituição.
        return 0.0
    return similaridade(texto_a, texto_b)


def descricoes_com_par(descricoes: Iterable[str], marcas=None) -> set:
    """Descrições que têm pelo menos uma "irmã" (mesmo item de outra marca) no NCM.

    Sem irmã não existe família de substituição — "ALFACE AMERICANA" e
    "ALFACE CRESPA HIDROPONICA" são produtos diferentes (as palavras que sobram depois da
    marca não coincidem), não marcas do mesmo item. Nesse caso a marca fica NULL (e o
    relatório também não mostraria substituta).
    """
    marcas = marcas or {}
    textos = [str(descricao).strip() for descricao in descricoes or [] if str(descricao or "").strip()]
    resultado = set()
    for indice, texto in enumerate(textos):
        for outro_indice, outro in enumerate(textos):
            if indice == outro_indice:
                continue
            similar = similaridade_produto(texto, marcas.get(texto), outro, marcas.get(outro))
            if similar > SIMILARIDADE_MINIMA_SUBSTITUTA:
                resultado.add(texto)
                break
    return resultado


def recalcular_marcas(
    db,
    usar_ia: bool = False,
    apenas_cache: bool = False,
    progresso=None,
) -> dict:
    """(Re)calcula `itens_nota.marca` para TODA a base.

    A heurística local por NCM continua sendo a base (e o fallback). Com `usar_ia=True`,
    cada descrição ÚNICA passa antes pela memória `marcas_ia` (`obter_marca_inteligente`):
    uma descrição já analisada não gasta tokens e só as inéditas chamam a DeepSeek.
    `apenas_cache=True` aplica somente o que já está memorizado — nenhuma chamada de IA
    (usado pelo botão depois que o front processou os lotes e desenhou o progresso).

    Os números de controle ("- 1", "- 2") saem antes do corpus: sem isso a mesma
    descrição repetida viraria "termo genérico" e esconderia a marca. `progresso`, quando
    informado, é chamado como ``progresso(processadas, total, estatisticas)``. Devolve um
    resumo para log/tela, incluindo quantas consultas à IA e quantos cache hits houve.
    """
    from backend.models import ItemNota  # import tardio: evita ciclo models <-> marcas

    linhas = db.query(ItemNota.id, ItemNota.ncm, ItemNota.descricao_produto).all()
    descricao_por_id = {}
    descricoes_por_ncm = {}
    for linha in linhas:
        descricao = remover_numero_controle(linha.descricao_produto)
        if not descricao:
            continue
        descricao_por_id[linha.id] = descricao
        descricoes_por_ncm.setdefault(ncm_normalizado(linha.ncm), set()).add(descricao)

    genericos = {
        ncm: termos_genericos(descricoes)
        for ncm, descricoes in descricoes_por_ncm.items()
        if len(descricoes) >= 2
    }
    # 1ª passada: marca candidata de cada descrição — o que VARIA dentro do NCM.
    marca_por_descricao = {}
    for ncm in genericos:
        for descricao in descricoes_por_ncm[ncm]:
            marca_por_descricao[(ncm, descricao)] = extrair_marca(descricao, genericos[ncm])

    # 2ª passada: a marca só vale se a descrição tiver uma "irmã" (mesmo produto, outra
    # marca). Sem irmã, a palavra que varia é o próprio produto ("ALFACE CRESPA" x
    # "ALFACE AMERICANA") e a marca fica NULL — o relatório também não mostraria substituta.
    com_par = {
        ncm: descricoes_com_par(
            descricoes,
            {descricao: marca_por_descricao.get((ncm, descricao)) for descricao in descricoes},
        )
        for ncm, descricoes in descricoes_por_ncm.items()
        if ncm in genericos
    }

    # 3ª passada (opcional): memória da IA. A chave é a descrição normalizada, então a
    # MESMA descrição vinda de NCMs/notas diferentes reaproveita a resposta. Só entra no
    # mapa o que foi respondido pela memória/IA; um cache miss (None) mantém a heurística.
    descricoes_unicas = sorted(set(descricao_por_id.values()))
    marcas_memoria = {}
    estatisticas_ia = {
        "cache_hits": 0,
        "cache_misses": 0,
        "consultas_ia": 0,
        "sem_marca": 0,
        "marcas_encontradas": 0,
    }
    erro_ia = None
    if usar_ia or apenas_cache:
        from backend import ia_consenso  # import tardio: evita ciclo ia_consenso <-> marcas

        permitir_ia = bool(usar_ia)
        total = len(descricoes_unicas)
        for indice, descricao in enumerate(descricoes_unicas, start=1):
            try:
                resultado = ia_consenso.obter_marca_inteligente(
                    descricao,
                    db=db,
                    usar_ia=permitir_ia,
                    estatisticas=estatisticas_ia,
                )
            except (ia_consenso.IAnaoconfigurada, ia_consenso.ErroIA) as erro:
                # A exceção só acontece depois de um cache miss (a consulta ao banco já
                # rodou), então não há o que recuperar: a heurística local assume daqui
                # em diante e as próximas descrições seguem apenas pela memória.
                erro_ia = str(erro)
                permitir_ia = False
                resultado = None
            if resultado is not None:
                marcas_memoria[descricao] = (
                    None if ia_consenso.eh_sem_marca(resultado) else resultado
                )
            if callable(progresso):
                progresso(indice, total, dict(estatisticas_ia))

    marcas = {}
    for linha in linhas:
        descricao = descricao_por_id.get(linha.id, "")
        ncm = ncm_normalizado(linha.ncm)
        if descricao in marcas_memoria:
            marcas[linha.id] = marcas_memoria[descricao]
            continue
        if not descricao or descricao not in com_par.get(ncm, set()):
            marcas[linha.id] = None
            continue
        marcas[linha.id] = marca_por_descricao.get((ncm, descricao))

    alterados = 0
    for item in db.query(ItemNota).yield_per(500):
        nova = marcas.get(item.id)
        if item.marca != nova:
            item.marca = nova
            alterados += 1
    db.commit()
    return {
        "itens": len(linhas),
        "atualizados": alterados,
        "com_marca": sum(1 for marca in marcas.values() if marca),
        "ncms_com_marca": len(genericos),
        "descricoes_unicas": len(descricoes_unicas),
        "consultas_ia": estatisticas_ia["consultas_ia"],
        "cache_hits": estatisticas_ia["cache_hits"],
        "economias_cache": estatisticas_ia["cache_hits"],
        "sem_marca": estatisticas_ia["sem_marca"],
        "erro_ia": erro_ia,
    }
