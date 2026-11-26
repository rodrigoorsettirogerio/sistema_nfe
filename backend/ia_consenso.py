"""Consenso com IA (DeepSeek) para a Auditoria de produtos.

A IA é usada como **segunda opinião** do auditor, nunca para alterar a base sozinha:

- `validar_marcas_com_ia(lista_palavras)`: confirma quais palavras candidatas são
  marcas (fabricante/grife/linha) e não palavras genéricas do produto;
- `resolver_ambiguidade_com_ia(texto1, texto2)`: diz se duas descrições são o mesmo
  produto ou produtos diferentes, com confiança e justificativa.
- `obter_marca_inteligente(descricao)`: extrai a marca comercial de uma descrição com
  MEMÓRIA DE CACHE na tabela `marcas_ia`. A IA só é chamada quando a descrição normalizada
  ainda não foi analisada; a resposta (inclusive "SEM MARCA") fica gravada para as
  próximas rodadas. O recálculo em massa usa esta função para não repetir consultas.

Configuração (variáveis de ambiente, nenhuma chave fica no código):

- `DEEPSEEK_API_KEY` (obrigatória): sem ela as rotas respondem 503 com instrução;
- `DEEPSEEK_MODEL` (opcional): padrão `deepseek-chat`;
- `DEEPSEEK_API_URL` (opcional): padrão `https://api.deepseek.com/chat/completions`;
- `DEEPSEEK_TIMEOUT` (opcional): segundos, padrão 45.

A resposta do modelo é lida como JSON tolerante (aceita bloco ```json, texto ao redor
e variações de booleanos). Qualquer falha vira `ErroIA`, que a rota converte em
HTTP 502/503 — a tela mostra a mensagem e a auditoria continua funcionando sem IA.
"""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, List, Optional

import requests
from dotenv import load_dotenv
from sqlalchemy.exc import IntegrityError

# Carrega o .env da raiz do projeto ANTES de qualquer leitura de DEEPSEEK_*.
# `override=False` preserva variáveis já definidas no ambiente do processo (produção
# pode injetar a chave pelo serviço, sem depender do arquivo local).
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

# Endpoint oficial da API compatível com OpenAI. Sobrescreva em testes/homologação
# com DEEPSEEK_API_URL (proxy interno, por exemplo).
URL_PADRAO = "https://api.deepseek.com/chat/completions"
MODELO_PADRAO = "deepseek-chat"
TEMPO_LIMITE_PADRAO = 45
# Teto por chamada: a lista de marcas é pequena e o prompt não deve virar um lote
# gigante (custo e risco de resposta truncada). O frontend pode dividir em lotes.
MAXIMO_PALAVRAS = 200
MAXIMO_DESCRICAO = 500
# Sentinela gravado na memória quando a IA conclui que a descrição não cita marca.
MARCA_SEM_MARCA = "SEM MARCA"
# Termos que a heurística local pode gravar como marca, mas são tipo/corte/estado/
# abreviação de adjetivo (não fabricante). A IA às vezes confirma a candidata por
# engano; esta lista é a última barreira antes de gravar em `itens_nota`. Mantida
# curta e conservadora: só entram termos com significado genérico inequívoco.
MARCAS_INVALIDAS = frozenset(
    {
        # Palavras genéricas de produto/estado/embalagem que a heurística local ou uma
        # resposta antiga da IA podem ter gravado por engano.
        "OSSO", "LIXO", "TRANSP", "REFOR", "BLOCO", "FGO", "CONTRA", "ENFRIADA",
        "COMUM", "RESF", "RESFRIADO", "RESFRIADA", "VAZIO", "VACIO", "CHILENA",
        "PALMER", "FLOCOS", "VERM", "APIMENTADA", "PURA", "BIFES", "SACOLA", "ROLO",
        "DESCARTAVEL", "OUTROS", "MISTA", "IND", "MEIO", "INFERIOR", "CHORIZO",
        "CUBO", "CALABRESA", "AMOSTRA", "PERUANO", "ESPANHOL", "ANTI", "EUCALIPTO",
        "REINO", "EXTRAFORTE", "PAST", "PASTEUR", "TRAD", "REF", "EXTRA", "INTER",
        "PRODUTO", "SEM", "NOME", "TETRAPAK", "VIRGEM", "ALVEJADO", "ALVEJANTE",
        # Cortes/produtos in natura: são o PRODUTO, nunca a marca.
        "CARNE", "BOVINO", "SUINO", "MAMINHA", "ALCATRA", "PICANHA", "COXAO",
        "ACEM", "PALETA", "PATINHO", "COSTELA", "FILE", "FRANGO", "CONGELADO",
        "CONGELADA", "IN NATURA",
        # Embalagens e categorias de limpeza.
        "CAIXA", "CX", "FD", "FARDO", "PACOTE", "PCT", "GARRAFA", "LATA", "SACHE",
        "DETERGENTE", "DESINFETANTE", "SANITARIA", "LIMPEZA",
        # Adjetivos/estado/origem e pedaços do nome do produto que a IA ou a
        # heurística local devolvem como se fossem fabricante: "VERDE", "GRANDE",
        # "ZERO" descrevem o produto; KAL/PLAST/AMP costumam ser abreviação ou
        # resto de entidade HTML ("&amp;" -> AMP). Nenhum deles individualiza o
        # item, então não podem virar marca nem alias de marca composta.
        "VERDE", "GRANDE", "ZERO", "STEAK", "ORIGEM", "KAL", "CAMPO", "PET",
        "PLAST", "SACO", "FRALDA", "DIAFRAGMA", "DIAPHRAGMA", "ENTRANHA", "AMP",
    }
)
# Abreviações de marca que os fornecedores usam nas descrições (a NF-e não tem o
# campo de marca). A expansão é determinística para não depender do humor da IA.
# A chave pode ter mais de uma palavra ("J CUERVO"): `_completar_marca` compara o
# nome inteiro e `_marca_na_descricao` procura a abreviação como frase, então
# "TEQUILA J CUERVO SILVER" confirma a marca JOSE CUERVO sem gastar tokens.
MARCAS_ABREVIACOES = {
    "FTA": "FANTA",
    "SCHW": "SCHWEPPES",
    "J CUERVO": "JOSE CUERVO",
}
# Marcas reais que a IA vinha devolvendo como "SEM MARCA". A lista é uma trava
# determinística: quando o nome aparece na descrição, ele é extraído mesmo que o
# cache antigo tenha gravado "SEM MARCA" (o registro envenenado é apagado).
# Ela também protege marcas que o vocabulário de produto barrava por engano:
# COOPERBOI estava em QUALIFICADORES (como se fosse corte) e JOSE CUERVO só
# aparecia abreviada ("J CUERVO" — ver MARCAS_ABREVIACOES). Nome desta lista nunca
# é tratado como termo genérico por `_marca_invalida`.
MARCAS_CONHECIDAS = {
    "SADIA": "SADIA",
    "SEARA": "SEARA",
    "PERDIGAO": "PERDIGÃO",
    "TABASCO": "TABASCO",
    "ARRIFANA": "ARRIFANA",
    "VILLA PIVA": "VILLA PIVA",
    "IPANEMA": "IPANEMA",
    "CEPERA": "CEPERA",
    "COOPERBOI": "COOPERBOI",
    "JOSE CUERVO": "JOSE CUERVO",
}
# Teto de segurança para o nome devolvido (não é uma descrição: é só a marca).
MAXIMO_MARCA = 80
# Tamanho do lote que o front envia por requisição no recálculo em massa (progresso).
MAXIMO_LOTE_MARCAS = 50


class ErroIA(RuntimeError):
    """Falha controlada da consulta à IA (a rota converte em resposta HTTP)."""

    def __init__(self, mensagem: str, status: int = 502):
        super().__init__(mensagem)
        self.status = status


class IAnaoconfigurada(ErroIA):
    """`DEEPSEEK_API_KEY` ausente: a rota orienta o administrador a configurar."""

    def __init__(self, mensagem: Optional[str] = None):
        super().__init__(
            mensagem
            or (
                "IA não configurada no servidor: defina a variável de ambiente "
                "DEEPSEEK_API_KEY e reinicie a API."
            ),
            status=503,
        )


def _api_key() -> str:
    return (os.getenv("DEEPSEEK_API_KEY") or "").strip()


def configurada() -> bool:
    """True quando a chave da DeepSeek está presente no ambiente do processo."""
    return bool(_api_key())


def modelo() -> str:
    """Modelo usado nas chamadas (DEEPSEEK_MODEL ou o padrão)."""
    return (os.getenv("DEEPSEEK_MODEL") or MODELO_PADRAO).strip() or MODELO_PADRAO


def _limpar_espacos(texto) -> str:
    return " ".join(str(texto or "").split())


# Números de controle do fornecedor no fim da descrição ("POLENTA - 1", "- 2"...)
# não identificam o produto. Eles saem ANTES de comparar/agrupar e antes de enviar a
# descrição para a IA; o texto original continua intacto em itens_nota.
_NUMERO_CONTROLE_FINAL_RE = re.compile(r"(?:\s*[-–—]\s*\d{1,3})+\s*$")


def remover_numero_controle(descricao) -> str:
    """Remove o sufixo de controle (" - 1", " - 2") mantendo o resto da descrição."""
    original = _limpar_espacos(descricao)
    sem_controle = _limpar_espacos(_NUMERO_CONTROLE_FINAL_RE.sub("", original))
    return sem_controle or original


def chave_equivalencia_descricao(descricao) -> str:
    """Chave para considerar duas descrições a MESMA descrição de auditoria.

    Ignora maiúsculas/minúsculas, espaços repetidos e os números de controle do fim
    (``- 1``, ``- 2``...): as quatro variações de "POLENTA ... - 1..4" viram uma única
    sugestão, em vez de quatro cards idênticos.
    """
    return remover_numero_controle(descricao).casefold()


def _completar_marca(nome) -> str:
    """Maiúsculas + expansão das abreviações conhecidas (FTA -> FANTA)."""
    marca = _limpar_espacos(nome).upper()
    return MARCAS_ABREVIACOES.get(marca, marca)


def _marca_invalida(nome) -> bool:
    """True para termos que nunca podem ser gravados como marca (REFOR, TRANSP...).

    A comparação ignora acento para a barreira também pegar COXÃO/ACÉM, que são cortes
    de carne e não marcas. Uma marca COMPOSTA cai na mesma barreira quando TODAS as
    palavras são genéricas/de produto ("PLAST PET", "VERDE CAMPO", "PRIME STEAK
    ORIGEM"): empilhar termos genéricos não individualiza o item, e a resposta antiga
    ficava no cache e voltava em todas as rodadas seguintes. Nome de
    `MARCAS_CONHECIDAS` passa sempre — COOPERBOI e JOSE CUERVO são marcas reais e não
    podem ser barradas só porque as palavras existem no vocabulário de produto.
    """
    partes = _tokens_busca_marca(nome)
    if not partes:
        return False
    chave = " ".join(partes)
    if chave in _chaves_marcas_conhecidas():
        return False
    if chave in MARCAS_INVALIDAS:
        return True
    return len(partes) > 1 and all(
        parte in _termos_genericos_de_marca() for parte in partes
    )


def _marca_entre_parenteses(descricao) -> str:
    """Marca de descrições no formato ``Produto Sem Nome (Marca)``.

    A extração determinística evita gastar uma consulta de IA e impede que o texto
    "SEM NOME" seja confundido com o sentinela ``SEM MARCA``. Só é aplicada quando a
    descrição realmente declara que não tem nome; parênteses com medidas/embalagens
    ("(CX 12)") não entram por conterem números ou termo genérico.
    """
    texto = _limpar_espacos(descricao)
    if "SEM NOME" not in texto.upper():
        return ""
    for candidato in reversed(re.findall(r"\(([^()]{2,80})\)", texto)):
        if re.search(r"\d", candidato):
            continue
        marca = _limpar_marca_resposta(candidato)
        if marca and marca != MARCA_SEM_MARCA and not _marca_invalida(marca):
            return marca
    return ""


def _marca_conhecida_na_descricao(descricao) -> str:
    """Marca real citada na descrição (lista de segurança), ou ``""``.

    A comparação ignora acento/pontuação e exige o nome inteiro: "VILLA PIVA"
    só casa com as duas palavras juntas, e "SADIA" não casa dentro de outra
    palavra. É uma trava conservadora para os casos em que a IA/cache errou.
    """
    texto = _normalizar_descricao_memoria(descricao).replace("_", " ")
    if not texto:
        return ""
    for nome in sorted(MARCAS_CONHECIDAS, key=len, reverse=True):
        padrao = r"(?<![A-Z0-9])" + re.escape(nome) + r"(?![A-Z0-9])"
        if re.search(padrao, texto):
            return MARCAS_CONHECIDAS[nome]
    return ""


# Palavras que aparecem em NOMES DE MARCA, mas são tipo/categoria do produto.
# Elas não viram token isolado (senão "GIN" ou "XAROPE" marcariam qualquer
# descrição); o nome completo da marca continua valendo como frase.
# A lista inclui os adjetivos/estado/origem que mais apareciam como falso
# positivo (VERDE, GRANDE, ZERO, STEAK...) e restos de entidade (AMP).
PALAVRAS_GENERICAS_DE_MARCA = frozenset(
    {
        "A", "AS", "COM", "DA", "DAS", "DE", "DEL", "DO", "DOS", "E", "O", "OS",
        "APERITIVO", "CACACA", "COPOS", "EMBALAGENS", "GIN", "LICOR", "PROFESSIONAL",
        "SAQUE", "TEQUILA", "VERMOUTH", "WHISKY", "XAROPE",
        "VERDE", "GRANDE", "ZERO", "STEAK", "ORIGEM", "KAL", "CAMPO", "PET",
        "PLAST", "SACO", "FRALDA", "DIAFRAGMA", "DIAPHRAGMA", "ENTRANHA", "AMP",
    }
)
# Uma marca de 2+ palavras só gera alias de UMA palavra quando ela é distintiva:
# pelo menos 5 letras e fora de termos genéricos/unidades/cortes. Assim
# "GIN GORDONS" → GORDONS, mas "VERDE CAMPO" não casa com qualquer "VERDE".
MINIMO_LETRAS_ALIAS_ISOLADO = 5
# Cache de unidades/cortes/qualificadores de `backend.marcas` (import tardio para
# este módulo continuar independente das regras de inferência por NCM).
_TERMOS_DE_PRODUTO = None
# Cache do vocabulário completo que nunca individualiza marca (ver
# `_termos_genericos_de_marca`) e das chaves canônicas de `MARCAS_CONHECIDAS`.
_TERMOS_GENERICOS_MARCA = None
_CHAVES_MARCAS_CONHECIDAS = None


def _termos_de_produto() -> frozenset:
    """Unidades, embalagens, cortes e qualificadores que nunca viram alias só."""
    global _TERMOS_DE_PRODUTO
    if _TERMOS_DE_PRODUTO is None:
        from backend import marcas as regras_marcas

        _TERMOS_DE_PRODUTO = frozenset(regras_marcas.UNIDADES) | frozenset(
            regras_marcas.QUALIFICADORES
        )
    return _TERMOS_DE_PRODUTO


def _termos_genericos_de_marca() -> frozenset:
    """Vocabulário que, sozinho, nunca individualiza uma marca.

    União de `MARCAS_INVALIDAS`, `PALAVRAS_GENERICAS_DE_MARCA` e do vocabulário de
    unidades/qualificadores/cortes de `backend.marcas`. É a base da trava de marca
    COMPOSTA em `_marca_invalida`: "PLAST PET" e "VERDE CAMPO" são apenas termos
    genéricos empilhados. Montado uma vez por processo.
    """
    global _TERMOS_GENERICOS_MARCA
    if _TERMOS_GENERICOS_MARCA is None:
        _TERMOS_GENERICOS_MARCA = (
            frozenset(MARCAS_INVALIDAS)
            | frozenset(PALAVRAS_GENERICAS_DE_MARCA)
            | _termos_de_produto()
        )
    return _TERMOS_GENERICOS_MARCA


def _chaves_marcas_conhecidas() -> frozenset:
    """Nomes de `MARCAS_CONHECIDAS` na forma canônica (montado uma vez por processo)."""
    global _CHAVES_MARCAS_CONHECIDAS
    if _CHAVES_MARCAS_CONHECIDAS is None:
        _CHAVES_MARCAS_CONHECIDAS = frozenset(
            _chave_marca(nome) for nome in MARCAS_CONHECIDAS.values()
        )
    return _CHAVES_MARCAS_CONHECIDAS


def _alias_isolado_valido(parte: str) -> bool:
    """True quando uma palavra de marca COMPOSTA pode valer como alias sozinha."""
    if len(parte) < MINIMO_LETRAS_ALIAS_ISOLADO or parte.isdigit():
        return False
    if parte in PALAVRAS_GENERICAS_DE_MARCA or parte in MARCAS_INVALIDAS:
        return False
    return parte not in _termos_de_produto()


def _tokens_busca_marca(texto) -> List[str]:
    """Tokens normalizados da descrição/marca (pontuação vira separador)."""
    normalizado = _normalizar_descricao_memoria(texto).replace("_", " ")
    return re.findall(r"[A-Z0-9]+", normalizado)


def _aliases_da_marca(nome) -> List[str]:
    """Formas pelas quais uma marca pode aparecer na descrição.

    Regras (guardas contra falso positivo):
    - o nome completo sempre vale como frase ("VILLA PIVA");
    - marca de UMA palavra (SADIA) também vale isolada;
    - marca de 2+ palavras só gera alias de uma palavra quando ele é DISTINTIVO
      (>= 5 letras e fora de termos genéricos/unidades/cortes), para "VERDE
      CAMPO" não casar com qualquer "VERDE" da descrição;
    - pares vizinhos concatenados continuam valendo ("COCA COLA" → COCACOLA).
    """
    partes = _tokens_busca_marca(nome)
    if not partes:
        return []
    aliases = {" ".join(partes)}
    if len(partes) == 1:
        aliases.add(partes[0])
    else:
        aliases.update(parte for parte in partes if _alias_isolado_valido(parte))
        for esquerda, direita in zip(partes, partes[1:]):
            if (
                len(esquerda) >= 3
                and len(direita) >= 1
                and not esquerda.isdigit()
                and not direita.isdigit()
                and esquerda not in PALAVRAS_GENERICAS_DE_MARCA
                and direita not in PALAVRAS_GENERICAS_DE_MARCA
            ):
                aliases.add(esquerda + direita)
    return sorted(aliases, key=len, reverse=True)


def _marca_por_tokenizacao(descricao, sessao) -> str:
    """NÍVEL 2: marca do catálogo mestre (`marcas_master`) citada na descrição.

    Consulta SÓ o catálogo mestre e a lista curta `MARCAS_CONHECIDAS`. As marcas
    memorizadas em `marcas_ia` não entram como candidatas de propósito: a memória é
    entrada do cache (candidato no NÍVEL 1), não catálogo — usar as respostas
    antigas aqui transformava um palpite de IA em regra fixa e autoalimentava o
    erro ("VERDE", "STEAK", ...). As frases completas têm prioridade sobre tokens
    isolados ("GUARANA ANTARCTICA" antes de "ANTARCTICA"), e a primeira marca
    encontrada na ordem dos tokens é devolvida. Retorna ``""`` quando nenhuma marca
    conhecida aparece.
    """
    tokens = _tokens_busca_marca(descricao)
    if not tokens:
        return ""
    texto = f" {' '.join(tokens)} "

    from backend.models import MarcaMaster

    candidatos = [
        nome
        for (nome,) in sessao.query(MarcaMaster.nome_marca)
        .order_by(MarcaMaster.nome_marca.asc())
        .all()
    ]
    candidatos.extend(MARCAS_CONHECIDAS.values())

    mapa = {}
    for marca in candidatos:
        nome = _limpar_espacos(marca)
        if not nome or eh_sem_marca(nome) or _marca_invalida(nome):
            continue
        for alias in _aliases_da_marca(nome):
            mapa.setdefault(alias, nome)

    # Frases completas primeiro: evita devolver "ANTARCTICA" quando a descrição
    # diz "GUARANA ANTARCTICA".
    for alias in sorted((chave for chave in mapa if " " in chave), key=len, reverse=True):
        if f" {alias} " in texto:
            return _completar_marca(mapa[alias])
    for token in tokens:
        if token in mapa:
            return _completar_marca(mapa[token])
    return ""


def _chave_marca(texto) -> str:
    """Forma canônica do nome, para comparar marca da IA x marca do catálogo."""
    return " ".join(_tokens_busca_marca(texto))


def _marca_no_catalogo(marca, sessao) -> bool:
    """True quando `marca` existe em `marcas_master` (ou na lista `MARCAS_CONHECIDAS`).

    A comparação ignora acento, caixa e pontuação ("Villa Piva" == "VILLA PIVA").
    """
    from backend.models import MarcaMaster

    chave = _chave_marca(marca)
    if not chave:
        return False
    if chave in {_chave_marca(nome) for nome in MARCAS_CONHECIDAS.values()}:
        return True
    return any(
        _chave_marca(nome_marca) == chave
        for (nome_marca,) in sessao.query(MarcaMaster.nome_marca).all()
    )


def _marca_na_descricao(marca, descricao) -> bool:
    """True quando a marca aparece na própria descrição (palavras contíguas).

    A comparação roda sobre a descrição normalizada e exige fronteira de palavra,
    para "SUCO" não casar dentro de "SUCOS". "COCA-COLA" aceita "COCA COLA" e a
    abreviação conhecida do catálogo também confirma a marca ("SCHW" → SCHWEPPES).
    """
    texto = _normalizar_descricao_memoria(descricao).replace("_", " ")
    partes = _tokens_busca_marca(marca)
    if not texto or not partes:
        return False
    chave = " ".join(partes)
    for abreviacao, completa in MARCAS_ABREVIACOES.items():
        if _chave_marca(completa) == chave and re.search(
            r"(?<![A-Z0-9])" + re.escape(abreviacao) + r"(?![A-Z0-9])", texto
        ):
            return True
    sequencia = r"[^A-Z0-9]*".join(re.escape(parte) for parte in partes)
    return bool(re.search(r"(?<![A-Z0-9])" + sequencia + r"(?![A-Z0-9])", texto))


def _marca_aceitavel(marca, descricao, sessao) -> bool:
    """Resposta da IA só vale se for VERIFICÁVEL (catálogo mestre ou descrição).

    A IA às vezes preenche o campo com um palpite ("VERDE", "GRANDE", uma grife
    que não existe no texto) só para não devolver vazio. Sem esta trava o palpite
    vira cache (`marcas_ia`) e contamina todas as próximas rodadas. A marca é
    aceita quando:
    - não é sentinela nem termo genérico (`MARCAS_INVALIDAS`); e
    - existe em `marcas_master`/`MARCAS_CONHECIDAS`; ou
    - aparece na própria descrição normalizada (todas as palavras do nome,
      admitindo a abreviação conhecida no lugar do nome completo).
    Fora disso a resposta é tratada como "SEM MARCA".
    """
    if not marca or eh_sem_marca(marca) or _marca_invalida(marca):
        return False
    return _marca_no_catalogo(marca, sessao) or _marca_na_descricao(marca, descricao)


def limpar_memoria_marcas_invalidas(db=None) -> int:
    """Remove do cache ``marcas_ia`` as respostas que são termos genéricos.

    Uma resposta antiga como ``OSSO`` ou ``TRANSP`` envenenaria todas as próximas
    rodadas (o cache é consultado antes da IA). A limpeza é idempotente e devolve
    quantos registros foram apagados. Pode receber uma sessão SQLAlchemy já aberta
    (testes/rotinas) ou abrir a sessão padrão do projeto.
    """
    from backend.database import SessionLocal
    from backend.models import MarcaIA

    propria_sessao = db is None
    sessao = db or SessionLocal()
    try:
        invalidos = [
            registro
            for registro in sessao.query(MarcaIA).all()
            if _marca_invalida(registro.marca_extraida)
        ]
        for registro in invalidos:
            sessao.delete(registro)
        if invalidos:
            sessao.commit()
        return len(invalidos)
    finally:
        if propria_sessao:
            sessao.close()


def _extrair_json(conteudo):
    """Converte a resposta do modelo em objeto Python, tolerando cercas de código.

    O modelo às vezes devolve ```json ... ``` ou um texto antes/depois do JSON.
    Tentamos o texto inteiro, o conteúdo da cerca e, por fim, o primeiro bloco
    `{...}`/`[...]` encontrado.
    """
    if isinstance(conteudo, (dict, list)):
        return conteudo
    texto = str(conteudo or "").strip()
    if not texto:
        raise ErroIA("A IA respondeu vazio; tente novamente.")

    tentativas = [texto]
    sem_cerca = re.sub(r"^```(?:json)?\s*", "", texto, flags=re.IGNORECASE)
    sem_cerca = re.sub(r"\s*```$", "", sem_cerca).strip()
    if sem_cerca != texto:
        tentativas.append(sem_cerca)
    for abertura, fechamento in (("{", "}"), ("[", "]")):
        inicio = texto.find(abertura)
        fim = texto.rfind(fechamento)
        if inicio >= 0 and fim > inicio:
            tentativas.append(texto[inicio : fim + 1])

    for tentativa in tentativas:
        try:
            return json.loads(tentativa)
        except (TypeError, ValueError):
            continue
    raise ErroIA("A IA respondeu em formato inesperado (JSON inválido).")


def _booleano(valor) -> Optional[bool]:
    """Booleano tolerante às variações que o modelo costuma devolver."""
    if isinstance(valor, bool):
        return valor
    if isinstance(valor, (int, float)):
        return bool(valor)
    texto = _limpar_espacos(valor).lower()
    if texto in {"true", "sim", "s", "1", "marca", "yes", "y"}:
        return True
    if texto in {"false", "nao", "não", "n", "0", "none", "null", "nao_marca"}:
        return False
    return None


def sugerir_correcao_grupo(descricoes, descricao_padrao=None) -> dict:
    """Pede à IA qual descrição deve SAIR de um grupo possivelmente misturado.

    O agrupamento automático usa EAN/nome e pode juntar produtos diferentes
    (por exemplo, Chopp 20L e 30L com o mesmo EAN). A IA recebe todas as
    descrições do grupo e devolve uma sugestão — nada é gravado nem alterado
    aqui; a rota da Auditoria persiste a sugestão para aprovação humana.

    Resposta:
        {
          "status": "sucesso" | "vazio",
          "modelo": ..., "descricoes": [...], "mesmo_grupo": bool,
          "manter": "...", "motivo": "...", "confianca": 0..1,
          "sugestoes": [{"item_sugerido_sair": "...", "motivo": "...",
                         "confianca": 0..1}]
        }
    """
    lista = []
    vistos = set()
    for descricao in descricoes or []:
        # O número de controle ("- 1", "- 2") não é parte do produto: a IA recebe
        # uma única descrição por produto e não devolve quatro sugestões iguais.
        texto = remover_numero_controle(descricao)
        if texto and texto.casefold() not in vistos:
            vistos.add(texto.casefold())
            lista.append(texto)
    if len(lista) < 2:
        return {
            "status": "vazio",
            "modelo": modelo(),
            "descricoes": lista,
            "mesmo_grupo": True,
            "manter": lista[0] if lista else "",
            "motivo": "O grupo tem menos de duas descrições; não há o que separar.",
            "confianca": 1.0,
            "sugestoes": [],
        }
    if len(lista) > MAXIMO_DESCRICOES_GRUPO:
        raise ErroIA(
            f"O grupo tem {len(lista)} descrições; o limite por consulta de IA é "
            f"{MAXIMO_DESCRICOES_GRUPO}.",
            status=422,
        )

    padrao = _descricao_correspondente(descricao_padrao, lista) or lista[0]
    prompt = (
        "Você audita o catálogo de um supermercado brasileiro. O agrupamento "
        "automático juntou as descrições abaixo como se fossem o MESMO produto "
        "vendável. Sua tarefa é apontar, SEM INVENTAR NADA, quais descrições "
        "devem SAIR do grupo porque são outro produto. A MARCA É PARTE DA "
        "IDENTIDADE do produto e É CRITÉRIO DE SEPARAÇÃO: marcas/fabricantes "
        "diferentes DEVEM ser separados, mesmo quando o tipo, o sabor e o "
        "volume são iguais (ex.: ARRICO x EL CHEF, COCA-COLA x PEPSI, QUEIJO "
        "MINAS CAMANDUCAIA x QUEIJO MINAS IPANEMA são produtos diferentes e "
        "NÃO devem ser agrupados). "
        "Mantenha no mesmo grupo apenas descrições do mesmo produto E da mesma "
        "marca (ou quando a marca não estiver identificada em nenhuma delas). "
        "Também sugira separação quando mudar o PRODUTO (tipo, sabor ou "
        "variedade, ex.: POLENTA x BATATA) ou o VOLUME/PESO/QUANTIDADE (ex.: "
        "1KG x 5KG, CHOPP 20L x CHOPP 30L). Se todas as descrições forem o "
        "mesmo produto da mesma marca, devolva mesmo_grupo=true e sair=[]; não "
        "sugira correção apenas por ordem, abreviação, acento ou erro de "
        "grafia; marca diferente, porém, é motivo suficiente para separar. "
        "NUNCA diga que marca não é critério de separação: quando as marcas "
        "forem diferentes, a separação é obrigatória. "
        "Responda SOMENTE com um objeto JSON válido no formato: "
        '{"mesmo_grupo": true ou false, "manter": "descrição exata que deve '
        'ficar no grupo", "sair": ["descrição exata que deve sair"], '
        '"motivo": "explicação curta", "confianca": número de 0 a 1}. '
        f"A descrição mais antiga do grupo (padrão) é: "
        f"{json.dumps(padrao, ensure_ascii=False)}. "
        f"Descrições do grupo: {json.dumps(lista, ensure_ascii=False)}."
    )
    conteudo = _chamar_deepseek(
        [
            {
                "role": "system",
                "content": (
                    "Você é um auditor de catálogo rigoroso, responde sempre em "
                    "JSON válido, sem markdown, e só usa descrições da lista."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        temperatura=0.0,
        max_tokens=900,
    )
    dados = _extrair_json(conteudo)
    if not isinstance(dados, dict):
        raise ErroIA("A IA não devolveu a sugestão em formato de objeto JSON.")

    mesmo = _booleano(
        dados.get(
            "mesmo_grupo",
            dados.get("mesmo_produto", dados.get("iguais", dados.get("same_group"))),
        )
    )
    motivo = _limpar_espacos(
        dados.get("motivo") or dados.get("justificativa") or dados.get("reason")
    )
    confianca = _confianca(dados.get("confianca", dados.get("confidence", 0.0)))

    saidas = dados.get("sair", dados.get("itens_sair", dados.get("remover"))) or []
    if isinstance(saidas, str):
        saidas = [saidas]
    if not isinstance(saidas, list):
        saidas = []
    validas = []
    for item in saidas:
        descricao = _descricao_correspondente(item, lista)
        if descricao and descricao not in validas:
            validas.append(descricao)

    manter = _descricao_correspondente(dados.get("manter"), lista) or padrao
    if mesmo is False and not validas:
        # A IA disse que há mistura mas não listou o que sai: tudo o que não é
        # o item que deve permanecer é sugestão de saída.
        validas = [descricao for descricao in lista if descricao != manter]
    if mesmo is None:
        mesmo = not validas
    if mesmo:
        validas = []

    sugestoes = [
        {
            "item_sugerido_sair": descricao,
            "motivo": motivo
            or "A IA identificou que esta descrição não pertence ao grupo.",
            "confianca": confianca,
        }
        for descricao in validas
        if descricao != manter
    ]
    if not mesmo and not sugestoes:
        raise ErroIA(
            "A IA indicou produtos diferentes, mas não apontou nenhuma descrição "
            "válida para sair do grupo."
        )

    return {
        "status": "sucesso",
        "modelo": modelo(),
        "descricoes": lista,
        "mesmo_grupo": mesmo,
        "manter": manter,
        "motivo": motivo,
        "confianca": confianca,
        "sugestoes": sugestoes,
    }


def _confianca(valor) -> float:
    """Confiança entre 0 e 1; aceita 0-100 e valores inválidos viram 0."""
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return 0.0
    if numero > 1.0:
        numero /= 100.0
    return max(0.0, min(1.0, numero))


def _chamar_deepseek(mensagens, temperatura: float = 0.0, max_tokens: int = 900) -> str:
    """Chama o endpoint de chat da DeepSeek e devolve o conteúdo textual.

    Mantida separada para os testes substituírem a chamada HTTP sem rede
    (`monkeypatch.setattr(ia_consenso, "_chamar_deepseek", fake)`).
    """
    chave = _api_key()
    if not chave:
        raise IAnaoconfigurada()

    url = (os.getenv("DEEPSEEK_API_URL") or URL_PADRAO).strip() or URL_PADRAO
    try:
        tempo_limite = float(os.getenv("DEEPSEEK_TIMEOUT") or TEMPO_LIMITE_PADRAO)
    except ValueError:
        tempo_limite = TEMPO_LIMITE_PADRAO

    corpo = {
        "model": modelo(),
        "messages": mensagens,
        "temperature": float(temperatura),
        "max_tokens": int(max_tokens),
        "stream": False,
        # O DeepSeek aceita response_format no modo JSON; os prompts abaixo pedem
        # explicitamente um objeto JSON, requisito do modo.
        "response_format": {"type": "json_object"},
    }
    try:
        resposta = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {chave}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=corpo,
            timeout=tempo_limite,
        )
    except requests.RequestException as erro:
        raise ErroIA(f"Não foi possível falar com a IA (DeepSeek): {erro}") from erro

    if resposta.status_code == 401:
        raise ErroIA("A IA recusou a chave configurada (DEEPSEEK_API_KEY inválida).", status=502)
    if resposta.status_code == 429:
        raise ErroIA("A IA está temporariamente sem cota (limite de requisições).", status=502)
    if resposta.status_code >= 400:
        detalhe = _limpar_espacos((resposta.text or ""))[:300]
        raise ErroIA(f"A IA respondeu {resposta.status_code}: {detalhe or 'sem detalhe'}")

    try:
        dados = resposta.json()
        conteudo = dados["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as erro:
        raise ErroIA("A resposta da IA veio sem conteúdo utilizável.") from erro
    if not _limpar_espacos(conteudo):
        raise ErroIA("A IA respondeu vazio; tente novamente.")
    return str(conteudo)


# ──────────────────────────────────────────────────────────────────────────────
# Memória de cache da extração de marca (tabela `marcas_ia`)
# ──────────────────────────────────────────────────────────────────────────────
def _incrementar(estatisticas, chave: str, quantidade: int = 1) -> None:
    """Soma um contador no dicionário de estatísticas (quando o chamador passou um)."""
    if isinstance(estatisticas, dict):
        estatisticas[chave] = int(estatisticas.get(chave, 0)) + quantidade


def _normalizar_descricao_memoria(descricao) -> str:
    """Chave do cache: mesma normalização usada na chave inteligente dos relatórios.

    Maiúsculas, sem acento, sem pontuação e sem o número de controle ("- 1", "- 2").
    Import tardio para `backend.ia_consenso` continuar independente de `backend.marcas`.
    """
    from backend import marcas as regras_marcas

    return regras_marcas.normalizar(
        regras_marcas.remover_numero_controle(descricao)
    ).strip()


def eh_sem_marca(valor) -> bool:
    """True quando o texto é o sentinela de "não há marca comercial" (tolerante)."""
    if valor is None:
        return False
    texto = _normalizar_descricao_memoria(valor).replace("_", " ")
    return texto in {
        "SEM",
        "SEM MARCA",
        "SEM GRIFE",
        "NENHUMA",
        "NENHUMA MARCA",
        "NAO IDENTIFICADA",
        "NAO IDENTIFICADO",
        "NAO INFORMADA",
        "NAO INFORMADO",
        "NAO POSSUI MARCA",
    }


def _limpar_marca_resposta(valor) -> str:
    """Normaliza o nome devolvido pela IA (sem cerca markdown, prefixo ou frase)."""
    if valor is None:
        return MARCA_SEM_MARCA
    if isinstance(valor, bool):
        return ""
    if isinstance(valor, (list, tuple)):
        for item in valor:
            marca = _limpar_marca_resposta(item)
            if marca:
                return marca
        return ""
    if isinstance(valor, dict):
        for chave in ("marca", "brand", "nome", "marca_extraida", "resultado"):
            if chave in valor:
                return _limpar_marca_resposta(valor[chave])
        return ""

    texto = _limpar_espacos(valor)
    texto = re.sub(r"^```(?:json)?\s*", "", texto, flags=re.IGNORECASE)
    texto = re.sub(r"\s*```$", "", texto).strip()
    if not texto:
        return ""
    # O modelo pode devolver "MARCA: YOKI" ou "A marca é YOKI" mesmo no modo JSON.
    texto = texto.splitlines()[0].strip().strip("\"'` ")
    texto = re.sub(r"^(?:a\s+)?marca\s+(?:e|é)\s+", "", texto, flags=re.IGNORECASE)
    texto = re.sub(r"^(?:marca|brand)\s*[:\-]\s*", "", texto, flags=re.IGNORECASE)
    texto = texto.strip(" .,;:-")
    if eh_sem_marca(texto):
        return MARCA_SEM_MARCA
    if texto.startswith(("{", "[")):
        return ""
    return _completar_marca(texto[:MAXIMO_MARCA])


def _extrair_marca_da_resposta(conteudo) -> str:
    """Lê a marca do JSON pedido no prompt e devolve o sentinela quando não houver."""
    try:
        dados = _extrair_json(conteudo)
    except ErroIA:
        dados = None

    if isinstance(dados, dict):
        if dados.get("sem_marca") is True:
            return MARCA_SEM_MARCA
        for chave in ("marca", "brand", "nome", "marca_extraida", "resultado"):
            if chave in dados:
                if dados[chave] is None:
                    return MARCA_SEM_MARCA
                marca = _limpar_marca_resposta(dados[chave])
                if marca:
                    return marca
    elif isinstance(dados, (str, list, tuple)):
        marca = _limpar_marca_resposta(dados)
        if marca:
            return marca

    # Alguns modelos ignoram o response_format e devolvem apenas o nome.
    texto = _limpar_espacos(conteudo)
    if texto.startswith(("{", "[")):
        raise ErroIA("A IA não devolveu o nome da marca em formato utilizável.")
    marca = _limpar_marca_resposta(texto)
    if marca:
        return marca
    raise ErroIA("A IA não devolveu o nome da marca.")


def _prompt_marca_inteligente(descricao: str, marca_candidata=None) -> str:
    """Prompt de extração da marca a partir da DESCRIÇÃO COMPLETA do item.

    A candidata (opcional) é a marca que a heurística local gravou (`itens_nota.marca`).
    Dar esse contexto evita que a IA descarte uma marca real pouco conhecida (LIMPOL,
    MADALENA, MARAJOARA) só porque ela não aparece nos exemplos, ao mesmo tempo em que
    rejeita palavras genéricas/cortes/embalagens (TRANSP, REFOR, OSSO, TETRAPAK).
    """
    regras = (
        "Leia a DESCRIÇÃO COMPLETA do item como ela veio na nota fiscal; nunca trate uma "
        "palavra isolada como marca sem considerar o contexto. Uma MARCA COMERCIAL é o "
        "nome próprio de fabricante, grife, marca própria ou linha comercial; não precisa "
        "ser famosa. Seja CONFIANTE: se a descrição contiver uma palavra ou sigla que "
        "claramente representa um fabricante/marca (mesmo que não esteja em nenhuma "
        "lista prévia), extraia essa marca. Não seja excessivamente conservador. "
        "Exemplos válidos: YOKI, SADIA, SEARA, PERDIGÃO, HEINZ, ARRICO, LIMPOL, YPE, "
        "MADALENA, MARAJOARA, TABASCO, ARRIFANA, IPANEMA, SELETO, DA BARRA, ASSOLAN, "
        "481, FSW, GOSTO CASEIRO, BAITA, ANDALUZIA, CAFE PELE. Marcas menos conhecidas "
        "também contam quando o contexto confirma "
        "(TOSCANO de vinagre, FINEST de batata, NOVA SAFRA de manteiga - marca composta, "
        "não só NOVA; ORIGINAL de cerveja, MARIANA de maionese, CONDE/SUPREMA de água "
        "sanitária, PRIMOR/NORDESTE de farinha, BUFALO de desinfetante, ELITE de papel "
        "toalha). NÃO são marcas: cortes de carne (CARNE, MAMINHA, ALCATRA, PICANHA, "
        "COXÃO, ACÉM), embalagens (TETRAPAK, CAIXA, CX, FD, FARDO), categorias de "
        "limpeza (ALVEJANTE, DETERGENTE), produtos in natura (ALFACE, TOMATE), "
        "tipo/estado/qualidade, sabor, origem, variedade, medida, palavra genérica ou "
        "abreviação de adjetivo (ex.: OSSO, LIXO, TRANSP, REFOR, TRAD, PAST, REF, EXTRA, "
        "COMUM, ENFRIADA, BLOCO, CONTRA, CHILENA, PALMER, FLOCOS, VIRGEM, ALVEJADO, "
        "INTER (em TOALHA INTER PLUS é interfolha), OUTROS). Abreviações de marca "
        "conhecidas podem ser completadas (SCHW = Schweppes; YPE = Ypê; FTA = Fanta), "
        "mas nunca invente uma marca a partir de fragmento ou adjetivo. Marca composta "
        "de duas palavras é uma marca só: 'SUCO VILLA PIVA' -> VILLA PIVA; "
        "'MOSTARDA CEPERA' -> CEPERA. Só retorne 'SEM MARCA' se a descrição for "
        "puramente um corte de carne, um produto in natura ou uma embalagem, sem "
        "nenhum nome de fabricante/marca. Se houver marca real (YOKI, HEINEKEN, "
        "ASSOLAN), retorne apenas o nome em MAIÚSCULAS. Se a descrição vier como "
        "'Produto Sem Nome (Marca)', extraia a marca de dentro dos parênteses."
    )
    formato = (
        " Responda SOMENTE com um objeto JSON válido no formato "
        '{"marca": "NOME DA MARCA EM MAIÚSCULAS ou SEM MARCA"}.'
    )
    descricao_json = json.dumps(
        _limpar_espacos(descricao)[:MAXIMO_DESCRICAO], ensure_ascii=False
    )
    if marca_candidata:
        candidata_json = json.dumps(
            str(marca_candidata).strip(), ensure_ascii=False
        )
        return (
            f"Analise a descrição completa {descricao_json}. A heurística anterior "
            f"sugeriu a marca candidata {candidata_json}. Confirme ou corrija. Se a "
            "candidata for uma marca comercial real, retorne o nome dela em MAIÚSCULAS, "
            "sem acrescentar palavras do produto (REF, EXTRA, TRAD); complete apenas uma "
            "abreviação conhecida da própria marca. Se a candidata NÃO for marca, "
            "procure outra marca comercial na descrição; se não houver, retorne "
            "'SEM MARCA'. "
            + regras
            + formato
        )
    return (
        f"Analise a descrição completa {descricao_json}. Identifique a MARCA COMERCIAL, "
        "se houver. Se a descrição não citar um nome comercial identificável, retorne "
        "'SEM MARCA'. " + regras + formato
    )


def _gravar_resultado_memoria(sessao, chave, resultado):
    """Grava a marca da descrição em `marcas_ia` e devolve o valor efetivo.

    O INSERT pode colidir com outra requisição que analisou a mesma descrição
    no mesmo instante (a coluna é única). Nesse caso o valor já persistido é o
    vencedor e passa a ser a resposta.
    """
    from backend.models import MarcaIA

    registro = MarcaIA(
        descricao_original=chave,
        marca_extraida=resultado,
        data_consulta=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    sessao.add(registro)
    try:
        sessao.commit()
    except IntegrityError:
        sessao.rollback()
        existente = (
            sessao.query(MarcaIA)
            .filter(MarcaIA.descricao_original == chave)
            .first()
        )
        if existente is not None:
            salva = _limpar_espacos(existente.marca_extraida)
            return (
                MARCA_SEM_MARCA
                if not salva or eh_sem_marca(salva) or _marca_invalida(salva)
                else _completar_marca(salva[:MAXIMO_MARCA])
            )
    return resultado


def obter_marca_inteligente(
    descricao,
    db=None,
    usar_ia: bool = True,
    estatisticas=None,
    marca_candidata=None,
) -> Optional[str]:
    """Marca comercial de `descricao` pela cascata cache → catálogo → DeepSeek.

    Fluxo:
      1. NÍVEL 1 — CACHE EXATO: normaliza a descrição (a mesma chave do recálculo em
         massa) e procura a linha em `marcas_ia`. Se existir, devolve o valor salvo
         sem chamar a DeepSeek. Resposta antiga com termo genérico (OSSO, TRANSP...)
         ou "SEM MARCA" para uma marca conhecida é APAGADA e tratada como cache miss;
      2. NÍVEL 2 — TOKENIZAÇÃO: quebra a descrição em palavras e procura cada uma
         (e o nome completo) no catálogo `marcas_master`. Se encontrar, grava a
         DESCRIÇÃO COMPLETA com essa marca em `marcas_ia` e devolve — sem gastar
         tokens. As respostas antigas de `marcas_ia` NÃO entram como candidatas
         (memória não é catálogo: um palpite salvo não pode virar regra);
      3. NÍVEL 3 — IA: quando nada foi encontrado e `usar_ia=True`, envia a DESCRIÇÃO
         COMPLETA no prompt, grava descrição + marca + data e devolve o resultado.
         O palpite só é aceito se estiver em `marcas_master`/`MARCAS_CONHECIDAS`
         ou aparecer na descrição; caso contrário vira ``"SEM MARCA"``;
      4. se a IA está desabilitada e a cascata não resolveu, devolve ``None`` (cache
         miss) para o recálculo cair na heurística local.

    `marca_candidata` (opcional) é a marca que a heurística já gravou. Ela só entra no
    prompt quando há cache miss e serve para a IA CONFIRMAR/CORRIGIR o candidato — útil
    na validação em massa, sem mudar a chave do cache (a resposta final é memorizada).

    O retorno ``"SEM MARCA"`` é uma resposta VÁLIDA já memorizada. Já ``None`` significa
    "não analisado agora" e permite ao recálculo cair na heurística local sem confundir
    uma descrição sem marca com uma consulta que não aconteceu. `estatisticas` (dict
    opcional) recebe contadores ``cache_hits``, ``cache_misses`` e ``consultas_ia``.
    """
    descricao_completa = _limpar_espacos(descricao)[:MAXIMO_DESCRICAO]
    chave = _normalizar_descricao_memoria(descricao)
    if not chave:
        return None

    from backend.database import SessionLocal
    from backend.models import MarcaIA

    propria_sessao = db is None
    sessao = db or SessionLocal()
    try:
        conhecida = _marca_conhecida_na_descricao(descricao_completa)
        registro = (
            sessao.query(MarcaIA)
            .filter(MarcaIA.descricao_original == chave)
            .first()
        )
        marca_token = ""
        if registro is not None:
            salva = _limpar_espacos(registro.marca_extraida)
            salva_normalizada = _normalizar_descricao_memoria(salva)
            conhecida_normalizada = _normalizar_descricao_memoria(conhecida)
            cache_invalido = (
                not salva
                or eh_sem_marca(salva)
                or _marca_invalida(salva)
                or (
                    conhecida
                    and salva_normalizada != conhecida_normalizada
                )
            )
            if not cache_invalido:
                _incrementar(estatisticas, "cache_hits")
                _incrementar(estatisticas, "marcas_encontradas")
                return _completar_marca(salva[:MAXIMO_MARCA])
            if not salva or eh_sem_marca(salva):
                # O cache pode ter sido gravado antes de a marca entrar no catálogo
                # mestre. Antes de aceitar "SEM MARCA", tenta a tokenização.
                marca_token = _marca_por_tokenizacao(descricao_completa, sessao)
                if not conhecida and not marca_token:
                    _incrementar(estatisticas, "cache_hits")
                    _incrementar(estatisticas, "sem_marca")
                    return MARCA_SEM_MARCA
            # Cache envenenado por uma resposta antiga (OSSO, TRANSP, "SEM MARCA"
            # para uma marca real...): apaga e segue como cache miss para o
            # catálogo/IA responder de novo.
            sessao.delete(registro)
            sessao.commit()

        _incrementar(estatisticas, "cache_misses")

        # NÍVEL 2 — tokenização contra o catálogo mestre e a memória da IA.
        resultado = marca_token or _marca_por_tokenizacao(descricao_completa, sessao)
        if resultado != MARCA_SEM_MARCA and _marca_invalida(resultado):
            resultado = ""
        if resultado:
            resultado = _gravar_resultado_memoria(sessao, chave, resultado)
            _incrementar(estatisticas, "marcas_encontradas")
            return resultado

        if not usar_ia:
            return conhecida or None

        # NÍVEL 3 — IA. As travas determinísticas abaixo evitam gastar tokens em
        # formatos que já têm resposta certa (VILLA PIVA, "Produto (Marca)").
        resultado = _marca_conhecida_na_descricao(descricao_completa)
        if not resultado:
            # Descrição no formato "Produto Sem Nome (Marca)": extração determinística.
            resultado = _marca_entre_parenteses(descricao_completa)
        if not resultado:
            prompt = _prompt_marca_inteligente(descricao_completa, marca_candidata)
            _incrementar(estatisticas, "consultas_ia")
            conteudo = _chamar_deepseek(
                [
                    {
                        "role": "system",
                        "content": (
                            "Você extrai marcas comerciais de descrições de produtos "
                            "brasileiros. Responda sempre em JSON válido, sem markdown."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                temperatura=0.0,
                max_tokens=120,
            )
            resultado = _extrair_marca_da_resposta(conteudo)
        # Trava final: o palpite da IA só é memorizado se for verificável (catálogo
        # mestre ou texto da descrição). Sem isso "VERDE"/"GRANDE"/grife inventada
        # entraria no cache e voltaria como acerto em todas as próximas rodadas.
        if resultado != MARCA_SEM_MARCA and not _marca_aceitavel(
            resultado, descricao_completa, sessao
        ):
            resultado = MARCA_SEM_MARCA
        resultado = _gravar_resultado_memoria(sessao, chave, resultado)
        _incrementar(
            estatisticas,
            "sem_marca" if eh_sem_marca(resultado) else "marcas_encontradas",
        )
        return resultado
    finally:
        if propria_sessao:
            sessao.close()


def _normalizar_palavras(lista_palavras: Iterable) -> List[str]:
    """Remove vazios e repetições (sem diferenciar maiúsculas), preservando a ordem."""
    palavras, vistos = [], set()
    for palavra in lista_palavras or []:
        texto = _limpar_espacos(palavra)
        if not texto:
            continue
        chave = texto.casefold()
        if chave in vistos:
            continue
        vistos.add(chave)
        palavras.append(texto)
    return palavras


def validar_marcas_com_ia(lista_palavras) -> dict:
    """Confirma com a IA quais das palavras são marcas.

    Retorna:
        {
          "status": "sucesso" | "vazio",
          "modelo": "deepseek-chat",
          "marcas": [...], "nao_marcas": [...], "nao_classificadas": [...],
          "resultados": [{"palavra", "e_marca", "confianca", "justificativa"}]
        }

    Lista vazia devolve `status="vazio"` sem chamar a API (economia de cota).
    """
    palavras = _normalizar_palavras(lista_palavras)
    if not palavras:
        return {
            "status": "vazio",
            "modelo": modelo(),
            "marcas": [],
            "nao_marcas": [],
            "nao_classificadas": [],
            "resultados": [],
        }
    if len(palavras) > MAXIMO_PALAVRAS:
        raise ErroIA(
            f"Envie no máximo {MAXIMO_PALAVRAS} palavras por consulta "
            f"(recebidas {len(palavras)}).",
            status=422,
        )

    prompt = (
        "Você é um auditor de catálogo de um supermercado brasileiro. "
        "Para CADA palavra da lista, decida se ela é uma MARCA — fabricante, grife, "
        "marca própria ou linha comercial reconhecida (ex.: YOKI, NESTLÉ, HEINZ, "
        "UNIÃO) — ou se é apenas palavra genérica de produto, tipo, sabor, "
        "embalagem ou medida (ex.: REFINADO, MANDIOCA, CAIXA, 5KG, COM, SEM). "
        "Considere o contexto do varejo brasileiro. Em caso de dúvida, prefira "
        "NÃO MARCA (e_marca=false) e confiança menor. Responda SOMENTE com um "
        "objeto JSON válido no formato: "
        '{"resultados": [{"palavra": "texto exato da lista", '
        '"e_marca": true ou false, "confianca": número de 0 a 1, '
        '"justificativa": "frase curta"}]}. '
        "Classifique todas as palavras, na mesma ordem da lista. Lista: "
        + json.dumps(palavras, ensure_ascii=False)
    )
    conteudo = _chamar_deepseek(
        [
            {
                "role": "system",
                "content": (
                    "Você responde sempre em JSON válido, sem markdown, e nunca "
                    "inventa palavras que não estejam na lista."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        temperatura=0.0,
        max_tokens=1200,
    )
    dados = _extrair_json(conteudo)
    if isinstance(dados, dict):
        itens = dados.get("resultados")
        if itens is None:
            # Alguns modelos devolvem {"YOKI": true, ...}: aceitamos esse mapa.
            itens = [
                {"palavra": palavra, "e_marca": valor}
                for palavra, valor in dados.items()
                if isinstance(valor, (bool, str, int, float))
            ]
    elif isinstance(dados, list):
        itens = dados
    else:
        itens = None
    if not isinstance(itens, list):
        raise ErroIA("A IA não devolveu a lista de resultados esperada.")

    por_palavra = {}
    for item in itens:
        if not isinstance(item, dict):
            continue
        palavra = _limpar_espacos(item.get("palavra") or item.get("word"))
        if palavra:
            por_palavra[palavra.casefold()] = item

    resultados, marcas, nao_marcas, nao_classificadas = [], [], [], []
    for palavra in palavras:
        item = por_palavra.get(palavra.casefold()) or {}
        e_marca = _booleano(item.get("e_marca", item.get("marca", item.get("is_brand"))))
        justificativa = _limpar_espacos(
            item.get("justificativa") or item.get("motivo") or item.get("reason")
        )
        if e_marca is None:
            nao_classificadas.append(palavra)
            e_marca = False
            if not justificativa:
                justificativa = "A IA não classificou esta palavra."
        elif e_marca:
            marcas.append(palavra)
        else:
            nao_marcas.append(palavra)
        resultados.append(
            {
                "palavra": palavra,
                "e_marca": e_marca,
                "confianca": _confianca(
                    item.get("confianca", item.get("confidence", 0.0))
                ),
                "justificativa": justificativa,
            }
        )

    return {
        "status": "sucesso",
        "modelo": modelo(),
        "marcas": marcas,
        "nao_marcas": nao_marcas,
        "nao_classificadas": nao_classificadas,
        "resultados": resultados,
    }


def resolver_ambiguidade_com_ia(texto1, texto2) -> dict:
    """Pergunta à IA se duas descrições são o MESMO produto.

    Retorna:
        {
          "status": "sucesso", "modelo": ..., "texto1": ..., "texto2": ...,
          "mesmo_produto": bool, "veredito": "iguais" | "diferentes",
          "confianca": 0..1, "motivo": "...", "diferencas": [...]
        }

    A decisão é só uma sugestão: nenhuma regra de agrupamento é gravada aqui.
    """
    descricao_1 = remover_numero_controle(texto1)
    descricao_2 = remover_numero_controle(texto2)
    if not descricao_1 or not descricao_2:
        raise ErroIA("Informe as duas descrições para a IA comparar.", status=422)
    if len(descricao_1) > MAXIMO_DESCRICAO or len(descricao_2) > MAXIMO_DESCRICAO:
        raise ErroIA(
            f"Cada descrição deve ter no máximo {MAXIMO_DESCRICAO} caracteres.",
            status=422,
        )

    prompt = (
        "Você audita descrições de itens de nota fiscal de um supermercado "
        "brasileiro. Compare as duas descrições e diga se elas representam o "
        "MESMO produto vendável. Considere o mesmo produto quando mudam apenas "
        "ordem das palavras, abreviação, grafia, acentos, código interno "
        "do fornecedor ou a embalagem comercial (CX 12, FD, PCT, DZ), desde que "
        "o conteúdo e a unidade de venda sejam equivalentes e a marca seja a "
        "mesma (ou não esteja identificada). Considere produtos "
        "DIFERENTES quando mudarem o tipo/produto, sabor, medida/peso/volume, "
        "quantidade de unidades, ou quando uma descrição disser COM e a outra "
        "SEM (ex.: com osso x sem osso), ou quando as marcas/fabricantes forem "
        "diferentes (ex.: refrigerante COCA x refrigerante PEPSI, QUEIJO MINAS "
        "CAMANDUCAIA x QUEIJO MINAS IPANEMA). A marca é "
        "parte da identidade do produto e é critério de separação. Não invente "
        "dados. Responda SOMENTE com um objeto JSON válido no formato: "
        '{"mesmo_produto": true ou false, "confianca": número de 0 a 1, '
        '"motivo": "explicação curta", "diferencas": ["diferença 1", "..."]}. '
        f"Descrição 1: {json.dumps(descricao_1, ensure_ascii=False)}. "
        f"Descrição 2: {json.dumps(descricao_2, ensure_ascii=False)}."
    )
    conteudo = _chamar_deepseek(
        [
            {
                "role": "system",
                "content": (
                    "Você é um auditor de catálogo rigoroso e responde sempre em "
                    "JSON válido, sem markdown."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        temperatura=0.0,
        max_tokens=500,
    )
    dados = _extrair_json(conteudo)
    if not isinstance(dados, dict):
        raise ErroIA("A IA não devolveu um veredito em formato de objeto JSON.")

    mesmo = _booleano(
        dados.get(
            "mesmo_produto",
            dados.get("iguais", dados.get("same_product", dados.get("veredito"))),
        )
    )
    if mesmo is None and isinstance(dados.get("veredito"), str):
        veredito_texto = _limpar_espacos(dados["veredito"]).lower()
        if veredito_texto.startswith(("igual", "mesmo", "same")):
            mesmo = True
        elif veredito_texto.startswith(("diferente", "distinto", "different")):
            mesmo = False
    if mesmo is None:
        raise ErroIA("A IA não informou se os produtos são iguais ou diferentes.")

    diferencas = dados.get("diferencas", dados.get("diferenças")) or []
    if isinstance(diferencas, str):
        diferencas = [diferencas]
    if not isinstance(diferencas, list):
        diferencas = []
    diferencas = [_limpar_espacos(item) for item in diferencas if _limpar_espacos(item)]

    return {
        "status": "sucesso",
        "modelo": modelo(),
        "texto1": descricao_1,
        "texto2": descricao_2,
        "mesmo_produto": mesmo,
        "veredito": "iguais" if mesmo else "diferentes",
        "confianca": _confianca(dados.get("confianca", dados.get("confidence", 0.0))),
        "motivo": _limpar_espacos(dados.get("motivo") or dados.get("justificativa")),
        "diferencas": diferencas,
    }


MAXIMO_DESCRICOES_GRUPO = 60


def _descricao_correspondente(valor, descricoes):
    """Mapeia um texto devolvido pela IA para a descrição EXATA do grupo.

    A IA às vezes muda caixa/espaços. Nunca aceitamos uma descrição inventada:
    se não houver correspondência, devolvemos ``None`` para a sugestão ser
    descartada (o auditor não pode aplicar um item que não existe no grupo).
    """
    alvo = _limpar_espacos(valor)
    if not alvo:
        return None
    for descricao in descricoes:
        if _limpar_espacos(descricao) == alvo:
            return descricao
    alvo_casefold = alvo.casefold()
    for descricao in descricoes:
        if _limpar_espacos(descricao).casefold() == alvo_casefold:
            return descricao
    # A descrição enviada já vem sem o número de controle; a padrão (e a resposta da
    # IA) ainda podem citá-lo. A equivalência mantém o mapeamento determinístico.
    alvo_equivalente = chave_equivalencia_descricao(alvo)
    for descricao in descricoes:
        if chave_equivalencia_descricao(descricao) == alvo_equivalente:
            return descricao
    return None
