"""Consulta de CNPJ e CEP na Receita/Correios com troca de provedor.

Motivo: a BrasilAPI (fonte principal do cadastro de loja) responde HTTP 429 com
frequência em horário de pico. Antes a consulta era repetida no MESMO serviço e, se o
limite continuasse, a loja era salva sem endereço — a ACBr recusava depois com
"Complete o cadastro da empresa". Agora, em 429/5xx/timeout, a consulta passa para o
próximo provedor da lista em vez de insistir no mesmo:

1. ``brasilapi``   — brasilapi.com.br/api/cnpj/v1/{cnpj}
2. ``minhareceita``— minhareceita.org/{cnpj}
3. ``cnpjws``      — publica.cnpj.ws/cnpj/{cnpj}

Nenhum deles exige chave de API. O retorno é SEMPRE normalizado nos campos que o
cadastro/ACBr usam, então quem chama não precisa saber de qual serviço veio o dado
(``provedor`` fica registrado só para log/diagnóstico; ``fonte`` continua
``brasilapi``/``indisponivel`` por compatibilidade com o restante da API).

Para o CEP a ordem é ViaCEP -> BrasilAPI CEP, usada pelo formulário "Minhas Lojas"
quando o cliente digita o CEP e a Receita não devolveu o endereço.

Resultado bom fica em cache de memória (por processo) por
``NFE_CACHE_CADASTRAL_HORAS`` (padrão 720h = 30 dias). Falha NUNCA é cacheada: o
próximo clique tenta de novo. ``NFE_ESPERA_ENTRE_PROVEDORES=0`` deixa os testes
instantâneos.
"""

import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

# Ordem de fallback do CNPJ. Quem chamar pode passar outra ordem em `provedores`.
PROVEDORES_CNPJ = ("brasilapi", "minhareceita", "cnpjws")

URL_CNPJ = {
    "brasilapi": "https://brasilapi.com.br/api/cnpj/v1/{cnpj}",
    "minhareceita": "https://minhareceita.org/{cnpj}",
    "cnpjws": "https://publica.cnpj.ws/cnpj/{cnpj}",
}

URL_CEP = {
    "viacep": "https://viacep.com.br/ws/{cep}/json/",
    "brasilapi": "https://brasilapi.com.br/api/cep/v2/{cep}",
}
PROVEDORES_CEP = ("viacep", "brasilapi")

# Status em que vale a pena TROCAR de provedor (limite de requisições e
# instabilidade). 404 significa CNPJ inexistente/baixado: nenhum outro provedor
# vai encontrar, então a consulta para ali.
STATUS_QUE_TROCAM_DE_PROVEDOR = {429, 500, 502, 503, 504}

TIMEOUT_CONSULTA = 12
# Pausa curta antes de tentar o próximo provedor (evita rajada em todos ao mesmo
# tempo). `NFE_ESPERA_ENTRE_PROVEDORES=0` = sem espera (testes).
ESPERA_ENTRE_PROVEDORES = float(os.getenv("NFE_ESPERA_ENTRE_PROVEDORES", "1") or 0)
# TTL do cache de memória. 0 desliga o cache (usado em teste que repete a chamada).
CACHE_HORAS = float(os.getenv("NFE_CACHE_CADASTRAL_HORAS", "720") or 0)

# cnpj/cep -> (expira_em, dados). `dados` já normalizado (nunca é cópia do JSON cru).
_CACHE_CNPJ = {}
_CACHE_CEP = {}

# Cabeçalho mínimo: a publica.cnpj.ws recusa requisições sem User-Agent.
_CABECALHOS = {
    "Accept": "application/json",
    "User-Agent": "InteligenciaDeCustos-NFe/1.0 (+cadastro de lojas)",
}


def somente_digitos(valor) -> str:
    """Remove tudo que não é dígito (CNPJ/CEP podem vir com pontuação)."""
    return re.sub(r"\D", "", str(valor or ""))


def _agora() -> datetime:
    return datetime.now(timezone.utc)


def _ler_cache(cache: dict, chave: str):
    """Dados em cache ainda válidos (ou None quando expirou/está desligado)."""
    if CACHE_HORAS <= 0:
        return None
    registro = cache.get(chave)
    if not registro:
        return None
    expira_em, dados = registro
    if expira_em <= _agora():
        cache.pop(chave, None)
        return None
    return dados


def _gravar_cache(cache: dict, chave: str, dados: dict) -> None:
    if CACHE_HORAS <= 0 or not dados:
        return
    cache[chave] = (_agora() + timedelta(hours=CACHE_HORAS), dados)


def limpar_cache() -> None:
    """Esvazia os caches de memória (testes e recarga manual do cadastro)."""
    _CACHE_CNPJ.clear()
    _CACHE_CEP.clear()


def _json_da_resposta(resposta):
    """JSON da resposta (dict) ou None quando o corpo não é um objeto JSON."""
    try:
        dados = resposta.json()
    except ValueError:
        return None
    return dados if isinstance(dados, dict) else None


def _texto(valor) -> str:
    return str(valor or "").strip()


def _primeiro(dados: dict, *chaves, padrao=""):
    """Primeiro valor não vazio entre as chaves (cada provedor usa um nome)."""
    for chave in chaves:
        valor = dados.get(chave)
        if valor not in (None, "", []):
            return valor
    return padrao


def _normalizar_cnpj(dados: dict, provedor: str) -> dict:
    """Traduz a resposta de QUALQUER provedor para os campos do cadastro.

    A publica.cnpj.ws aninha os dados da filial em ``estabelecimento`` (a matriz e
    as filiais vêm separadas); a BrasilAPI e a minhareceita devolvem tudo no nível
    de cima. O logradouro é montado com o tipo da via porque a ACBr espera o
    endereço completo em um único campo (``RUA JOSE FELIX...``).
    """
    dados = dados or {}
    filial = dados.get("estabelecimento")
    if isinstance(filial, dict):
        # cnpj.ws: "estado": {"sigla": "SP"}, "cidade": {"ibge_id": ...}
        estado = filial.get("estado")
        if isinstance(estado, dict):
            filial = {**filial, "uf": estado.get("sigla")}
        cidade = filial.get("cidade")
        if isinstance(cidade, dict):
            filial = {
                **filial,
                "municipio": cidade.get("nome") or filial.get("municipio"),
                "codigo_municipio_ibge": cidade.get("ibge_id")
                or filial.get("codigo_municipio_ibge"),
            }
        dados = {**dados, **filial}

    tipo_logradouro = _texto(
        _primeiro(dados, "descricao_tipo_de_logradouro", "tipo_logradouro")
    )
    nome_logradouro = _texto(_primeiro(dados, "logradouro"))
    logradouro = " ".join(
        parte for parte in (tipo_logradouro, nome_logradouro) if parte
    )
    telefone = _texto(_primeiro(dados, "ddd_telefone_1", "telefone_1", "telefone"))
    ddd = _texto(_primeiro(dados, "ddd1", "ddd_telefone_1"))
    if telefone and ddd and not telefone.startswith(ddd):
        telefone = f"{ddd}{telefone}"

    return {
        "razao_social": _texto(_primeiro(dados, "razao_social")),
        "nome_loja": _texto(_primeiro(dados, "nome_fantasia", "razao_social")),
        "municipio": _texto(_primeiro(dados, "municipio", "cidade")),
        "cep": somente_digitos(_primeiro(dados, "cep")),
        "uf": _texto(_primeiro(dados, "uf", "estado")).upper(),
        "codigo_municipio": somente_digitos(
            _primeiro(
                dados,
                "codigo_municipio_ibge",
                "codigo_municipio",
                "municipio_ibge",
                "ibge_id",
            )
        ),
        "bairro": _texto(_primeiro(dados, "bairro")),
        "logradouro": logradouro,
        "numero": _texto(_primeiro(dados, "numero")),
        "complemento": _texto(_primeiro(dados, "complemento")),
        "email": _texto(_primeiro(dados, "email")),
        "telefone": telefone,
        # `fonte`/`provedor` ficam fora do payload da ACBr: são só diagnóstico.
        "fonte": "brasilapi",
        "provedor": provedor,
    }


def consultar_cnpj(cnpj, provedores=None, timeout: int = TIMEOUT_CONSULTA):
    """Dados cadastrais do CNPJ tentando os provedores na ordem, ou None.

    Um provedor só é abandonado em 429/5xx/timeout/JSON inválido — nesses casos a
    ``ESPERA_ENTRE_PROVEDORES`` é respeitada antes do próximo. 404 (CNPJ
    inexistente) encerra a consulta: trocar de serviço não mudaria a resposta.
    """
    digitos = somente_digitos(cnpj)
    if len(digitos) != 14:
        return None
    em_cache = _ler_cache(_CACHE_CNPJ, digitos)
    if em_cache:
        return dict(em_cache)

    ordem = tuple(provedores or PROVEDORES_CNPJ)
    for indice, provedor in enumerate(ordem):
        url = URL_CNPJ[provedor].format(cnpj=digitos)
        resposta = None
        try:
            resposta = requests.get(url, timeout=timeout, headers=_CABECALHOS)
        except Exception as erro:
            logger.warning(
                "Consulta CNPJ %s: provedor %s falhou (%s: %s)",
                digitos,
                provedor,
                type(erro).__name__,
                erro,
            )
        else:
            if resposta.status_code == 200:
                dados = _json_da_resposta(resposta)
                if dados is None:
                    logger.warning(
                        "Consulta CNPJ %s: %s respondeu HTTP 200 sem JSON válido.",
                        digitos,
                        provedor,
                    )
                else:
                    normalizado = _normalizar_cnpj(dados, provedor)
                    if normalizado["nome_loja"]:
                        _gravar_cache(_CACHE_CNPJ, digitos, normalizado)
                        if provedor != ordem[0]:
                            logger.info(
                                "Consulta CNPJ %s atendida pelo provedor reserva %s.",
                                digitos,
                                provedor,
                            )
                        return dict(normalizado)
                    logger.warning(
                        "Consulta CNPJ %s: %s devolveu JSON sem razão social.",
                        digitos,
                        provedor,
                    )
            elif resposta.status_code == 404:
                # CNPJ não existe (ou foi baixado): nenhum provedor vai achar.
                logger.warning(
                    "Consulta CNPJ %s: %s respondeu HTTP 404 (CNPJ não encontrado).",
                    digitos,
                    provedor,
                )
                return None
            else:
                trecho = " ".join(
                    str(getattr(resposta, "text", "") or "").split()
                )[:160]
                logger.warning(
                    "Consulta CNPJ %s: %s respondeu HTTP %s%s",
                    digitos,
                    provedor,
                    resposta.status_code,
                    f" ({trecho})" if trecho else "",
                )
                if resposta.status_code not in STATUS_QUE_TROCAM_DE_PROVEDOR:
                    return None
        # Vai para o próximo provedor: pausa curta (respeita Retry-After quando há).
        if indice < len(ordem) - 1 and ESPERA_ENTRE_PROVEDORES > 0:
            espera = ESPERA_ENTRE_PROVEDORES
            cabecalho = ""
            try:
                cabecalho = str(
                    (getattr(resposta, "headers", None) or {}).get("Retry-After") or ""
                )
            except Exception:
                cabecalho = ""
            if cabecalho.strip().isdigit():
                espera = max(espera, float(cabecalho.strip()))
            time.sleep(max(0.0, min(espera, 10.0)))

    logger.warning(
        "Consulta CNPJ %s: nenhum provedor respondeu (%s). O cadastro segue sem os "
        "dados da Receita (endereço pode ser informado na tela Minhas Lojas).",
        digitos,
        ", ".join(ordem),
    )
    return None


def _normalizar_cep(dados: dict, provedor: str) -> dict:
    """Resposta do ViaCEP/BrasilAPI CEP nos nomes do formulário da loja."""
    dados = dados or {}
    return {
        "cep": somente_digitos(_primeiro(dados, "cep")),
        "logradouro": _texto(_primeiro(dados, "logradouro", "street")),
        "bairro": _texto(_primeiro(dados, "bairro", "neighborhood")),
        "cidade": _texto(_primeiro(dados, "localidade", "city")),
        "uf": _texto(_primeiro(dados, "uf", "state")).upper(),
        # ViaCEP devolve `ibge`; a BrasilAPI CEP não traz o código do município.
        "codigo_municipio": somente_digitos(_primeiro(dados, "ibge")),
        "fonte": provedor,
    }


def consultar_cep(cep, provedores=None, timeout: int = TIMEOUT_CONSULTA):
    """Endereço do CEP (ViaCEP -> BrasilAPI) ou None quando não foi encontrado.

    ``erro`` no corpo do ViaCEP (CEP inexistente) encerra a consulta sem tentar o
    próximo — é resposta definitiva dos Correios, não instabilidade.
    """
    digitos = somente_digitos(cep)
    if len(digitos) != 8:
        return None
    em_cache = _ler_cache(_CACHE_CEP, digitos)
    if em_cache:
        return dict(em_cache)

    ordem = tuple(provedores or PROVEDORES_CEP)
    for indice, provedor in enumerate(ordem):
        url = URL_CEP[provedor].format(cep=digitos)
        try:
            resposta = requests.get(url, timeout=timeout, headers=_CABECALHOS)
        except Exception as erro:
            logger.warning(
                "Consulta CEP %s: provedor %s falhou (%s: %s)",
                digitos,
                provedor,
                type(erro).__name__,
                erro,
            )
        else:
            if resposta.status_code == 200:
                dados = _json_da_resposta(resposta)
                if dados is None:
                    logger.warning(
                        "Consulta CEP %s: %s respondeu HTTP 200 sem JSON válido.",
                        digitos,
                        provedor,
                    )
                elif dados.get("erro") is True or dados.get("erro") == "true":
                    logger.info(
                        "Consulta CEP %s: %s informou CEP inexistente.",
                        digitos,
                        provedor,
                    )
                    return None
                else:
                    normalizado = _normalizar_cep(dados, provedor)
                    if normalizado["logradouro"] or normalizado["cidade"]:
                        _gravar_cache(_CACHE_CEP, digitos, normalizado)
                        return dict(normalizado)
            elif resposta.status_code == 404:
                logger.info("Consulta CEP %s: %s respondeu HTTP 404.", digitos, provedor)
                return None
            else:
                logger.warning(
                    "Consulta CEP %s: %s respondeu HTTP %s.",
                    digitos,
                    provedor,
                    resposta.status_code,
                )
                if resposta.status_code not in STATUS_QUE_TROCAM_DE_PROVEDOR:
                    return None
        if indice < len(ordem) - 1 and ESPERA_ENTRE_PROVEDORES > 0:
            time.sleep(min(ESPERA_ENTRE_PROVEDORES, 10.0))

    logger.warning(
        "Consulta CEP %s: nenhum provedor respondeu (%s).", digitos, ", ".join(ordem)
    )
    return None


