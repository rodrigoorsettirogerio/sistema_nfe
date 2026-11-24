"""Cliente HTTP da API do Omie para notas fiscais de entrada (compras).

A API do Omie não usa OAuth nem token: **toda** chamada é um ``POST`` JSON no
formato::

    {"call": "NomeDoMetodo", "app_key": "...", "app_secret": "...",
     "param": [{...}]}

Por isso o cliente guarda ``app_key``/``app_secret`` (lidos do ``.env``) e
reenvia o envelope em cada método. Os segredos nunca entram em log nem nas
mensagens de erro.

A fase 1 da integração usa dois métodos:

- ``ListarRecebimentos`` (``/api/v1/produtos/recebimentonfe/``): relação das
  notas de entrada já lançadas no Omie. O filtro é por **data de emissão**
  (``dtEmissaoDe``/``dtEmissaoAte``, no formato ``dd/mm/aaaa``) e
  ``cExibirDetalhes="S"`` inclui itens, parcelas, departamentos, categorias e
  infoCadastro — sem isso a listagem traz só o cabeçalho, o que impediria
  conferir a nota com o XML baixado. ``cEtapa`` filtra a coluna do kanban
  (40=Faturado, 60=Recebido, 80=Conferido) e as datas são **opcionais**: sem
  elas a listagem traz o histórico inteiro da etapa, que é o recorte usado na
  importação inicial da fase 2;
- ``ListarDocumentos`` (``/api/v1/contador/xml/``): XML completo da NF-e. Os
  campos ``cModelo="55"`` e ``cOperacao="0"`` são **obrigatórios**: ``55``
  restringe o resultado à NF-e (fora CT-e/NFS-e) e ``cOperacao`` tem default
  ``"1"`` (Saída) no Omie, então sem ele viriam as notas **emitidas** pela
  empresa em vez das **compras**. O XML integral vem no campo ``cXml``.
  ``cAmbiente`` é omitido de propósito: vale o default ``P`` (produção), onde
  estão as compras reais — notas de homologação (``H``) ficam fora da fase 1.

Tratamento de erro em dois níveis (só ``resposta.ok`` do ``requests`` não
basta, porque a Omie sinaliza falha de negócio com HTTP 200):

1. HTTP: status fora da faixa 2xx vira :class:`ErroOmie` com o corpo preservado;
2. aplicação: com HTTP 200, o corpo pode trazer ``{"omie_fail": {...}}`` ou
   ``cCodStatus`` diferente de ``"0"`` (o motivo fica em
   ``cDescStatus``/``cDesStatus``). Os dois formatos são convertidos em
   :class:`ErroOmie` — sem essa checagem uma recusa seria tratada como página
   vazia e o usuário veria "nenhuma nota" em vez do motivo real.

Variáveis de ambiente (lidas do ``.env`` da raiz do projeto):

- ``OMIE_APP_KEY`` (obrigatória): chave do aplicativo criado no Omie;
- ``OMIE_APP_SECRET`` (obrigatória): segredo do aplicativo;
- ``OMIE_TIMEOUT`` (opcional): segundos por chamada, padrão 60.

O módulo é deliberadamente isolado: não importa banco nem ``models`` para poder
ser exercitado por script (``testar_omie.py``) antes da fase 2 (importação).
"""

from __future__ import annotations

import html
import logging
import os
import re
import time
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import requests
from dotenv import load_dotenv

# Carrega o .env da raiz do projeto ANTES de qualquer leitura de OMIE_*.
# `override=False` preserva variáveis injetadas pelo serviço em produção.
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

URL_RECEBIMENTOS = "https://app.omie.com.br/api/v1/produtos/recebimentonfe/"
URL_DOCUMENTOS = "https://app.omie.com.br/api/v1/contador/xml/"
TEMPO_LIMITE_PADRAO = 60
# O `ListarRecebimentos` documenta "até 100; um valor acima de 100 passa a
# 100". Usar 50 reduz o tamanho de cada resposta (que vem com itens/parcelas
# por causa do `cExibirDetalhes="S"`) sem multiplicar as chamadas.
REGISTROS_POR_PAGINA_PADRAO = 50
MAXIMO_REGISTROS_POR_PAGINA = 100
# Teto de segurança para a varredura de páginas: evita laço infinito caso a
# Omie devolva sempre a mesma página (bug/proxy) e mantém o teste previsível.
MAXIMO_PAGINAS_PADRAO = 100
# Modelo 55 = NF-e. `cOperacao` 0 = Entrada (compras); 1 = Saída (default).
MODELO_NFE = "55"
OPERACAO_ENTRADA = "0"

logger = logging.getLogger("sistema_nfe")

# Data aceita pelas APIs do Omie: objeto `date`/`datetime` ou texto.
DataOmie = Union[date, datetime, str]

# A chave de acesso aparece no atributo `Id="NFe<44 dígitos>"` da `<infNFe>` e,
# em documentos resumidos/protocolados, na tag `<chNFe>`. A regex cobre os dois
# casos sem depender de namespaces (o XML da Omie pode vir como `nfeProc`).
PADRAO_CHAVE_NFE = re.compile(
    r"(?i)(?:Id\s*=\s*[\"']\s*NFe|<chNFe>)\s*(\d{44})"
)


class ErroOmie(RuntimeError):
    """Falha controlada na comunicação com a API do Omie.

    ``status`` é o código HTTP devolvido (normalmente ``200``, porque a Omie
    sinaliza erro de negócio com sucesso HTTP) e ``codigo`` é o código do Omie
    (``cCodStatus`` ou ``omie_fail.code``), que identifica o motivo real da
    recusa. ``corpo`` preserva a resposta para o script/teste exibir.
    """

    def __init__(
        self,
        mensagem: str,
        status: Optional[int] = None,
        corpo: Optional[Any] = None,
        codigo: Optional[str] = None,
    ):
        super().__init__(mensagem)
        self.status = status
        self.corpo = corpo
        self.codigo = codigo


class OmieNaoConfigurado(ErroOmie):
    """Credenciais ausentes no ambiente: a integração está desligada."""

    def __init__(self, mensagem: Optional[str] = None):
        super().__init__(
            mensagem
            or (
                "API do Omie não configurada: defina OMIE_APP_KEY e "
                "OMIE_APP_SECRET no .env e execute novamente."
            )
        )


class OmieSemRegistros(ErroOmie):
    """A Omie respondeu HTTP 500 "Não existem registros para a página".

    A API não devolve lista vazia quando a etapa/período não tem notas: o
    gateway responde erro 500 com essa mensagem. Para a sincronização isso
    significa "nenhum dado naquele recorte" — não é falha de credencial, de
    rede nem de contrato, então as listagens traduzem a exceção em ``[]`` e o
    agendador não marca a loja como com erro.
    """


def _sem_acento(texto: Any) -> str:
    """Texto em minúsculas e sem acentos, para comparar mensagens do Omie.

    A mensagem pode chegar como ``"Não existem registros"`` ou
    ``"Nao existem registros"`` (gateway/SO diferentes); normalizar evita que
    o 500 de "sem dados" seja tratado como erro fatal em um dos formatos.
    """
    decomposto = unicodedata.normalize("NFKD", str(texto or ""))
    return "".join(
        caractere for caractere in decomposto if not unicodedata.combining(caractere)
    ).lower()


def _pagina_sem_registros(status: Any, corpo: Any) -> bool:
    """True quando o erro HTTP é o 500 de "página sem registros" do Omie."""
    if _inteiro(status) != 500:
        return False
    return "nao existem registros" in _sem_acento(_detalhe_erro(corpo))


def sem_registros(erro: Any) -> bool:
    """True quando a exceção representa apenas "não há dados" no Omie.

    A checagem olha o status 500 **e** a mensagem, porque a Omie usa 500 para
    outros problemas também. É usada pelo agendador (uma loja sem notas no
    período não pode virar erro no log) e fica exposta para os scripts de
    diagnóstico usarem o mesmo critério.
    """
    if not isinstance(erro, ErroOmie):
        return False
    if _inteiro(getattr(erro, "status", None)) != 500:
        return False
    if isinstance(erro, OmieSemRegistros):
        return True
    return "nao existem registros" in _sem_acento(str(erro))


def _valor_ambiente(nome: str, padrao: str = "") -> str:
    """Lê uma variável e remove espaços acidentais nas pontas."""
    return (os.getenv(nome) or padrao).strip()


def _json_ou_texto(resposta: requests.Response) -> Any:
    """Tenta JSON; se a Omie devolver HTML/texto, preserva o texto do erro."""
    try:
        return resposta.json()
    except ValueError:
        return resposta.text


def _texto(valor: Any) -> str:
    """Converte um campo da Omie em texto aparado (``None`` vira ``""``)."""
    if valor is None:
        return ""
    return str(valor).strip()


def _inteiro(valor: Any, padrao: int = 0) -> int:
    """Converte números que a Omie devolve como texto/int/float sem estourar."""
    try:
        return int(float(_texto(valor)))
    except (TypeError, ValueError):
        return padrao


def _normalizar_data(valor: DataOmie, nome: str = "data") -> date:
    """Converte a data recebida do chamador em ``date``.

    A Omie só entende ``dd/mm/aaaa``, mas aceitar ``date``, ``datetime`` e ISO
    (``aaaa-mm-dd``) deixa o cliente confortável para a fase 2, quando as datas
    virão do banco/front — sem obrigar cada chamador a formatar manualmente.
    """
    if isinstance(valor, datetime):
        return valor.date()
    if isinstance(valor, date):
        return valor
    texto = _texto(valor)
    if not texto:
        raise ValueError(f"Informe a {nome} no formato dd/mm/aaaa.")
    for formato in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(texto, formato).date()
        except ValueError:
            continue
    raise ValueError(
        f"{nome.capitalize()} inválida: use dd/mm/aaaa (recebido: {texto!r})."
    )


def _periodo(data_inicio: DataOmie, data_fim: DataOmie) -> Tuple[date, date]:
    """Normaliza e valida o período antes de qualquer chamada à Omie.

    Validar aqui evita gastar uma requisição para receber da Omie um erro que
    já poderia ser detectado localmente.
    """
    inicio = _normalizar_data(data_inicio, "data inicial")
    fim = _normalizar_data(data_fim, "data final")
    if inicio > fim:
        raise ValueError("A data inicial não pode ser posterior à data final.")
    return inicio, fim


def _periodo_opcional(
    data_inicio: Optional[DataOmie], data_fim: Optional[DataOmie]
) -> Tuple[Optional[date], Optional[date]]:
    """Período opcional: as duas datas ausentes significam "sem filtro de data".

    A ``ListarRecebimentos`` aceita ser chamada só com ``cEtapa`` (foi assim que a
    fase 2 confirmou 40=Faturado, 60=Recebido e 80=Conferido: 743/158/990 notas
    no histórico). A sincronização usa essa forma quando o operador quer a
    importação inicial completa; informar só uma das pontas continua sendo erro,
    porque um período meio aberto devolveria um recorte silencioso.
    """
    vazia_inicio = data_inicio in (None, "")
    vazia_fim = data_fim in (None, "")
    if vazia_inicio and vazia_fim:
        return None, None
    if vazia_inicio or vazia_fim:
        raise ValueError(
            "Informe as duas datas (início e fim) ou nenhuma — sem datas a "
            "listagem traz o histórico inteiro."
        )
    return _periodo(data_inicio, data_fim)


def _paginacao(pagina: Any, registros_por_pagina: Any) -> Tuple[int, int]:
    """Valida a paginação e limita o lote ao teto de 100 registros.

    O teto é o mesmo que o ``ListarRecebimentos`` aplica internamente
    ("um valor acima de 100 passa a 100"); limitar aqui deixa explícito o que
    a Omie faria em silêncio e protege o ``ListarDocumentos``, cujo XML é
    grande e não documenta máximo.
    """
    try:
        pagina_numero = int(pagina)
        por_pagina = int(registros_por_pagina)
    except (TypeError, ValueError) as erro:
        raise ValueError(
            "A página e os registros por página devem ser números inteiros."
        ) from erro
    if pagina_numero < 1:
        raise ValueError("A página deve ser maior ou igual a 1.")
    if por_pagina < 1:
        raise ValueError("Informe pelo menos 1 registro por página.")
    return pagina_numero, min(por_pagina, MAXIMO_REGISTROS_POR_PAGINA)


def _status_sucesso(valor: Any) -> bool:
    """True quando o código de status do Omie representa sucesso.

    O Omie devolve ``cCodStatus`` ora como inteiro, ora como texto. Considerar
    apenas ``"0"`` faria ``0`` (int) passar como erro; já texto vazio ou só com
    zeros (``"0"``, ``"0000"``) é sucesso em todas as versões do serviço.
    """
    texto = _texto(valor)
    return all(caractere == "0" for caractere in texto)


def _mensagem_omie_fail(omie_fail: Any) -> Tuple[str, str]:
    """Extrai código e mensagem do objeto ``omie_fail``.

    Formato documentado: ``{"code": int, "description": texto,
    "referer": texto, "fatal": bool}``. Versões do gateway também já
    devolveram o objeto como texto puro, por isso o fallback.
    """
    if isinstance(omie_fail, dict):
        codigo = _texto(omie_fail.get("code") or omie_fail.get("cCodStatus"))
        mensagem = _texto(
            omie_fail.get("description")
            or omie_fail.get("cDescStatus")
            or omie_fail.get("message")
        )
        origem = _texto(omie_fail.get("referer"))
        if origem:
            mensagem = (
                f"{mensagem} (origem: {origem})"
                if mensagem
                else f"Origem: {origem}"
            )
        if omie_fail.get("fatal") is True:
            mensagem = (
                f"{mensagem} [erro fatal]" if mensagem else "Erro fatal."
            )
        return codigo, mensagem or "O Omie não detalhou o motivo da falha."
    return "", _texto(omie_fail) or "O Omie não detalhou o motivo da falha."


def _detalhe_erro(corpo: Any) -> str:
    """Extrai a mensagem mais útil de um corpo de erro HTTP/Omie."""
    if isinstance(corpo, dict):
        if "omie_fail" in corpo:
            _codigo, mensagem = _mensagem_omie_fail(corpo.get("omie_fail"))
            if mensagem:
                return mensagem
        for chave in (
            "cDescStatus",
            "cDesStatus",
            "faultstring",
            "mensagem",
            "message",
            "error_description",
            "description",
            "error",
        ):
            valor = corpo.get(chave)
            if isinstance(valor, (dict, list)):
                continue
            if valor:
                return str(valor)
        return str(corpo)
    return str(corpo or "").strip()


def _erro_aplicacao(corpo: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    """Detecta o erro de negócio que o Omie devolve com HTTP 200.

    A presença de ``omie_fail`` já indica falha (o objeto só existe nesse
    cenário). ``cCodStatus`` só é erro quando difere de ``"0"``; nesse caso o
    motivo vem em ``cDescStatus`` (grafia da documentação) ou ``cDesStatus``
    (grafia do envelope SOAP). O ``faultstring`` cobre falhas de gateway que
    chegam sem ``cCodStatus``.
    """
    if "omie_fail" in corpo and corpo.get("omie_fail") is not None:
        return _mensagem_omie_fail(corpo.get("omie_fail"))

    status = corpo.get("cCodStatus")
    if status is not None and not _status_sucesso(status):
        mensagem = _texto(
            corpo.get("cDescStatus")
            or corpo.get("cDesStatus")
            or corpo.get("faultstring")
        )
        return _texto(status), mensagem or "O Omie recusou a chamada."

    faultstring = _texto(corpo.get("faultstring"))
    if faultstring and status is None:
        return _texto(corpo.get("faultcode")), faultstring
    return None


def _lista_de(
    corpo: Dict[str, Any], chave: str, call: str
) -> List[Dict[str, Any]]:
    """Devolve a lista de itens da resposta, ignorando entradas não-dicionário.

    Quando o filtro não encontra nada, a Omie pode omitir a chave ou devolver
    lista vazia; nos dois casos o resultado correto é ``[]``. Se o campo vier
    com outro tipo, é sinal de resposta fora do contrato e o erro é explícito.
    """
    itens = corpo.get(chave)
    if itens is None:
        return []
    if not isinstance(itens, list):
        raise ErroOmie(
            f"A API do Omie devolveu {chave!r} fora do formato esperado "
            f"em {call}.",
            corpo=corpo,
        )
    return [item for item in itens if isinstance(item, dict)]


def chave_do_xml(xml: Any) -> str:
    """Chave de acesso (44 dígitos) contida no XML da NF-e, ou ``""``.

    A gravação dos arquivos segue o padrão do projeto (``<chave>.xml``) para
    que a fase 2 consiga localizar/deduplicar a nota. A leitura é por regex
    (e não por ``ElementTree``) porque o XML vem como texto da Omie, pode ter
    declaração de encoding e namespaces, e a chave está sempre em ``infNFe``
    ou ``chNFe`` — a regex funciona nos dois formatos sem parsear o arquivo
    inteiro.

    O ``cXml``/``cXmlNfe`` da Omie chega **HTML-escapado**
    (``"&lt;?xml version=&quot;1.0&quot;..."``), então o ``html.unescape``
    roda antes do regex: sem ele o texto não tem ``<``/``"`` literais, o
    padrão não casa e a chave sairia vazia mesmo com o XML completo.
    """
    texto = html.unescape(_texto(xml))
    if not texto:
        return ""
    encontrado = PADRAO_CHAVE_NFE.search(texto)
    return encontrado.group(1) if encontrado else ""


class OmieClient:
    """Cliente mínimo da API do Omie para NF-e de entrada.

    Como a Omie não emite token, as credenciais acompanham cada requisição;
    elas nunca são registradas em log nem ecoadas em ``ErroOmie``. As datas são
    normalizadas localmente e a paginação é explícita (método por página) para
    a fase 2 poder pausar/retomar a leitura sem baixar tudo de novo.
    """

    def __init__(
        self,
        app_key: Optional[str] = None,
        app_secret: Optional[str] = None,
        timeout: Optional[float] = None,
    ):
        """Guarda as credenciais do aplicativo Omie e o tempo limite.

        ``app_key``/``app_secret`` podem ser omitidos para usar as variáveis
        ``OMIE_APP_KEY``/``OMIE_APP_SECRET`` do ``.env`` — útil para scripts e
        para a fase 2, mas o chamador continua podendo passá-los explicitamente
        (por exemplo, em um teste com credencial de homologação).
        """
        self.app_key = (
            app_key if app_key is not None else _valor_ambiente("OMIE_APP_KEY")
        ).strip()
        self.app_secret = (
            app_secret
            if app_secret is not None
            else _valor_ambiente("OMIE_APP_SECRET")
        ).strip()
        self.timeout = self._resolver_timeout(timeout)

    @staticmethod
    def _resolver_timeout(timeout: Optional[float]) -> float:
        """Prioriza o timeout do chamador e depois ``OMIE_TIMEOUT``.

        Um valor inválido no ``.env`` não pode derrubar o teste: nesse caso
        volta-se ao padrão em vez de estourar ``ValueError`` na construção.
        """
        if timeout is not None:
            return float(timeout)
        bruto = _valor_ambiente("OMIE_TIMEOUT")
        if not bruto:
            return float(TEMPO_LIMITE_PADRAO)
        try:
            return float(bruto)
        except ValueError:
            return float(TEMPO_LIMITE_PADRAO)

    @property
    def configurado(self) -> bool:
        """True quando ``app_key`` e ``app_secret`` estão preenchidos."""
        return bool(self.app_key and self.app_secret)

    def _exigir_credenciais(self) -> None:
        if not self.configurado:
            raise OmieNaoConfigurado()

    def _chamar(
        self, url: str, call: str, param: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Executa um método do Omie e padroniza os dois níveis de erro.

        O envelope ``{"call", "app_key", "app_secret", "param": [param]}`` é o
        mesmo para qualquer endpoint; centralizá-lo garante que nenhuma chamada
        esqueça a autenticação. A validação do corpo depois do POST existe
        porque a Omie responde HTTP 200 em erros de negócio: sem ela, o
        resultado seria interpretado como "nenhum registro".
        """
        self._exigir_credenciais()
        envelope = {
            "call": call,
            "app_key": self.app_key,
            "app_secret": self.app_secret,
            "param": [param],
        }
        logger.info(
            "Omie %s: POST %s (página %s)", call, url, param.get("nPagina")
        )
        try:
            resposta = requests.post(url, json=envelope, timeout=self.timeout)
        except requests.Timeout as exc:
            raise ErroOmie(
                f"Tempo limite na API do Omie ({call}, {self.timeout:g}s)."
            ) from exc
        except requests.RequestException as exc:
            raise ErroOmie(
                f"Falha de conexão com a API do Omie ({call}): {exc}"
            ) from exc

        corpo = _json_ou_texto(resposta)

        # Nível 1: erro HTTP (proxy, URL errada, indisponibilidade do Omie).
        # O 500 "Não existem registros para a página" é o jeito do Omie de dizer
        # "sem dados" — tem classe própria para as listagens devolverem [].
        if not resposta.ok:
            detalhe = _detalhe_erro(corpo)
            mensagem = (
                f"API do Omie recusou {call} (HTTP {resposta.status_code})"
            )
            if detalhe:
                mensagem += f": {detalhe}"
            if _pagina_sem_registros(resposta.status_code, corpo):
                raise OmieSemRegistros(
                    mensagem, status=resposta.status_code, corpo=corpo
                )
            raise ErroOmie(
                mensagem, status=resposta.status_code, corpo=corpo
            )

        if not isinstance(corpo, dict):
            raise ErroOmie(
                f"A API do Omie devolveu uma resposta inesperada em {call}.",
                status=resposta.status_code,
                corpo=corpo,
            )

        # Nível 2: erro de negócio dentro de uma resposta HTTP 200.
        falha = _erro_aplicacao(corpo)
        if falha is not None:
            codigo, detalhe = falha
            mensagem = f"API do Omie recusou {call}"
            if codigo:
                mensagem += f" (código {codigo})"
            if detalhe:
                mensagem += f": {detalhe}"
            raise ErroOmie(
                mensagem,
                status=resposta.status_code,
                corpo=corpo,
                codigo=codigo,
            )
        return corpo

    # ── Recebimentos de NF-e (compras) ────────────────────────────────────────
    def _pagina_recebimentos(
        self,
        inicio: Optional[date],
        fim: Optional[date],
        pagina: int,
        registros_por_pagina: int,
        etapa: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Busca uma página de ``ListarRecebimentos`` já validada.

        ``cExibirDetalhes="S"`` é essencial para a integração: é o que acrescenta
        ``itensRecebimento``, ``parcelas``, ``departamentos``, ``categorias``,
        ``infoCadastro`` e ``cabec.cNaturezaOperacao``. Sem ele a nota vem só
        com o cabeçalho (chave/valor), insuficiente para a conferência de custo
        que o sistema faz na fase 2.

        ``etapa`` (``cEtapa``) filtra a coluna do kanban do Omie — a importação
        usa 40/60/80 (Faturado/Recebido/Conferido). O campo entra só quando
        informado: sem ele a Omie devolve as notas de todas as etapas.
        """
        param: Dict[str, Any] = {
            "nPagina": pagina,
            "nRegistrosPorPagina": registros_por_pagina,
            "cExibirDetalhes": "S",
        }
        if inicio is not None and fim is not None:
            param["dtEmissaoDe"] = inicio.strftime("%d/%m/%Y")
            param["dtEmissaoAte"] = fim.strftime("%d/%m/%Y")
        if etapa not in (None, ""):
            param["cEtapa"] = str(etapa).strip()
        return self._chamar(URL_RECEBIMENTOS, "ListarRecebimentos", param)

    def listar_recebimentos(
        self,
        data_inicio: Optional[DataOmie] = None,
        data_fim: Optional[DataOmie] = None,
        pagina: int = 1,
        registros_por_pagina: int = REGISTROS_POR_PAGINA_PADRAO,
        etapa: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Lista as notas de entrada (recebimentos) de **uma** página.

        O filtro é por data de emissão da NF-e, então uma compra lançada no
        Omie fora desse período não aparece — é o recorte que o teste manual
        usa para conferir os últimos dias. Sem datas (``None`` nas duas), a
        listagem traz o histórico inteiro da etapa informada.

        Args:
            data_inicio: início do período (``date``/``datetime`` ou
                ``dd/mm/aaaa``/``aaaa-mm-dd``) ou ``None`` para não filtrar.
            data_fim: fim do período, inclusive (``None`` junto com o início).
            pagina: número da página, começando em 1.
            registros_por_pagina: tamanho da página (máximo 100 no Omie).
            etapa: ``cEtapa`` do Omie (40/60/80) ou ``None`` para todas.

        Returns:
            Lista de recebimentos; cada item tem a estrutura documentada
            (``cabec``, ``itensRecebimento``, ``parcelas``...).

        Raises:
            ValueError: datas inválidas ou período invertido.
            OmieNaoConfigurado: credenciais ausentes.
            ErroOmie: falha HTTP, de negócio (``omie_fail``/``cCodStatus``)
                ou resposta fora do contrato.
        """
        inicio, fim = _periodo_opcional(data_inicio, data_fim)
        pagina, por_pagina = _paginacao(pagina, registros_por_pagina)
        try:
            resposta = self._pagina_recebimentos(
                inicio, fim, pagina, por_pagina, etapa
            )
        except OmieSemRegistros:
            # Etapa/período sem notas: a Omie responde 500 em vez de lista vazia.
            return []
        return _lista_de(resposta, "recebimentos", "ListarRecebimentos")

    def listar_todos_recebimentos(
        self,
        data_inicio: Optional[DataOmie] = None,
        data_fim: Optional[DataOmie] = None,
        *,
        registros_por_pagina: int = REGISTROS_POR_PAGINA_PADRAO,
        maximo_paginas: int = MAXIMO_PAGINAS_PADRAO,
        etapa: Optional[str] = None,
        intervalo: float = 0.0,
    ) -> List[Dict[str, Any]]:
        """Percorre todas as páginas do período e devolve os recebimentos.

        A parada usa ``nTotalPaginas`` (fonte da Omie) e, como reserva, a
        página incompleta/vazia. ``maximo_paginas`` é um teto de segurança
        contra laço infinito — quando ele é atingido, fica registrado no log
        para o operador saber que a leitura foi parcial.

        ``etapa`` filtra o ``cEtapa`` (coluna do kanban) e ``intervalo`` é a
        pausa em segundos entre páginas. O intervalo existe porque a Omie
        devolve **HTTP 425** (consumo indevido) em rajadas de chamadas: a
        importação completa percorre centenas de notas e precisa desse respiro.
        """
        inicio, fim = _periodo_opcional(data_inicio, data_fim)
        _pagina, por_pagina = _paginacao(1, registros_por_pagina)
        limite = max(1, int(maximo_paginas))
        pausa = max(0.0, float(intervalo or 0.0))

        recebimentos: List[Dict[str, Any]] = []
        for pagina in range(1, limite + 1):
            try:
                resposta = self._pagina_recebimentos(
                    inicio, fim, pagina, por_pagina, etapa
                )
            except OmieSemRegistros:
                # Página 1 sem registros = etapa vazia no período; o que já foi
                # lido (se for uma página seguinte) continua válido.
                return recebimentos
            lote = _lista_de(resposta, "recebimentos", "ListarRecebimentos")
            recebimentos.extend(lote)

            total_paginas = _inteiro(resposta.get("nTotalPaginas"))
            if (
                not lote
                or len(lote) < por_pagina
                or (total_paginas and pagina >= total_paginas)
            ):
                return recebimentos
            if pausa:
                time.sleep(pausa)

        logger.warning(
            "Omie ListarRecebimentos: limite de %s páginas atingido; "
            "a listagem pode estar incompleta.",
            limite,
        )
        return recebimentos

    # ── Documentos fiscais (XML completo) ────────────────────────────────────
    def _pagina_documentos(
        self,
        inicio: date,
        fim: date,
        pagina: int,
        registros_por_pagina: int,
    ) -> Dict[str, Any]:
        """Busca uma página de ``ListarDocumentos`` (XML de entrada).

        Os nomes dos campos diferem dos recebimentos: aqui o tamanho da página
        é ``nRegPorPagina`` e as datas são ``dEmiInicial``/``dEmiFinal``.
        Enviar ``nRegistrosPorPagina`` faria a Omie ignorar o tamanho pedido.
        """
        param = {
            "nPagina": pagina,
            "nRegPorPagina": registros_por_pagina,
            # Obrigatórios: 55 = NF-e; 0 = Entrada (o default do Omie é Saída).
            "cModelo": MODELO_NFE,
            "cOperacao": OPERACAO_ENTRADA,
            "dEmiInicial": inicio.strftime("%d/%m/%Y"),
            "dEmiFinal": fim.strftime("%d/%m/%Y"),
        }
        return self._chamar(URL_DOCUMENTOS, "ListarDocumentos", param)

    def listar_documentos_entrada(
        self,
        data_inicio: DataOmie,
        data_fim: DataOmie,
        pagina: int = 1,
        registros_por_pagina: int = REGISTROS_POR_PAGINA_PADRAO,
    ) -> List[Dict[str, Any]]:
        """Lista os documentos de **entrada** de uma página, com o ``cXml``.

        ``cOperacao="0"`` é obrigatório: o default do Omie é ``"1"`` (Saída),
        que devolveria as notas emitidas pela empresa. ``cModelo="55"`` evita
        CT-e (57/67) e NFS-e (99) na mesma listagem.

        Returns:
            Lista de ``documentosEncontrados``; o XML completo de cada nota
            está em ``cXml`` e a chave de acesso em ``nChave``.

        Raises:
            ValueError: datas inválidas ou período invertido.
            OmieNaoConfigurado: credenciais ausentes.
            ErroOmie: falha HTTP, de negócio ou resposta fora do contrato.
        """
        inicio, fim = _periodo(data_inicio, data_fim)
        pagina, por_pagina = _paginacao(pagina, registros_por_pagina)
        try:
            resposta = self._pagina_documentos(inicio, fim, pagina, por_pagina)
        except OmieSemRegistros:
            # Nenhum documento de entrada no período (500 "sem registros").
            return []
        return _lista_de(
            resposta, "documentosEncontrados", "ListarDocumentos"
        )

    def listar_todos_documentos_entrada(
        self,
        data_inicio: DataOmie,
        data_fim: DataOmie,
        *,
        registros_por_pagina: int = REGISTROS_POR_PAGINA_PADRAO,
        maximo_paginas: int = MAXIMO_PAGINAS_PADRAO,
    ) -> List[Dict[str, Any]]:
        """Percorre todas as páginas e devolve os documentos de entrada.

        A resposta de ``ListarDocumentos`` não traz ``nTotalPaginas``; a parada
        é feita por página incompleta/vazia e pelo teto ``maximo_paginas``.
        Como cada item carrega o XML inteiro, o teto impede que um filtro
        errado baixe centenas de páginas sem limite.
        """
        inicio, fim = _periodo(data_inicio, data_fim)
        _pagina, por_pagina = _paginacao(1, registros_por_pagina)
        limite = max(1, int(maximo_paginas))

        documentos: List[Dict[str, Any]] = []
        for pagina in range(1, limite + 1):
            try:
                resposta = self._pagina_documentos(inicio, fim, pagina, por_pagina)
            except OmieSemRegistros:
                return documentos
            lote = _lista_de(
                resposta, "documentosEncontrados", "ListarDocumentos"
            )
            documentos.extend(lote)
            if not lote or len(lote) < por_pagina:
                return documentos

        logger.warning(
            "Omie ListarDocumentos: limite de %s páginas atingido; "
            "a listagem pode estar incompleta.",
            limite,
        )
        return documentos
