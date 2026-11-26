"""Memória de aprendizado (de-para) e tratamento de embalagens dos XMLs de NFe.

Duas responsabilidades que andam juntas:

1. Embalagens: o XML traz a unidade comercial (`uCom`) e a descrição do produto. A
   embalagem faz parte da IDENTIDADE do produto: "ORIGINAL 600ML CX C/24" e
   "ORIGINAL 600ML UN" são itens diferentes e têm médias/variações calculadas
   separadamente. `normalizar_embalagem` produz um token canônico (CX24, UN, FD10X1KG)
   só para compor a chave do produto; `normalizar_quantidades` NÃO converte mais
   quantidade/preço (os valores crus do XML são preservados).

2. Memória de aprendizado: o fornecedor repete o mesmo `cProd` em todas as notas. A
   tabela `mapeamento_fornecedor_produtos` guarda o EAN/descrição/NCM/embalagem
   canônicos de cada (CNPJ do fornecedor + cProd). Quando o XML seguinte vier com
   "SEM GTIN" ou descrição abreviada, o importador reaproveita o que já aprendeu.

O módulo é puro Python/SQLAlchemy (sem FastAPI) — é usado pelo importador, pelo
backfill (`backfill_aprendizado.py`), pelos relatórios e pelas rotas da Auditoria.
"""

import html
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ──────────────────────────────────────────────────────────────────────────────
# Unidades de medida e embalagens
# ──────────────────────────────────────────────────────────────────────────────
# (unidade_base, fator para a unidade base): 1 G = 0,001 KG; 1 ML = 0,001 L...
MEDIDAS = {
    "KG": ("KG", 1.0),
    "KGS": ("KG", 1.0),
    "QUILO": ("KG", 1.0),
    "QUILOS": ("KG", 1.0),
    "G": ("KG", 0.001),
    "GR": ("KG", 0.001),
    "GRS": ("KG", 0.001),
    "GRAMA": ("KG", 0.001),
    "GRAMAS": ("KG", 0.001),
    "MG": ("KG", 0.000001),
    "L": ("L", 1.0),
    "LT": ("L", 1.0),
    "LTS": ("L", 1.0),
    "LITRO": ("L", 1.0),
    "LITROS": ("L", 1.0),
    "ML": ("L", 0.001),
    "MLS": ("L", 0.001),
    "CL": ("L", 0.01),
    "M": ("M", 1.0),
    "MT": ("M", 1.0),
    "MTS": ("M", 1.0),
    "METRO": ("M", 1.0),
    "METROS": ("M", 1.0),
    "CM": ("M", 0.01),
    "MM": ("M", 0.001),
    "M2": ("M2", 1.0),
    "M3": ("M3", 1.0),
}

# Códigos de uCom que são EMBALAGEM (a descrição diz quantas unidades/conteúdo tem).
SIMBOLOS_EMBALAGEM = {
    "CAIXA": "CAIXA",
    "CXA": "CAIXA",
    "CXS": "CAIXA",
    "CX": "CAIXA",
    "FARDO": "FARDO",
    "FRD": "FARDO",
    "FDO": "FARDO",
    "FD": "FARDO",
    "PACOTE": "PACOTE",
    "PCTE": "PACOTE",
    "PCT": "PACOTE",
    "PACK": "PACOTE",
    "PAC": "PACOTE",
    "BANDEJA": "BANDEJA",
    "BG": "BANDEJA",
    "BD": "BANDEJA",
    "SACARIA": "SACO",
    "SACO": "SACO",
    "SC": "SACO",
    "POTE": "POTE",
    "TP": "POTE",
    "PT": "POTE",
    "GALAO": "GALAO",
    "GL": "GALAO",
    "GARRAFA": "GARRAFA",
    "GRF": "GARRAFA",
    "GF": "GARRAFA",
    "DUZIA": "DUZIA",
    "DZ": "DUZIA",
    "DISPLAY": "DISPLAY",
    "ENGRADADO": "ENGRADADO",
    "ROLO": "ROLO",
    "RO": "ROLO",
    "BOLSA": "BOLSA",
    "BO": "BOLSA",
    "FRASCO": "FRASCO",
    "FR": "FRASCO",
    "MACO": "MACO",
    "MC": "MACO",
}
# Palavras de embalagem aceitas na DESCRIÇÃO (sem acento, já normalizadas).
_PALAVRAS_EMBALAGEM = (
    r"CAIXA|CXA|CXS|CX|FARDO|FRD|FDO|FD|PACOTE|PCTE|PCT|PACK|PAC|BANDEJA|BG|BD|"
    r"SACARIA|SACO|SC|POTE|TP|PT|GALAO|GL|GARRAFA|GRF|GF|DUZIA|DZ|DISPLAY|"
    r"ENGRADADO|ROLO|BOLSA|FRASCO|MACO"
)

# Códigos aceitos com sufixo numérico na uCom: CX24, CX027, FD12, UN10...
_CODIGOS_SUFIXO = (
    "ENGRADADO", "DISPLAY", "GARRAFA", "FRASCO", "SACARIA", "BANDEJA", "PACOTE",
    "PCTE", "CAIXA", "FARDO", "POTE", "CXA", "CXS", "PCT", "PACK", "PAC", "SACO",
    "GALAO", "DUZIA", "ROLO", "BOLSA", "GRF", "MACO", "CX", "FRD", "FDO", "GF",
    "GL", "SC", "BD", "BG", "DZ", "TP", "PT", "RO", "BO", "FR", "MC", "FD",
    "UNID", "UND", "UN",
)
_RE_SUFIXO_EMBALAGEM = re.compile(
    r"^(" + "|".join(sorted({re.escape(c) for c in _CODIGOS_SUFIXO}, key=len, reverse=True))
    + r")(\d{1,4})$"
)

# Separador tolerante entre a palavra de embalagem e o número ("CX. 24", "CX/24").
_SEP = r"[\s./-]*"

# "12X300ML", "10 X 1KG", "20X900 ML" → 12 unidades de 300 ML...
_RE_CONTAGEM_X_CONTEUDO = re.compile(
    r"(?<![A-Z0-9])(\d{1,4})\s*X\s*(\d+(?:[.,]\d+)?)\s*"
    r"(KG|KGS|G|GR|GRS|MG|L|LT|LTS|ML|CL)\b"
)

# "CAIXA COM 24 UN", "CX 24", "FD 12", "PCT 10" (número que NÃO é medida).
# `(?!,\d)` evita confundir "CX 15,000KG" (conteúdo) com uma contagem de 15 unidades.
_RE_EMBALAGEM_CONTAGEM = re.compile(
    r"\b(?:" + _PALAVRAS_EMBALAGEM + r")\b"
    r"(?:" + _SEP + r"(?:COM|C|CONTENDO|DE))?" + _SEP + r"(\d{1,4})(?!\d)"
    r"(?!\s*,\s*\d)"
    r"(?!" + _SEP + r"(?:KG|KGS|G|GR|GRS|MG|L|LT|LTS|ML|CL)\b)"
    r"(?!" + _SEP + r"X" + _SEP + r"\d)"
)

# "24 UN" / "24UN" na descrição — só vale quando a uCom já é uma embalagem (CX/FD...).
_RE_CONTAGEM_UNIDADES = re.compile(
    r"(?<![A-Z0-9])(\d{1,4})(?!\d)\s*(?:UN|UND|UNID|UNIDADES)\b"
)

# "CX 15KG", "CAIXA 10,5 KG", "FD 20 L" → conteúdo total da embalagem.
_RE_EMBALAGEM_CONTEUDO = re.compile(
    r"\b(?:" + _PALAVRAS_EMBALAGEM + r")\b"
    r"(?:" + _SEP + r"(?:COM|C|CONTENDO|DE))?" + _SEP + r"(\d+(?:[.,]\d+)?)\s*"
    r"(KG|KGS|G|GR|GRS|MG|L|LT|LTS|ML|CL)\b"
)


# Entidades HTML/XML que o Omie e alguns emissores deixam escapadas no texto do
# produto ("&amp; SKIRT STEAK"). A guarda só dispara em entidade CONHECIDA: um "&"
# comum da descrição nunca é tocado e o laço sempre termina.
_RE_ENTIDADE_TEXTO = re.compile(
    r"&(amp|lt|gt|quot|apos|#\d+|#x[0-9a-f]+);", re.IGNORECASE
)
# Teto do laço: "&amp;amp;amp;..." (escapado várias vezes) estabiliza bem antes disso.
LIMITE_DECODIFICACAO_ENTIDADES = 5


def limpar_entidades_texto(texto) -> str:
    """Remove entidades HTML/XML do texto ("&amp; Skirt Steak" -> "& Skirt Steak").

    O texto pode chegar escapado UMA ou VÁRIAS vezes ("&amp;", "&amp;amp;"). A
    decodificação roda em laço até estabilizar, com teto de segurança, e só age
    quando a guarda `_RE_ENTIDADE_TEXTO` encontra uma entidade conhecida — assim
    um "&" solto na descrição continua "&".

    É aplicada na ENTRADA (antes de normalizar/gerar a chave): entidade
    decodificada depois já teria virado parte do nome do produto, e o resíduo
    ("AMP") podia até ser lido como marca.
    """
    if texto is None:
        return ""
    atual = str(texto)
    for _ in range(LIMITE_DECODIFICACAO_ENTIDADES):
        if not _RE_ENTIDADE_TEXTO.search(atual):
            break
        decodificado = html.unescape(atual)
        if decodificado == atual:
            break
        atual = decodificado
    return atual


def normalizar_texto(texto) -> str:
    """Maiúsculas, sem acento e com espaços simples (mesmo padrão dos outros módulos)."""
    texto = unicodedata.normalize("NFKD", str(texto or ""))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = texto.upper()
    texto = re.sub(r"[^A-Z0-9,./]+", " ", texto)
    return re.sub(r"\s+", " ", texto).strip()


def _decimal(valor) -> float:
    """Converte "1.234,56" / "0,5" / "15.000" em float."""
    texto = str(valor or "").strip()
    if not texto:
        return 0.0
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    try:
        return float(texto)
    except ValueError:
        return 0.0


def _medida(codigo: str):
    """(unidade_base, fator) quando `codigo` é uma unidade de medida; senão None."""
    return MEDIDAS.get(re.sub(r"[^A-Z0-9]", "", str(codigo or "").upper()))


def _prefixo_embalagem(codigo: str) -> str:
    """Maior código de embalagem que inicia a uCom (CXA1 → CXA, FD → FD)."""
    codigo = re.sub(r"[^A-Z0-9]", "", str(codigo or "").upper())
    for prefixo in sorted(SIMBOLOS_EMBALAGEM, key=len, reverse=True):
        if codigo.startswith(prefixo):
            return prefixo
    return ""


# Token curto e estável usado na CHAVE do produto. A embalagem é identidade, não
# unidade de conversão: CX24 e UN nunca podem cair no mesmo grupo, mesmo com o
# mesmo EAN e a mesma descrição base.
TIPOS_EMBALAGEM_CANONICOS = {
    "CAIXA": "CX", "CXA": "CX", "CXS": "CX", "CX": "CX",
    "FARDO": "FD", "FRD": "FD", "FDO": "FD", "FD": "FD",
    "PACOTE": "PCT", "PCTE": "PCT", "PCT": "PCT", "PACK": "PCT", "PAC": "PCT",
    "BANDEJA": "BG", "BG": "BG", "BD": "BD",
    "SACARIA": "SC", "SACO": "SC", "SC": "SC",
    "POTE": "PT", "TP": "PT", "PT": "PT",
    "GALAO": "GL", "GL": "GL",
    "GARRAFA": "GF", "GRF": "GF", "GF": "GF",
    "DUZIA": "DZ", "DZ": "DZ",
    "DISPLAY": "DISPLAY", "ENGRADADO": "ENGRADADO",
    "ROLO": "RO", "RO": "RO", "BOLSA": "BO", "BO": "BO",
    "FRASCO": "FR", "FR": "FR", "MACO": "MC", "MC": "MC",
}


def _tipo_embalagem_canonico(codigo) -> str:
    """Tipo canônico da uCom (CXA1 → CX, FARDO → FD) ou ``""``."""
    prefixo = _prefixo_embalagem(codigo)
    return TIPOS_EMBALAGEM_CANONICOS.get(prefixo, "")


def _tipo_embalagem_na_descricao(texto) -> str:
    """Primeiro tipo de embalagem citado na descrição já normalizada."""
    achado = re.search(r"\b(" + _PALAVRAS_EMBALAGEM + r")\b", texto or "")
    if not achado:
        return ""
    return TIPOS_EMBALAGEM_CANONICOS.get(achado.group(1), "")


def _numero_token(valor) -> str:
    """Número curto para o token (24.0 → 24; 1.5 → 1.5)."""
    numero = float(valor or 0.0)
    texto = f"{numero:.3f}".rstrip("0").rstrip(".")
    return texto or "0"

def normalizar_embalagem(unidade_compra="", descricao="") -> str:
    """Token canônico da embalagem para compor a chave do produto.

    A embalagem é apenas IDENTIDADE: este token não converte preço nem quantidade.

    Exemplos:
        ("CX", "CAIXA C/ 24")          -> "CX24"
        ("CX", "CX 24")                -> "CX24"
        ("",   "1X24")                 -> "CX24"
        ("UN", "UNIDADE")              -> "UN"
        ("",   "1X1")                  -> "UN"
        ("CX", "CX 15KG")              -> "CX15KG"
        ("FD", "10X1KG")               -> "FD10X1KG"

    O primeiro argumento também pode ser a própria descrição
    (``normalizar_embalagem("CAIXA C/ 24")``), útil em validações e scripts.
    """
    unidade = str(unidade_compra or "").strip()
    descricao = str(descricao or "").strip()
    texto_unidade = normalizar_texto(unidade)

    # Chamada com um único argumento que já é a descrição da embalagem.
    if not descricao and (
        _tipo_embalagem_na_descricao(texto_unidade)
        or "UNIDADE" in texto_unidade
        or re.search(r"(?<![A-Z0-9])1\s*X\s*\d", texto_unidade)
    ):
        descricao, unidade = unidade, ""

    codigo_ucom = re.sub(r"[^A-Z0-9]", "", unidade.upper())
    texto = normalizar_texto(descricao)
    tipo = _tipo_embalagem_canonico(codigo_ucom) or _tipo_embalagem_na_descricao(texto)
    quantidade = None

    # uCom com sufixo numérico explícito: CX24, CX027, FD12...
    sufixo = _RE_SUFIXO_EMBALAGEM.match(codigo_ucom)
    if sufixo:
        tipo = TIPOS_EMBALAGEM_CANONICOS.get(sufixo.group(1), "") or tipo
        numero = int(sufixo.group(2))
        if 1 < numero <= 1000:
            quantidade = numero

    # "12X300ML", "10 X 1KG": pacote com N unidades de conteúdo.
    achado = _RE_CONTAGEM_X_CONTEUDO.search(texto)
    if achado and quantidade is None:
        numero = int(achado.group(1))
        valor_conteudo = _decimal(achado.group(2))
        medida_conteudo = _medida(achado.group(3))
        if 1 <= numero <= 1000 and valor_conteudo > 0 and medida_conteudo:
            unidade_conteudo = {
                "ML": "ML", "MLS": "ML", "L": "L", "LT": "L", "LTS": "L",
                "CL": "CL", "G": "G", "GR": "G", "GRS": "G", "KG": "KG",
                "KGS": "KG", "MG": "MG",
            }.get(achado.group(3).upper(), medida_conteudo[0])
            return (
                f"{tipo or 'CX'}{numero}X{_numero_token(valor_conteudo)}"
                f"{unidade_conteudo}"
            )

    # Contagem explícita ao lado da palavra de embalagem: "CAIXA C/ 24", "CX 24".
    if quantidade is None:
        achado = _RE_EMBALAGEM_CONTAGEM.search(texto)
        if achado:
            numero = int(achado.group(1))
            if 1 < numero <= 1000:
                quantidade = numero
                tipo = tipo or "CX"

    # uCom já é embalagem e a descrição diz apenas quantas unidades: "24 UN".
    if quantidade is None and tipo:
        achado = _RE_CONTAGEM_UNIDADES.search(texto)
        if achado:
            numero = int(achado.group(1))
            if 1 < numero <= 1000:
                quantidade = numero

    # "1X24" (embalagem de 24) e "1X1" (unidade avulsa). Só o primeiro fator 1 é
    # interpretado como contagem: "30X40CM" (medida) não vira embalagem.
    if quantidade is None:
        achado = re.search(r"(?<![A-Z0-9])1\s*X\s*(\d{1,4})(?!\d)", texto)
        if achado:
            numero = int(achado.group(1))
            if numero == 1:
                return "UN"
            if numero <= 1000:
                quantidade = numero
                tipo = tipo or "CX"

    # Embalagem de conteúdo: "CX 15KG", "FD 20 L".
    if quantidade is None:
        achado = _RE_EMBALAGEM_CONTEUDO.search(texto)
        if achado:
            valor_conteudo = _decimal(achado.group(1))
            medida_conteudo = _medida(achado.group(2))
            if valor_conteudo > 0 and medida_conteudo:
                unidade_conteudo = {
                    "ML": "ML", "MLS": "ML", "L": "L", "LT": "L", "LTS": "L",
                    "CL": "CL", "G": "G", "GR": "G", "GRS": "G", "KG": "KG",
                    "KGS": "KG", "MG": "MG",
                }.get(achado.group(2).upper(), medida_conteudo[0])
                return (
                    f"{tipo or 'CX'}{_numero_token(valor_conteudo)}"
                    f"{unidade_conteudo}"
                )

    if quantidade is not None:
        return f"{tipo or 'UN'}{quantidade}"
    if tipo:
        return tipo

    # Sem embalagem explícita: a própria unidade comercial identifica o item.
    medida = _medida(codigo_ucom)
    if medida:
        return medida[0]
    return "UN"


def embalagem_para_chave(unidade_compra="", descricao="") -> str:
    """Sufixo de embalagem que entra na chave (``""`` para UN/KG/L...).

    UN/KG/L são o "estado natural" e não recebem sufixo para preservar as chaves
    antigas. Qualquer embalagem declarada (CX24, CX, FD10X1KG...) separa o produto.
    """
    token = normalizar_embalagem(unidade_compra, descricao)
    if token in {"", "UN", "KG", "L", "M", "M2", "M3"}:
        return ""
    return token




def interpretar_embalagem(unidade_compra, descricao="") -> dict:
    """Lê a embalagem da uCom + descrição e devolve o fator para a unidade base.

    Retorno:
        unidade_compra          uCom original (maiúscula)
        tipo_embalagem          CAIXA / FARDO / PACOTE... ("" quando não é embalagem)
        qtd_por_embalagem       unidades vendáveis (ou conteúdo, na embalagem de peso)
        unidade_base            UN / KG / L / M...
        conteudo_por_embalagem  conteúdo TOTAL em `conteudo_unidade` (12x300ML = 3,6 L)
        confirmada              True só quando o fator foi lido de forma explícita
        origem                  "unidade_compra" | "descricao" | "nao_identificada"

    Regras (na ordem):
    1. uCom é medida (KG/L/UN...) → preço já está na unidade base, fator 1;
    2. uCom tem sufixo numérico (CX24 / UN10) → fator = número;
    3. descrição "12X300ML" → fator = 12 unidades vendáveis (conteúdo registrado);
    4. descrição "CAIXA COM 24 UN" / "CX 24" → fator = 24;
    5. descrição "CX 15KG" (sem contagem) → fator = 15 KG.

    Quando nada é encontrado, fator = 1 e `confirmada=False`: o item NUNCA é
    normalizado "no chute" (o preço cru do XML é preservado).
    """
    codigo_ucom = re.sub(r"[^A-Z0-9]", "", str(unidade_compra or "").upper())
    texto = normalizar_texto(descricao)
    resultado = {
        "unidade_compra": str(unidade_compra or "").strip().upper(),
        "tipo_embalagem": "",
        "qtd_por_embalagem": 1.0,
        "unidade_base": "UN",
        "conteudo_por_embalagem": None,
        "conteudo_unidade": "",
        "confirmada": False,
        "origem": "nao_identificada",
    }

    # 1) uCom é uma unidade de medida: o preço do XML já é por KG/L/metro.
    medida = _medida(codigo_ucom)
    if medida:
        resultado["unidade_base"] = medida[0]
        resultado["origem"] = "unidade_compra"
        return resultado

    # 2) Sufixo numérico explícito na uCom: CX24, CX027, FD12, UN10...
    sufixo = _RE_SUFIXO_EMBALAGEM.match(codigo_ucom)
    if sufixo:
        prefixo, quantidade = sufixo.group(1), int(sufixo.group(2))
        resultado["tipo_embalagem"] = SIMBOLOS_EMBALAGEM.get(prefixo, "EMBALAGEM")
        if 1 < quantidade <= 1000:
            resultado.update(
                qtd_por_embalagem=float(quantidade),
                confirmada=True,
                origem="unidade_compra",
            )
        return resultado

    # Tipo pela uCom (mesmo sem fator explícito, a Auditoria mostra "CAIXA").
    prefixo = _prefixo_embalagem(codigo_ucom)
    if prefixo:
        resultado["tipo_embalagem"] = SIMBOLOS_EMBALAGEM[prefixo]

    # 3) "N X conteúdo" na descrição: 12X300ML, 10X1KG, 20X900ML...
    achado = _RE_CONTAGEM_X_CONTEUDO.search(texto)
    if achado:
        quantidade = int(achado.group(1))
        valor_conteudo = _decimal(achado.group(2))
        medida_conteudo = _medida(achado.group(3))
        if 1 < quantidade <= 1000 and valor_conteudo > 0 and medida_conteudo:
            base, fator = medida_conteudo
            resultado.update(
                qtd_por_embalagem=float(quantidade),
                unidade_base="UN",
                conteudo_por_embalagem=round(valor_conteudo * fator * quantidade, 6),
                conteudo_unidade=base,
                confirmada=True,
                origem="descricao",
            )
            return resultado

    # 4) Contagem explícita ao lado da palavra de embalagem: "CAIXA COM 24 UN", "CX 24".
    achado = _RE_EMBALAGEM_CONTAGEM.search(texto)
    if achado:
        quantidade = int(achado.group(1))
        if 1 < quantidade <= 1000:
            resultado.update(
                qtd_por_embalagem=float(quantidade),
                unidade_base="UN",
                confirmada=True,
                origem="descricao",
            )
            return resultado

    # 4b) uCom já é embalagem e a descrição diz apenas quantas unidades: "24 UN".
    if prefixo:
        achado = _RE_CONTAGEM_UNIDADES.search(texto)
        if achado:
            quantidade = int(achado.group(1))
            if 1 < quantidade <= 1000:
                resultado.update(
                    qtd_por_embalagem=float(quantidade),
                    unidade_base="UN",
                    confirmada=True,
                    origem="descricao",
                )
                return resultado

    # 5) Embalagem de peso/volume: "CX 15KG", "FD 20 L" → preço por KG/L.
    achado = _RE_EMBALAGEM_CONTEUDO.search(texto)
    if achado:
        valor_conteudo = _decimal(achado.group(1))
        medida_conteudo = _medida(achado.group(2))
        if valor_conteudo > 0 and medida_conteudo:
            base, fator = medida_conteudo
            total = round(valor_conteudo * fator, 6)
            resultado.update(
                qtd_por_embalagem=total,
                unidade_base=base,
                conteudo_por_embalagem=total,
                conteudo_unidade=base,
                confirmada=total > 1.0,
                origem="descricao",
            )
        return resultado

    return resultado



def normalizar_quantidades(quantidade, valor_unitario, embalagem=None) -> tuple:
    """Mantém os valores CRUS do XML: a embalagem é identidade, não conversão.

    A regra de negócio mudou: 1 CX de 24 UN não vira 24 UN para calcular média ou
    variação. "ORIGINAL 600ML CX C/24" e "ORIGINAL 600ML UN" são produtos
    diferentes e cada um usa o seu próprio preço da nota. O parâmetro `embalagem`
    continua aceito por compatibilidade, mas não altera quantidade/preço.
    """
    return float(quantidade or 0.0), float(valor_unitario or 0.0)


def aplicar_embalagem_aprendida(embalagem, mapeamento) -> dict:
    """Completa a embalagem do XML com o fator já aprendido para o mesmo cProd.

    O dado do XML atual manda quando a memória é automática: só usa a memória se a
    linha atual não permitiu identificar o fator (ex.: XML sem descrição detalhada, mas
    o fornecedor já foi aprendido em uma nota anterior). A exceção é a correção
    MANUAL da Auditoria, que é a palavra final do administrador e prevalece inclusive
    sobre um fator que o XML traga.
    """
    if mapeamento is None:
        return embalagem
    resultado = dict(embalagem or {})
    fator_memoria = float(getattr(mapeamento, "qtd_por_embalagem", 0.0) or 0.0)
    if fator_memoria <= 1.0:
        return resultado
    manual = str(getattr(mapeamento, "origem", "") or "").strip().lower() == "manual"
    if (
        not manual
        and resultado.get("confirmada")
        and float(resultado.get("qtd_por_embalagem") or 0) > 1
    ):
        return resultado  # nota atual trouxe o fator: não troca pela memória automática
    resultado.update(
        qtd_por_embalagem=fator_memoria,
        unidade_base=str(getattr(mapeamento, "unidade_base", "") or "UN"),
        confirmada=True,
        origem="manual" if manual else "memoria",
    )
    if not resultado.get("unidade_compra"):
        resultado["unidade_compra"] = str(getattr(mapeamento, "u_com_gravada", "") or "")
    return resultado


def formatar_embalagem(embalagem) -> str:
    """Texto curto para a tela: "CAIXA 24 UN", "FARDO 15 KG" ("" quando não há)."""
    if not embalagem or not embalagem.get("confirmada"):
        return ""
    fator = float(embalagem.get("qtd_por_embalagem") or 1.0)
    if fator <= 1.0:
        return ""
    tipo = str(embalagem.get("tipo_embalagem") or "EMBALAGEM")
    unidade = str(embalagem.get("unidade_base") or "UN")
    fator_texto = f"{fator:.3f}".rstrip("0").rstrip(".")
    return f"{tipo} {fator_texto} {unidade}"


# ──────────────────────────────────────────────────────────────────────────────
# Memória de aprendizado (de-para fornecedor + cProd)
# ──────────────────────────────────────────────────────────────────────────────
_NAO_INFORMADO = object()



def normalizar_cnpj(cnpj) -> str:
    """CNPJ só com dígitos (a chave da memória nunca depende de máscara)."""
    return re.sub(r"\D", "", str(cnpj or ""))


def normalizar_codigo_produto(codigo) -> str:
    """cProd do fornecedor: sem espaços, maiúsculo (XMLs variam caixa/espaço)."""
    return str(codigo or "").strip().upper()


_CAMPOS_XML_ITEM = ("cProd", "uCom", "xProd", "qCom", "vUnCom", "cEAN")


def _itens_do_xml(caminho_xml):
    """Itens do XML salvo da nota (cProd/uCom/descrição/quantidade/EAN).

    Devolve None quando o arquivo não existe ou não é um XML legível — o backfill
    apenas ignora a nota (não interrompe a sincronização da base).
    """
    caminho = Path(str(caminho_xml or ""))
    candidatos = [caminho]
    if not caminho.is_absolute():
        raiz = Path(__file__).resolve().parents[1]
        candidatos += [raiz / caminho, Path.cwd() / caminho]
    arquivo = next((c for c in candidatos if c.is_file()), None)
    if arquivo is None:
        return None
    try:
        raiz_xml = ET.parse(str(arquivo)).getroot()
    except (ET.ParseError, OSError):
        return None

    itens = []
    for det in raiz_xml.iter():
        if str(det.tag).rsplit("}", 1)[-1] != "det":
            continue
        prod = next(
            (filho for filho in det if str(filho.tag).rsplit("}", 1)[-1] == "prod"),
            None,
        )
        if prod is None:
            continue
        campos = {}
        for campo in prod:
            nome = str(campo.tag).rsplit("}", 1)[-1]
            if nome in _CAMPOS_XML_ITEM:
                campos[nome] = (campo.text or "").strip()
        c_prod = normalizar_codigo_produto(campos.get("cProd"))
        if not c_prod:
            continue
        itens.append(
            {
                "c_prod": c_prod,
                "u_com": str(campos.get("uCom") or "").strip().upper(),
                # xProd pode vir escapado no XML ("&amp; SKIRT STEAK"): limpa na
                # entrada para a chave (descrição/quantidade) casar com o item.
                "descricao": limpar_entidades_texto(campos.get("xProd") or ""),
                "quantidade": _decimal(campos.get("qCom")),
                "ean": str(campos.get("cEAN") or "").strip().lstrip("0"),
            }
        )
    return itens


def _enriquecer_codigos_do_xml(db, resumo) -> None:
    """Recupera cProd/uCom dos itens antigos relendo o XML salvo de cada nota.

    A base importada antes do de-para não tem a chave do fornecedor e, sem ela, não há
    como aprender. O XML de origem fica guardado em `notas_fiscais.caminho_xml`, então
    o backfill relê o arquivo e casa cada item pela DESCRIÇÃO + QUANTIDADE — a posição
    não serve, porque a importação ignora os itens de bonificação/doação.
    """
    from backend.models import ItemNota, NotaFiscal

    ids_pendentes = {
        nota_id
        for (nota_id,) in db.query(ItemNota.nota_id)
        .filter(ItemNota.c_prod.is_(None))
        .distinct()
        .all()
        if nota_id is not None
    }
    if not ids_pendentes:
        return
    notas = (
        db.query(NotaFiscal)
        .filter(NotaFiscal.id.in_(ids_pendentes))
        .filter(NotaFiscal.caminho_xml.isnot(None))
        .all()
    )
    for nota in notas:
        itens = db.query(ItemNota).filter(ItemNota.nota_id == nota.id).all()
        if not any(not normalizar_codigo_produto(item.c_prod) for item in itens):
            continue
        dados = _itens_do_xml(nota.caminho_xml)
        if not dados:
            resumo["xmls_nao_encontrados"] += 1
            continue
        resumo["xmls_lidos"] += 1
        por_chave = defaultdict(list)
        for dado in dados:
            chave = (normalizar_texto(dado["descricao"]), round(dado["quantidade"], 6))
            por_chave[chave].append(dado)
        for indice, item in enumerate(itens):
            if normalizar_codigo_produto(item.c_prod):
                continue
            chave = (
                normalizar_texto(item.descricao_produto),
                round(float(item.quantidade or 0.0), 6),
            )
            candidatos = por_chave.get(chave) or []
            dado = None
            if candidatos:
                ean_item = str(item.ean or "").lstrip("0")
                dado = next((d for d in candidatos if d["ean"] == ean_item), candidatos[0])
                candidatos.remove(dado)
            elif len(dados) == len(itens):
                dado = dados[indice]  # XML sem bonificação: a ordem é a mesma
            if not dado or not dado["c_prod"]:
                continue
            item.c_prod = dado["c_prod"]
            if not item.unidade_compra and dado["u_com"]:
                item.unidade_compra = dado["u_com"]
            resumo["itens_codigo_recuperado"] += 1


def _agora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def consultar_mapeamento(db, cnpj, c_prod):
    """Mapeamento aprendido para (CNPJ do fornecedor, cProd) ou None."""
    from backend.models import MapeamentoFornecedorProduto

    cnpj = normalizar_cnpj(cnpj)
    c_prod = normalizar_codigo_produto(c_prod)
    if not cnpj or not c_prod:
        return None
    return (
        db.query(MapeamentoFornecedorProduto)
        .filter(MapeamentoFornecedorProduto.cnpj_fornecedor == cnpj)
        .filter(MapeamentoFornecedorProduto.c_prod == c_prod)
        .first()
    )


def aplicar_identidade(mapeamento, ean, descricao, ncm) -> tuple:
    """EAN/descrição/NCM canônicos da memória (o que faltar volta do XML)."""
    if mapeamento is None:
        return ean, descricao, ncm
    return (
        mapeamento.ean_padrao or ean,
        mapeamento.descricao_padrao or descricao,
        mapeamento.ncm_gravado or ncm,
    )



def registrar_aprendizado(
    db,
    cnpj,
    c_prod,
    ean="",
    descricao="",
    ncm="",
    embalagem=None,
    mapeamento=_NAO_INFORMADO,
    commit=True,
):
    """Grava/atualiza a memória de um produto do fornecedor.

    Devolve `(mapeamento, criado, avisos)`.

    Regras de atualização:
    - mapeamento "manual" (corrigido na Auditoria) NUNCA é sobrescrito pela importação;
    - EAN: o primeiro válido vira o canônico; um EAN diferente no XML não troca a
      memória (gera aviso), porque o fornecedor pode reaproveitar o cProd;
    - descrição: a primeira vira a canônica (evita o rótulo mudar a cada nota);
    - embalagem: só é atualizada quando a memória ainda não tinha fator (> 1).
    """
    from backend.models import MapeamentoFornecedorProduto

    cnpj = normalizar_cnpj(cnpj)
    c_prod = normalizar_codigo_produto(c_prod)
    if not cnpj or not c_prod:
        return None, False, []

    if mapeamento is _NAO_INFORMADO:
        mapeamento = consultar_mapeamento(db, cnpj, c_prod)
    criado = mapeamento is None
    if criado:
        mapeamento = MapeamentoFornecedorProduto(
            cnpj_fornecedor=cnpj,
            c_prod=c_prod,
            origem="automatico",
        )
        db.add(mapeamento)

    avisos = []
    manual = str(mapeamento.origem or "").strip().lower() == "manual"
    ean = str(ean or "").strip()
    # A descrição canônica é gravada aqui: limpa as entidades na entrada para a
    # memória não guardar "&amp;" (e o "AMP" não virar marca na leitura).
    descricao = limpar_entidades_texto(str(descricao or "")).strip()
    ncm = str(ncm or "").strip()
    ean_valido = ean.upper() not in ("", "SEM_EAN", "SEM GTIN", "0")
    embalagem = embalagem or {}
    fator = float(embalagem.get("qtd_por_embalagem") or 1.0)
    embalagem_confirmada = bool(embalagem.get("confirmada")) and fator > 1.0

    if not manual:
        if ean_valido:
            if not mapeamento.ean_padrao:
                mapeamento.ean_padrao = ean
            elif mapeamento.ean_padrao != ean:
                avisos.append(
                    f"EAN divergente: memória {mapeamento.ean_padrao} x XML {ean}"
                )
        if descricao and not mapeamento.descricao_padrao:
            mapeamento.descricao_padrao = descricao
        if ncm and (
            not mapeamento.ncm_gravado or mapeamento.ncm_gravado == "00000000"
        ):
            mapeamento.ncm_gravado = ncm
        if embalagem.get("unidade_compra") and not mapeamento.u_com_gravada:
            mapeamento.u_com_gravada = embalagem["unidade_compra"]
        if embalagem_confirmada:
            fator_memoria = float(mapeamento.qtd_por_embalagem or 1.0)
            if fator_memoria <= 1.0:
                mapeamento.qtd_por_embalagem = fator
                mapeamento.unidade_base = embalagem.get("unidade_base") or "UN"
            elif abs(fator_memoria - fator) > 1e-9:
                avisos.append(
                    f"Embalagem divergente: memória {fator_memoria:g} x XML {fator:g}"
                )
    else:
        # Correção manual: só completa o que estiver vazio (o admin manda no resto).
        if ean_valido and not mapeamento.ean_padrao:
            mapeamento.ean_padrao = ean
        if descricao and not mapeamento.descricao_padrao:
            mapeamento.descricao_padrao = descricao
        if ncm and not mapeamento.ncm_gravado:
            mapeamento.ncm_gravado = ncm
        if embalagem_confirmada and float(mapeamento.qtd_por_embalagem or 1.0) <= 1.0:
            mapeamento.qtd_por_embalagem = fator
            mapeamento.unidade_base = embalagem.get("unidade_base") or "UN"

    mapeamento.atualizado_em = _agora()
    if commit:
        db.commit()
        if criado:
            db.refresh(mapeamento)
    return mapeamento, criado, avisos



def _embalagem_do_item(item, mapeamento=None) -> dict:
    """Embalagem de um ItemNota (uCom + descrição), completada pela memória."""
    embalagem = interpretar_embalagem(
        getattr(item, "unidade_compra", "") or "",
        getattr(item, "descricao_produto", "") or "",
    )
    return aplicar_embalagem_aprendida(embalagem, mapeamento)


def _recalcular_item(item, mapeamento=None) -> dict:
    """Recalcula quantidade/preço base de um item a partir dos valores CRUS do XML."""
    embalagem = _embalagem_do_item(item, mapeamento)
    quantidade_base, valor_base = normalizar_quantidades(
        item.quantidade, item.valor_unitario, embalagem
    )
    item.qtd_por_embalagem = float(embalagem.get("qtd_por_embalagem") or 1.0)
    item.unidade_base = str(embalagem.get("unidade_base") or "UN")
    item.quantidade_base = quantidade_base
    item.valor_unitario_base = valor_base
    return embalagem


def aplicar_mapeamento_historico(db, mapeamento) -> int:
    """Reaplica o de-para corrigido a TODOS os itens já importados do fornecedor/cProd.

    Usado quando o administrador corrige um mapeamento na Auditoria: os relatórios
    passam a usar o EAN/descrição/embalagem corrigidos também no histórico. Os valores
    crus (`quantidade`/`valor_unitario`) são preservados; só os campos canônicos e os
    normalizados são reescritos.
    """
    from backend.models import Fornecedor, ItemNota

    if mapeamento is None:
        return 0
    itens = (
        db.query(ItemNota)
        .join(Fornecedor, ItemNota.fornecedor_id == Fornecedor.id)
        .filter(Fornecedor.cnpj == normalizar_cnpj(mapeamento.cnpj_fornecedor))
        .filter(ItemNota.c_prod == normalizar_codigo_produto(mapeamento.c_prod))
        .all()
    )
    for item in itens:
        if mapeamento.ean_padrao:
            item.ean = mapeamento.ean_padrao
        if mapeamento.descricao_padrao:
            item.descricao_produto = mapeamento.descricao_padrao
        if mapeamento.ncm_gravado:
            item.ncm = mapeamento.ncm_gravado
        _recalcular_item(item, mapeamento)
    db.commit()
    return len(itens)


def sincronizar_aprendizado(db, commit=True) -> dict:
    """Reprocessa a base inteira: recalcula embalagens e (re)aprende o de-para.

    É o motor do `python backfill_aprendizado.py`, sem query por item. Primeiro
    relê os XMLs salvos para recuperar `c_prod`/`uCom` de itens antigos; depois
    aprende/atualiza a memória e, numa segunda passada em memória, normaliza todos
    os itens já com a memória completa (inclusive o que acabou de ser aprendido).

    Devolve contadores para o resumo do backfill.
    """
    from backend.models import Fornecedor, ItemNota, MapeamentoFornecedorProduto

    resumo = {
        "itens": 0,
        "itens_com_embalagem": 0,
        "mapeamentos_criados": 0,
        "mapeamentos_atualizados": 0,
        "sem_codigo_fornecedor": 0,
        "itens_codigo_recuperado": 0,
        "xmls_lidos": 0,
        "xmls_nao_encontrados": 0,
    }
    # Base antiga: recupera cProd/uCom dos XMLs salvos antes de tentar aprender.
    _enriquecer_codigos_do_xml(db, resumo)
    mapa = {
        (m.cnpj_fornecedor, m.c_prod): m
        for m in db.query(MapeamentoFornecedorProduto).all()
    }
    candidatos = {}
    consulta = (
        db.query(ItemNota, Fornecedor.cnpj)
        .outerjoin(Fornecedor, ItemNota.fornecedor_id == Fornecedor.id)
    )
    # 1ª passada: escolhe o item mais informativo de cada (fornecedor, cProd).
    for item, cnpj in consulta.yield_per(500):
        chave = None
        if cnpj and item.c_prod:
            chave = (normalizar_cnpj(cnpj), normalizar_codigo_produto(item.c_prod))
        if not chave:
            continue
        embalagem = _embalagem_do_item(item, mapa.get(chave))
        atual = candidatos.get(chave)
        if atual is None or (
            not atual.get("ean_valido") and item.ean not in (None, "", "SEM_EAN")
        ):
            candidatos[chave] = {
                "item": item,
                "ean_valido": item.ean not in (None, "", "SEM_EAN"),
                "embalagem": embalagem,
            }

    for chave, dados in candidatos.items():
        item = dados["item"]
        mapeamento, criado, _avisos = registrar_aprendizado(
            db,
            chave[0],
            chave[1],
            ean=item.ean,
            descricao=item.descricao_produto,
            ncm=item.ncm,
            embalagem=dados["embalagem"],
            mapeamento=mapa.get(chave),
            commit=False,
        )
        if mapeamento is not None:
            mapa[chave] = mapeamento
        resumo["mapeamentos_criados" if criado else "mapeamentos_atualizados"] += 1

    # 2ª passada: a memória (mesmo a recém-aprendida) normaliza todos os itens.
    consulta = (
        db.query(ItemNota, Fornecedor.cnpj)
        .outerjoin(Fornecedor, ItemNota.fornecedor_id == Fornecedor.id)
    )
    for item, cnpj in consulta.yield_per(500):
        resumo["itens"] += 1
        chave = None
        if cnpj and item.c_prod:
            chave = (normalizar_cnpj(cnpj), normalizar_codigo_produto(item.c_prod))
        if not chave:
            resumo["sem_codigo_fornecedor"] += 1
        embalagem = _recalcular_item(item, mapa.get(chave) if chave else None)
        if embalagem.get("confirmada"):
            resumo["itens_com_embalagem"] += 1

    if commit:
        db.commit()
    return resumo

