"""Regras de auditoria reaproveitadas pelos relatórios (lado servidor).

POR QUE ESTE MÓDULO EXISTE
--------------------------
A aba de Auditoria do front (`frontend/app.py`) decide o que é "Pendente de Aprovação"
comparando as descrições de cada grupo de produto (similaridade de texto + travas de
medida). Os relatórios, porém, são calculados no backend — então as MESMAS regras
precisam existir aqui para o servidor saber quais descrições ainda não foram validadas
e não deixá-las entrar nos cálculos.

As funções abaixo são um espelho de `frontend/app.py` (limpar_texto, tokens_*,
medidas_*, similaridade_*, grupo_e_excecao). Se algum limiar mudar lá, ele precisa ser
replicado aqui: o teste `tests/test_integridade_relatorios.py` compara as duas pontas
no banco real e acusa divergência.
"""

import difflib
import re
import unicodedata
from functools import lru_cache

# Limiares e tabelas — valores idênticos aos de frontend/app.py.
LIMIAR_SIMILARIDADE_EAN = 0.70
LIMIAR_SIMILARIDADE_SEM_EAN = 0.85
LIMIAR_TOKENS_MESMO_PRODUTO = 0.80
TAMANHO_MINIMO_TOKEN = 4
QUALIFICADORES_PRODUTO = frozenset({"COM", "SEM", "CON", "SIN"})
LIMIAR_TRAVA_CONFLITANTE = 0.50
TOLERANCIA_MEDIDA = 0.5
UNIDADES_AMBIGUAS = frozenset({"LT"})
PALAVRAS_EMBALAGEM = frozenset({"PCT", "CX", "FD", "EMB", "DZ"})
PREFIXO_GRUPO_NOVO = "NOVO_"
EANS_INVALIDOS = {"SEM_EAN", "SEM GTIN", "", "0", "NONE", "NAN", "NULL"}
UNIDADES_MEDIDA = {
    "MG": ("MASSA", 0.001),
    "G": ("MASSA", 1.0),
    "GR": ("MASSA", 1.0),
    "GRS": ("MASSA", 1.0),
    "KG": ("MASSA", 1000.0),
    "KGS": ("MASSA", 1000.0),
    "ML": ("VOLUME", 1.0),
    "CL": ("VOLUME", 10.0),
    "L": ("VOLUME", 1000.0),
    "LT": ("VOLUME", 1000.0),
    "LTS": ("VOLUME", 1000.0),
    "MM": ("COMPRIMENTO", 1.0),
    "CM": ("COMPRIMENTO", 10.0),
    "M": ("COMPRIMENTO", 1000.0),
    "MT": ("COMPRIMENTO", 1000.0),
    "MTS": ("COMPRIMENTO", 1000.0),
}


@lru_cache(maxsize=8192)
def limpar_texto(texto):
    """Normaliza a descrição (sem acento/pontuação, maiúscula) — igual a app.limpar_texto."""
    nfkd = unicodedata.normalize("NFKD", str(texto if texto is not None else ""))
    limpo = "".join(caractere for caractere in nfkd if not unicodedata.combining(caractere))
    limpo = re.sub(r"[^A-Z0-9\s]", "", limpo.upper())
    return " ".join(limpo.split())


def tem_ean_valido(ean) -> bool:
    """True quando o item tem código de barras real (não é vazio, '0' ou placeholder)."""
    if ean is None:
        return False
    return str(ean).strip().upper() not in EANS_INVALIDOS


def chave_descricao(chave_produto, descricao) -> str:
    """Chave de aprovação de UMA descrição na auditoria (formato gravado em ItemAuditado)."""
    return f"DESC_{chave_produto}_{limpar_texto(descricao)}"


@lru_cache(maxsize=32768)
def similaridade_textos(texto_a, texto_b) -> float:
    """Similaridade (0 a 1) por caracteres entre dois textos já normalizados."""
    return difflib.SequenceMatcher(None, texto_a, texto_b, autojunk=False).ratio()


@lru_cache(maxsize=8192)
def tokens_texto(descricao):
    """Tokens sem acento, com a pontuação como separador ("DET.LIQ." -> DET, LIQ)."""
    nfkd = unicodedata.normalize("NFKD", str(descricao if descricao is not None else ""))
    sem_acento = "".join(caractere for caractere in nfkd if not unicodedata.combining(caractere)).upper()
    return re.sub(r"[^A-Z0-9]+", " ", sem_acento).split()


@lru_cache(maxsize=8192)
def tokens_principais(descricao):
    """Palavras principais (mais de 3 caracteres) do produto, sem ordem."""
    return {token for token in tokens_texto(descricao) if len(token) >= TAMANHO_MINIMO_TOKEN}


@lru_cache(maxsize=8192)
def qualificadores_descricao(descricao):
    """Conectores que invertem o produto ("com osso" x "sem osso")."""
    return {token for token in tokens_texto(descricao) if token in QUALIFICADORES_PRODUTO}


@lru_cache(maxsize=32768)
def qualificadores_compativeis(descricao_a, descricao_b) -> bool:
    """COM/SEM não podem divergir quando os dois textos os citam."""
    qualificadores_a = qualificadores_descricao(descricao_a)
    qualificadores_b = qualificadores_descricao(descricao_b)
    return not (qualificadores_a and qualificadores_b and qualificadores_a != qualificadores_b)


@lru_cache(maxsize=32768)
def correspondencia_tokens(descricao_a, descricao_b) -> float:
    """Quanto das palavras principais do texto menor aparece no outro (0 a 1)."""
    tokens_a = tokens_principais(descricao_a)
    tokens_b = tokens_principais(descricao_b)
    if not tokens_a or not tokens_b:
        return 0.0
    em_comum = tokens_a & tokens_b
    if not em_comum:
        return 0.0
    menor = min(len(tokens_a), len(tokens_b))
    if menor >= 2 and len(em_comum) < 2:
        return 0.0
    return len(em_comum) / menor


@lru_cache(maxsize=8192)
def tokens_com_numeros(descricao):
    """Tokens preservando números ("1,7KG" -> "1,7", "KG") para ler medidas."""
    nfkd = unicodedata.normalize("NFKD", str(descricao if descricao is not None else ""))
    sem_acento = "".join(caractere for caractere in nfkd if not unicodedata.combining(caractere)).upper()
    return re.findall(r"[0-9]+(?:[.,][0-9]+)*|[A-Z]+", sem_acento)


@lru_cache(maxsize=8192)
def numero_do_token(token):
    """Converte o número do token no padrão brasileiro ("1,7" -> 1.7); None se inválido."""
    if "," in token and "." in token:
        token = token.replace(".", "").replace(",", ".")
    else:
        token = token.replace(",", ".")
    if token.count(".") > 1:
        return None
    try:
        return float(token)
    except ValueError:
        return None


@lru_cache(maxsize=8192)
def medidas_descricao(descricao):
    """Medidas físicas citadas na descrição, por família e convertidas para a base."""
    tokens = tokens_com_numeros(descricao)
    medidas = []
    for indice, token in enumerate(tokens):
        unidade = UNIDADES_MEDIDA.get(token)
        if not unidade or indice == 0:
            continue
        quantidade = numero_do_token(tokens[indice - 1])
        if quantidade is None:
            continue
        familia, fator = unidade
        multiplicador = None
        if indice >= 3 and tokens[indice - 2] == "X":
            multiplicador = numero_do_token(tokens[indice - 3])
        if (
            multiplicador is None
            and indice >= 2
            and token in UNIDADES_AMBIGUAS
            and tokens[indice - 2] in PALAVRAS_EMBALAGEM
        ):
            continue  # "PCT 12 LT" = 12 latas (contagem de embalagem, fora da trava)
        if multiplicador is not None:
            quantidade *= multiplicador
        medidas.append((familia, round(quantidade * fator, 3)))
    por_familia = {}
    for familia, valor in medidas:
        por_familia.setdefault(familia, []).append(valor)
    return {familia: tuple(sorted(valores)) for familia, valores in por_familia.items()}


def medidas_compativeis(descricao_a, descricao_b) -> bool:
    """True quando as medidas citadas nos dois textos não se contradizem."""
    medidas_a = medidas_descricao(descricao_a)
    medidas_b = medidas_descricao(descricao_b)
    if not medidas_a or not medidas_b:
        return True
    familias_comuns = set(medidas_a) & set(medidas_b)
    if not familias_comuns:
        return False  # famílias diferentes: peso x volume, peso x comprimento...
    for familia in familias_comuns:
        valores_a = medidas_a[familia]
        valores_b = medidas_b[familia]
        if len(valores_a) != len(valores_b):
            return False
        if any(abs(a - b) > TOLERANCIA_MEDIDA for a, b in zip(valores_a, valores_b)):
            return False
    return True


@lru_cache(maxsize=32768)
def travas_mesmo_produto(descricao_a, descricao_b) -> bool:
    """Travas de segurança do veredito "mesmo produto" (só restringem, nunca liberam)."""
    return qualificadores_compativeis(descricao_a, descricao_b) and medidas_compativeis(
        descricao_a, descricao_b
    )


@lru_cache(maxsize=32768)
def similaridade_descricoes(descricao_a, descricao_b) -> float:
    """Similaridade híbrida (0 a 1): caracteres (difflib) + palavras principais em comum.

    - Trava reprovada (COM x SEM ou medidas divergentes): limitada a LIMIAR_TRAVA_CONFLITANTE;
    - palavras principais >= LIMIAR_TOKENS_MESMO_PRODUTO: MESMO produto (1.0);
    - caso contrário: o melhor entre caracteres e correspondência de palavras.
    """
    por_caracteres = similaridade_textos(limpar_texto(descricao_a), limpar_texto(descricao_b))
    por_palavras = correspondencia_tokens(descricao_a, descricao_b)
    if not travas_mesmo_produto(descricao_a, descricao_b):
        return min(max(por_caracteres, por_palavras), LIMIAR_TRAVA_CONFLITANTE)
    if por_palavras >= LIMIAR_TOKENS_MESMO_PRODUTO:
        return 1.0
    return max(por_caracteres, por_palavras)


def similaridade_com_padrao(descricao_padrao, descricoes) -> float:
    """Menor similaridade entre a descrição padrão do grupo e todas as suas descrições."""
    return min(
        (similaridade_descricoes(descricao_padrao, descricao) for descricao in descricoes),
        default=1.0,
    )


def similaridade_entre_descricoes(descricoes) -> float:
    """Menor similaridade entre TODOS os pares de descrições do grupo (rigor sem EAN)."""
    menor = 1.0
    for indice, descricao_a in enumerate(descricoes):
        for descricao_b in descricoes[indice + 1 :]:
            valor = similaridade_descricoes(descricao_a, descricao_b)
            if valor < menor:
                menor = valor
    return menor


@lru_cache(maxsize=32768)
def _similaridade_abaixo_limiar(descricao_a, descricao_b, limiar) -> bool:
    """``similaridade_descricoes(a, b) < limiar`` sem calcular o valor exato.

    A Auditoria só precisa saber se um grupo é exceção (existe algum par abaixo do
    limiar), não a menor similaridade. Os atalhos abaixo evitam o ``difflib`` na
    maioria dos pares e devolvem exatamente o mesmo veredito de
    ``similaridade_descricoes``; é o que mantém a simulação de alertas rápida em
    grupos SEM_EAN grandes.
    """
    por_palavras = correspondencia_tokens(descricao_a, descricao_b)
    if not travas_mesmo_produto(descricao_a, descricao_b):
        # A similaridade é limitada a LIMIAR_TRAVA_CONFLITANTE (0.5) nesse caso.
        return LIMIAR_TRAVA_CONFLITANTE < limiar
    if por_palavras >= LIMIAR_TOKENS_MESMO_PRODUTO:
        # Mesmo produto: similaridade fixada em 1.0.
        return False
    if por_palavras >= limiar:
        return False
    por_caracteres = similaridade_textos(
        limpar_texto(descricao_a), limpar_texto(descricao_b)
    )
    return max(por_caracteres, por_palavras) < limiar


def grupo_e_excecao(chave, descricoes, tem_ean, descricao_padrao=None) -> bool:
    """True quando o grupo aparece na aba de Auditoria (espelho de app.grupo_e_excecao).

    - grupo criado à mão (NOVO_...): sempre aparece;
    - 1 só descrição: nada a auditar;
    - COM EAN: aparece se a similaridade com a descrição padrão (a mais antiga do grupo)
      ficar abaixo de LIMIAR_SIMILARIDADE_EAN;
    - SEM EAN: aparece se a menor similaridade ENTRE as descrições ficar abaixo de
      LIMIAR_SIMILARIDADE_SEM_EAN.
    """
    if str(chave or "").startswith(PREFIXO_GRUPO_NOVO):
        return True
    if len(descricoes) < 2:
        return False
    if tem_ean:
        padrao = descricao_padrao or descricoes[0]
        return any(
            _similaridade_abaixo_limiar(padrao, descricao, LIMIAR_SIMILARIDADE_EAN)
            for descricao in descricoes
        )
    # SEM EAN: interrompe no primeiro par divergente (antes comparava TODOS os pares
    # mesmo depois de já saber que o grupo era exceção).
    for indice, descricao_a in enumerate(descricoes):
        for descricao_b in descricoes[indice + 1 :]:
            if _similaridade_abaixo_limiar(
                descricao_a, descricao_b, LIMIAR_SIMILARIDADE_SEM_EAN
            ):
                return True
    return False


def descricoes_pendentes(chave, descricoes, tem_ean, aprovadas, descricao_padrao=None) -> list:
    """Descrições que a Auditoria mostra como "Pendente de Aprovação" neste grupo."""
    if not grupo_e_excecao(chave, descricoes, tem_ean, descricao_padrao):
        return []
    aprovadas = aprovadas or set()
    return [
        descricao
        for descricao in descricoes
        if chave_descricao(chave, descricao) not in aprovadas
    ]

