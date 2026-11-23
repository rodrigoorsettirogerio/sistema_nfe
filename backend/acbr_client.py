"""Cliente HTTP da ACBr API (sandbox/homologação).

Concentra a autenticação OAuth2 (`client_credentials`), o cadastro de empresas e
certificados e as operações de Distribuição NF-e (pedido, listagem e download
do XML). As credenciais vêm exclusivamente do `.env` da raiz do projeto — nada
de chave fixa no código.

Variáveis de ambiente:

- `ACBR_CLIENT_ID` (obrigatória);
- `ACBR_CLIENT_SECRET` (obrigatória);
- `ACBR_AMBIENTE` (opcional): `sandbox` ou `producao`, usada só como rótulo;
- `ACBR_AUTH_URL` (opcional): endpoint do Keycloak que emite o token;
- `ACBR_API_URL` (opcional): base da API de homologação/produção;
- `ACBR_SCOPES` (opcional): permissões do token, separadas por espaço
  (padrão `empresa distribuicao-nfe`, exigidas pelo cadastro da empresa e pelo
  download de XMLs);
- `ACBR_CIENCIA_AUTOMATICA` (opcional): `0`/`false` desliga o manifesto
  automático. Ligado por padrão, o `PUT /empresas/{cnpj}/distnfe` envia
  `ciencia_automatica: true` e a própria ACBr API envia a "Ciência da
  Operação" (210210) das notas recebidas;
- `ACBR_TIMEOUT` (opcional): segundos, padrão 30.

O token fica em memória e é reutilizado até perto do vencimento (`expires_in`).
Falhas de configuração, rede ou HTTP viram `ErroAcbr`, com a mensagem que a API
devolveu quando existir.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote

import requests
from dotenv import load_dotenv

# Carrega o .env da raiz do projeto ANTES de ler qualquer ACBR_*.
# `override=False` preserva variáveis injetadas pelo serviço em produção.
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

AMBIENTE_PADRAO = "sandbox"
AUTH_URL_PADRAO = (
    "https://auth.acbr.api.br/realms/ACBrAPI/protocol/openid-connect/token"
)
API_URL_PADRAO = "https://hom.acbr.api.br"
# A ACBr API exige `scope` no token. `empresa` cobre o cadastro da empresa e do
# certificado; `distribuicao-nfe` cobre o pedido/listagem/download dos XMLs.
SCOPES_PADRAO = "empresa distribuicao-nfe"
TEMPO_LIMITE_PADRAO = 30
# Renova o token um minuto antes do vencimento para evitar corrida na borda.
MARGEM_EXPIRACAO_SEGUNDOS = 60

logger = logging.getLogger("sistema_nfe")


class ErroAcbr(RuntimeError):
    """Falha controlada na comunicação com a ACBr API."""

    def __init__(
        self,
        mensagem: str,
        status: Optional[int] = None,
        corpo: Optional[Any] = None,
    ):
        super().__init__(mensagem)
        self.status = status
        self.corpo = corpo


class AcbrNaoConfigurado(ErroAcbr):
    """Credenciais ausentes no ambiente: a integração está desligada."""

    def __init__(self, mensagem: Optional[str] = None):
        super().__init__(
            mensagem
            or (
                "ACBr API não configurada: defina ACBR_CLIENT_ID e "
                "ACBR_CLIENT_SECRET no .env e reinicie o processo."
            )
        )


def _valor_ambiente(nome: str, padrao: str = "") -> str:
    """Lê uma variável e remove espaços acidentais nas pontas."""
    return (os.getenv(nome) or padrao).strip()


def _ciencia_automatica_habilitada() -> bool:
    """Lê ``ACBR_CIENCIA_AUTOMATICA`` (padrão ligado).

    Ligado, ``configurar_distribuicao_nfe`` envia ``ciencia_automatica: true``
    no ``PUT /empresas/{cnpj}/distnfe``: a ACBr API manifesta a "Ciência da
    Operação" (210210) das notas recebidas em qualquer forma de distribuição e
    libera o XML completo (procNFe). ``ACBR_CIENCIA_AUTOMATICA=0`` (ou
    ``false``/``off``) desliga o manifesto automático sem mudar o restante da
    configuração.
    """
    texto = _valor_ambiente("ACBR_CIENCIA_AUTOMATICA", "1").lower()
    return texto not in {"0", "false", "nao", "não", "off", "desligado"}


def _json_ou_texto(resposta: requests.Response) -> Any:
    """Tenta JSON; se a API devolver HTML/texto, preserva o texto do erro."""
    try:
        return resposta.json()
    except ValueError:
        return resposta.text


def _detalhe_erro(corpo: Any) -> str:
    """Extrai a mensagem mais útil de um corpo de erro OAuth/HTTP."""
    if isinstance(corpo, dict):
        for chave in (
            "error_description",
            "mensagem",
            "message",
            "detail",
            "error",
        ):
            valor = corpo.get(chave)
            if valor:
                return str(valor)
        return str(corpo)
    return str(corpo or "").strip()


def _so_digitos(valor: Any) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def _nsu_texto(valor: Any) -> str:
    """Normaliza um NSU devolvido pela ACBr como texto numérico.

    A API pode responder com inteiro, texto ou número em ponto flutuante. Valores
    inválidos (``None``, booleano, texto não numérico) viram string vazia.
    """
    if valor is None or isinstance(valor, bool):
        return ""
    texto = str(valor).strip()
    if not texto:
        return ""
    try:
        return str(max(0, int(texto)))
    except (TypeError, ValueError):
        pass
    try:
        return str(max(0, int(float(texto))))
    except (TypeError, ValueError):
        return ""


def _nsu_esperado_pela_acbr(erro: Any) -> str:
    """Extrai o cursor exigido quando a ACBr recusa ``dist_nsu`` desatualizado.

    A resposta de validação tem o formato::

        {"message": "O campo 'dist_nsu' deve ser igual a 28165."}

    O número devolvido é a fonte autoritativa para reparar uma loja cujo
    ``empresas.ultimo_nsu_acbr`` ficou atrás do estado da ACBr. Retorna ``""``
    quando a mensagem não é desse tipo.
    """
    candidatos = [str(erro)]
    corpo = getattr(erro, "corpo", None)
    if corpo is not None:
        candidatos.append(_detalhe_erro(corpo))
        try:
            candidatos.append(json.dumps(corpo, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            pass
    padrao = re.compile(
        r"dist_nsu[^0-9]{0,160}?(?:igual\s+a|equal\s+to)\s*['\"]?(\d+)",
        re.IGNORECASE | re.DOTALL,
    )
    for texto in candidatos:
        encontrado = padrao.search(str(texto or ""))
        if encontrado:
            nsu = _nsu_texto(encontrado.group(1))
            if nsu:
                return nsu
    return ""


def _mesclar_dados_empresa(base: Any, novos: Any) -> Dict[str, Any]:
    """Mescla dados cadastrais sem deixar valores vazios apagarem os existentes."""
    dados = dict(base or {})
    for chave, valor in (novos or {}).items():
        if isinstance(valor, str):
            valor = valor.strip()
        if valor in (None, "", [], {}):
            continue
        dados[chave] = valor
    return dados


def _endereco_acbr_completo(dados: Any) -> bool:
    """True quando o dicionário tem todos os campos de endereço da ACBr.

    Usado para saber se o cadastro salvo na tabela ``empresas`` pode substituir
    a BrasilAPI quando ela está indisponível (429/offline).
    """
    if not isinstance(dados, dict):
        return False
    logradouro = str(dados.get("logradouro") or "").strip()
    bairro = str(dados.get("bairro") or "").strip()
    uf = str(dados.get("uf") or "").strip().upper()
    codigo = _so_digitos(
        dados.get("codigo_municipio_ibge") or dados.get("codigo_municipio")
    )
    cep = _so_digitos(dados.get("cep"))
    return bool(
        logradouro
        and bairro
        and len(uf) == 2
        and len(codigo) == 7
        and len(cep) == 8
    )


def _dados_empresa_local(cnpj: str) -> Dict[str, Any]:
    """Endereço já persistido na tabela ``empresas`` (fallback opcional).

    A importação é tardia para o cliente HTTP continuar utilizável sem o banco
    (scripts isolados/testes) e para evitar import circular com ``models``.
    Qualquer falha ao abrir/consultar o banco é ignorada: nesse caso o fluxo
    segue com a BrasilAPI e devolve o erro original se ela também falhar.
    """
    try:
        from backend.database import SessionLocal
        from backend.models import Empresa
    except ImportError:
        return {}

    sessao = None
    try:
        sessao = SessionLocal()
        empresa = sessao.query(Empresa).filter(Empresa.cnpj == cnpj).first()
        if empresa is None:
            return {}
        return {
            "razao_social": getattr(empresa, "razao_social", "") or "",
            "nome_fantasia": getattr(empresa, "nome_loja", "") or "",
            "logradouro": getattr(empresa, "logradouro", "") or "",
            "numero": getattr(empresa, "numero", "") or "",
            "complemento": getattr(empresa, "complemento", "") or "",
            "bairro": getattr(empresa, "bairro", "") or "",
            "codigo_municipio_ibge": getattr(
                empresa, "codigo_municipio", ""
            )
            or "",
            "municipio": getattr(empresa, "municipio", "") or "",
            "uf": getattr(empresa, "uf", "") or "",
            "cep": getattr(empresa, "cep", "") or "",
        }
    except Exception:
        return {}
    finally:
        if sessao is not None:
            sessao.close()


def _ler_nsu_empresa_local(cnpj: str) -> str:
    """Lê ``empresas.ultimo_nsu_acbr`` imediatamente antes de chamar a ACBr.

    O cursor é a fonte da verdade para o campo ``dist_nsu``: a ACBr exige
    exatamente o ``ultNSU`` devolvido na consulta anterior. A leitura usa uma
    sessão curta e própria para enxergar avanços feitos por outra automação e
    nunca reaproveitar um valor que o chamador passou.

    Loja inexistente ou sem NSU devolve ``""`` (o chamador envia ``0``). Falha
    de banco vira ``ErroAcbr`` em vez de cair silenciosamente para ``0``, que
    faria a ACBr recusar a consulta com um cursor errado.
    """
    try:
        from backend.database import SessionLocal
        from backend.models import Empresa
    except ImportError as erro:
        raise ErroAcbr(
            "Não foi possível importar o acesso ao banco para ler "
            "empresas.ultimo_nsu_acbr."
        ) from erro

    sessao = None
    try:
        sessao = SessionLocal()
        empresa = sessao.query(Empresa).filter(Empresa.cnpj == cnpj).first()
        return (
            _nsu_texto(getattr(empresa, "ultimo_nsu_acbr", ""))
            if empresa is not None
            else ""
        )
    except Exception as erro:
        logger.exception(
            "ACBr NSU %s: falha ao ler empresas.ultimo_nsu_acbr", cnpj
        )
        raise ErroAcbr(
            "Não foi possível ler empresas.ultimo_nsu_acbr antes de "
            f"consultar a ACBr para o CNPJ {cnpj}: {erro}"
        ) from erro
    finally:
        if sessao is not None:
            sessao.close()


def _salvar_nsu_empresa_local(cnpj: str, nsu: Any) -> bool:
    """Grava o ``ultNSU`` devolvido pela ACBr em ``empresas.ultimo_nsu_acbr``.

    A escrita é feita numa sessão própria e curta para o cliente continuar sem
    depender do ``Session`` da rota. Retorna False quando a loja não existe ou
    o banco não está disponível, sem mascarar o resultado da consulta HTTP.
    """
    valor = _nsu_texto(nsu)
    if not valor:
        return False
    try:
        from backend.database import SessionLocal
        from backend.models import Empresa
    except ImportError:
        return False

    sessao = None
    try:
        sessao = SessionLocal()
        atualizadas = (
            sessao.query(Empresa)
            .filter(Empresa.cnpj == cnpj)
            .update({Empresa.ultimo_nsu_acbr: valor}, synchronize_session=False)
        )
        sessao.commit()
        return bool(atualizadas)
    except Exception:
        if sessao is not None:
            sessao.rollback()
        return False
    finally:
        if sessao is not None:
            sessao.close()


def _payload_empresa_acbr(cnpj: str, dados: Any) -> Dict[str, Any]:
    """Monta o JSON de ``POST /empresas`` a partir da BrasilAPI ou do banco."""
    dados = dados or {}

    def texto(campo: str) -> str:
        return str(dados.get(campo) or "").strip()

    tipo_logradouro = texto("descricao_tipo_de_logradouro")
    logradouro = texto("logradouro")
    if tipo_logradouro and logradouro:
        # O banco guarda o logradouro já completo ("RUA ..."); a BrasilAPI manda
        # o tipo separado. Evita "RUA RUA ..." ao mesclar as duas fontes.
        if not logradouro.upper().startswith(tipo_logradouro.upper()):
            logradouro = f"{tipo_logradouro} {logradouro}"
    elif tipo_logradouro:
        logradouro = tipo_logradouro

    razao_social = (
        texto("razao_social")
        or texto("nome_fantasia")
        or f"Empresa {cnpj}"
    )
    return {
        "cpf_cnpj": cnpj,
        "nome_razao_social": razao_social,
        "email": texto("email") or "contato@empresa.com.br",
        "endereco": {
            "logradouro": logradouro,
            "numero": texto("numero") or "S/N",
            "complemento": texto("complemento"),
            "bairro": texto("bairro"),
            "codigo_municipio": _so_digitos(
                dados.get("codigo_municipio_ibge")
                or dados.get("codigo_municipio")
            ),
            "cidade": texto("municipio") or texto("cidade"),
            "uf": texto("uf").upper(),
            "codigo_pais": "1058",
            "pais": "BRASIL",
            "cep": _so_digitos(dados.get("cep")),
        },
    }


def _extrair_xml(conteudo: bytes) -> bytes:
    """Devolve os bytes do XML mesmo quando a API embrulha o conteúdo em JSON.

    O contrato do endpoint de download é ``application/xml`` (arquivo), mas
    alguns proxies/sandboxes respondem JSON com o XML em ``xml``/``conteudo``.
    Nesse caso o campo textual é devolvido; se ele estiver em base64, é
    decodificado. XML puro passa intacto.
    """
    if not conteudo:
        return b""
    texto = conteudo.decode("utf-8-sig", errors="replace").strip()
    if texto.startswith("{"):
        try:
            dados = json.loads(texto)
        except ValueError:
            dados = None
        if isinstance(dados, dict):
            for chave in ("xml", "conteudo", "content", "documento"):
                valor = dados.get(chave)
                if not isinstance(valor, str) or not valor.strip():
                    continue
                valor = valor.strip()
                if valor.startswith("<"):
                    return valor.encode("utf-8")
                try:
                    return base64.b64decode(valor, validate=True)
                except (ValueError, TypeError):
                    return valor.encode("utf-8")
    return conteudo


class AcbrClient:
    """Cliente mínimo da ACBr API com autenticação OAuth2."""

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        ambiente: Optional[str] = None,
        auth_url: Optional[str] = None,
        api_url: Optional[str] = None,
        scopes: Optional[str] = None,
        timeout: Optional[float] = None,
    ):
        self.client_id = (
            client_id if client_id is not None else _valor_ambiente("ACBR_CLIENT_ID")
        ).strip()
        self.client_secret = (
            client_secret
            if client_secret is not None
            else _valor_ambiente("ACBR_CLIENT_SECRET")
        ).strip()
        self.ambiente = (
            ambiente or _valor_ambiente("ACBR_AMBIENTE", AMBIENTE_PADRAO)
        ) or AMBIENTE_PADRAO
        self.auth_url = (
            auth_url or _valor_ambiente("ACBR_AUTH_URL", AUTH_URL_PADRAO)
        ).strip() or AUTH_URL_PADRAO
        self.api_url = (
            api_url or _valor_ambiente("ACBR_API_URL", API_URL_PADRAO)
        ).strip().rstrip("/") or API_URL_PADRAO
        self.scopes = (
            scopes if scopes is not None else _valor_ambiente("ACBR_SCOPES")
        ).strip() or SCOPES_PADRAO
        self.timeout = self._resolver_timeout(timeout)

        self._access_token: Optional[str] = None
        self._expira_em = 0.0

    @staticmethod
    def _resolver_timeout(timeout: Optional[float]) -> float:
        if timeout is not None:
            return float(timeout)
        bruto = _valor_ambiente("ACBR_TIMEOUT")
        if not bruto:
            return float(TEMPO_LIMITE_PADRAO)
        try:
            return float(bruto)
        except ValueError:
            return float(TEMPO_LIMITE_PADRAO)

    @property
    def configurado(self) -> bool:
        """True quando cliente e segredo estão presentes no ambiente."""
        return bool(self.client_id and self.client_secret)

    @property
    def ambiente_distribuicao(self) -> str:
        """Ambiente aceito pela ACBr: ``homologacao`` ou ``producao``.

        ``ACBR_AMBIENTE=sandbox`` (ou ``sandbox1``, ``homologacao``...) cai em
        homologação; qualquer outro valor é tratado como produção.
        """
        rotulo = re.sub(r"[^a-z]", "", str(self.ambiente or "").lower())
        if rotulo in {"producao", "prod", "production"}:
            return "producao"
        return "homologacao"

    def _exigir_credenciais(self) -> None:
        if not self.configurado:
            raise AcbrNaoConfigurado()

    def autenticar(self, forcar: bool = False) -> str:
        """Obtém (ou reutiliza) o `access_token` via `client_credentials`.

        Args:
            forcar: ignora o token em cache e pede um novo.

        Returns:
            O access token pronto para o cabeçalho `Authorization: Bearer`.
        """
        self._exigir_credenciais()

        agora = time.monotonic()
        if not forcar and self._access_token and agora < self._expira_em:
            return self._access_token

        try:
            resposta = requests.post(
                self.auth_url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "scope": self.scopes,
                },
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=self.timeout,
            )
        except requests.Timeout as exc:
            raise ErroAcbr(
                f"Tempo limite ao autenticar na ACBr API ({self.timeout:g}s)."
            ) from exc
        except requests.RequestException as exc:
            raise ErroAcbr(
                f"Falha de conexão ao autenticar na ACBr API: {exc}"
            ) from exc

        corpo = _json_ou_texto(resposta)
        if not resposta.ok:
            detalhe = _detalhe_erro(corpo)
            mensagem = "Falha ao autenticar na ACBr API"
            if detalhe:
                mensagem += f": {detalhe}"
            raise ErroAcbr(mensagem, status=resposta.status_code, corpo=corpo)

        token = corpo.get("access_token") if isinstance(corpo, dict) else None
        if not token:
            raise ErroAcbr(
                "A ACBr API respondeu sem access_token.",
                status=resposta.status_code,
                corpo=corpo,
            )

        try:
            expires_in = float(corpo.get("expires_in") or 0)
        except (TypeError, ValueError):
            expires_in = 0.0

        self._access_token = str(token)
        if expires_in > 0:
            self._expira_em = agora + max(
                expires_in - MARGEM_EXPIRACAO_SEGUNDOS, 0.0
            )
        else:
            # Sem `expires_in`, não guarda para não arriscar usar token vencido.
            self._expira_em = agora
        return self._access_token

    def _cabecalhos(self, token: str) -> Dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        }

    def _requisicao(
        self,
        metodo: str,
        caminho: str,
        *,
        json: Optional[Any] = None,
        params: Optional[Dict[str, Any]] = None,
        retornar_resposta: bool = False,
        accept: Optional[str] = None,
    ) -> Any:
        """Executa uma chamada autenticada e padroniza os erros da ACBr.

        ``retornar_resposta=True`` devolve o ``requests.Response`` (usado no
        download binário do XML); nos demais casos devolve o JSON/texto.
        """
        token = self.autenticar()
        url = f"{self.api_url}/{str(caminho).lstrip('/')}"
        cabecalhos = self._cabecalhos(token)
        if accept:
            cabecalhos["Accept"] = str(accept)
        try:
            resposta = requests.request(
                metodo.upper(),
                url,
                headers=cabecalhos,
                json=json,
                params=params,
                timeout=self.timeout,
            )
        except requests.Timeout as exc:
            raise ErroAcbr(
                f"Tempo limite na ACBr API ({metodo.upper()} {caminho}, "
                f"{self.timeout:g}s)."
            ) from exc
        except requests.RequestException as exc:
            raise ErroAcbr(
                f"Falha de conexão com a ACBr API ({metodo.upper()} {caminho}): {exc}"
            ) from exc

        if resposta.ok and retornar_resposta:
            return resposta

        corpo = _json_ou_texto(resposta)
        if not resposta.ok:
            detalhe = _detalhe_erro(corpo)
            mensagem = (
                f"ACBr API recusou {metodo.upper()} {caminho} "
                f"(HTTP {resposta.status_code})"
            )
            if detalhe:
                mensagem += f": {detalhe}"
            raise ErroAcbr(mensagem, status=resposta.status_code, corpo=corpo)
        return corpo

    def consultar_cep(self, cep: str) -> Any:
        """Consulta um CEP (8 dígitos) no endpoint `/cep/{cep}`.

        Aceita máscara (`01311-200`) e devolve o JSON da API sem reinterpretar,
        para que o teste mostre exatamente o contrato do sandbox.
        """
        cep_limpo = re.sub(r"\D", "", str(cep or ""))
        if len(cep_limpo) != 8:
            raise ValueError("Informe um CEP com 8 dígitos.")

        token = self.autenticar()
        url = f"{self.api_url}/cep/{cep_limpo}"
        try:
            resposta = requests.get(
                url,
                headers=self._cabecalhos(token),
                timeout=self.timeout,
            )
        except requests.Timeout as exc:
            raise ErroAcbr(
                f"Tempo limite ao consultar o CEP {cep_limpo} ({self.timeout:g}s)."
            ) from exc
        except requests.RequestException as exc:
            raise ErroAcbr(
                f"Falha de conexão ao consultar o CEP {cep_limpo}: {exc}"
            ) from exc

        corpo = _json_ou_texto(resposta)
        if not resposta.ok:
            detalhe = _detalhe_erro(corpo)
            mensagem = (
                f"Falha ao consultar o CEP {cep_limpo} "
                f"(HTTP {resposta.status_code})"
            )
            if detalhe:
                mensagem += f": {detalhe}"
            raise ErroAcbr(mensagem, status=resposta.status_code, corpo=corpo)
        return corpo


    # ── Empresas / certificado ────────────────────────────────────────────────
    def consultar_empresa(self, cpf_cnpj: str) -> Dict[str, Any]:
        """Consulta uma empresa cadastrada na ACBr (404 quando não existe)."""
        cnpj = _so_digitos(cpf_cnpj)
        if len(cnpj) != 14:
            raise ValueError("Informe um CNPJ com 14 dígitos.")
        return self._requisicao("GET", f"/empresas/{cnpj}")

    def cadastrar_empresa(self, dados: Dict[str, Any]) -> Dict[str, Any]:
        """``POST /empresas`` com o cadastro completo exigido pela ACBr."""
        return self._requisicao("POST", "/empresas", json=dados)

    def atualizar_empresa(
        self, cpf_cnpj: str, dados: Dict[str, Any]
    ) -> Dict[str, Any]:
        """``PUT /empresas/{cnpj}`` (idempotente para reenvio do cadastro)."""
        cnpj = _so_digitos(cpf_cnpj)
        if len(cnpj) != 14:
            raise ValueError("Informe um CNPJ com 14 dígitos.")
        return self._requisicao("PUT", f"/empresas/{cnpj}", json=dados)

    def enviar_certificado(
        self, cpf_cnpj: str, conteudo: bytes, senha: Optional[str] = None
    ) -> Dict[str, Any]:
        """``PUT /empresas/{cnpj}/certificado`` com o ``.pfx`` em base64.

        O corpo envia o arquivo em base64 e a senha em ``password`` (a ACBr precisa
        dela para abrir o PKCS#12). A senha existe apenas nesta requisição: não é
        gravada no banco, não volta na resposta e não entra em log. O token OAuth2
        vai no cabeçalho ``Authorization: Bearer <token>`` por ``_requisicao``.
        """
        cnpj = _so_digitos(cpf_cnpj)
        if len(cnpj) != 14:
            raise ValueError("Informe um CNPJ com 14 dígitos.")
        if not conteudo:
            raise ValueError("O conteúdo do certificado digital está vazio.")
        corpo = {"certificado": base64.b64encode(conteudo).decode("ascii")}
        if str(senha or "").strip():
            corpo["password"] = str(senha)
        return self._requisicao(
            "PUT", f"/empresas/{cnpj}/certificado", json=corpo
        )


    # ── Configuração e Distribuição NF-e ──────────────────────────────────────
    def consultar_configuracao_distribuicao_nfe(
        self, cpf_cnpj: str
    ) -> Dict[str, Any]:
        cnpj = _so_digitos(cpf_cnpj)
        if len(cnpj) != 14:
            raise ValueError("Informe um CNPJ com 14 dígitos.")
        return self._requisicao("GET", f"/empresas/{cnpj}/distnfe")

    def configurar_distribuicao_nfe(
        self, cpf_cnpj: str, ambiente: Optional[str] = None
    ) -> Dict[str, Any]:
        """Garante a configuração do serviço de Distribuição NF-e da empresa.

        O ``PUT /empresas/{cnpj}/distnfe`` envia ``ciencia_automatica: true``
        (padrão; ``ACBR_CIENCIA_AUTOMATICA=0`` desliga): assim a própria ACBr
        API envia a "Ciência da Operação" (210210) das notas recebidas, em
        qualquer forma de distribuição, e o XML completo (procNFe) passa a ser
        liberado.

        Quando já existe configuração, preserva as opções atuais (distribuição
        automática/intervalo) e só troca o ambiente se ele divergir. O ``PUT``
        também é reenviado quando a ciência automática ainda está desligada na
        ACBr — sem isso, empresas configuradas antes desta opção nunca
        receberiam a manifestação.
        """
        cnpj = _so_digitos(cpf_cnpj)
        if len(cnpj) != 14:
            raise ValueError("Informe um CNPJ com 14 dígitos.")
        alvo = ambiente or self.ambiente_distribuicao
        if alvo not in {"homologacao", "producao"}:
            raise ValueError("Ambiente inválido: use 'homologacao' ou 'producao'.")

        ciencia_desejada = _ciencia_automatica_habilitada()

        atual: Optional[Dict[str, Any]] = None
        try:
            atual = self.consultar_configuracao_distribuicao_nfe(cnpj)
        except ErroAcbr as erro:
            if erro.status != 404:
                raise

        if atual:
            ambiente_atual = str(atual.get("ambiente") or "")
            ciencia_atual = bool(atual.get("ciencia_automatica", False))
            if ambiente_atual == alvo and ciencia_atual == ciencia_desejada:
                return atual
            corpo = {
                "distribuicao_automatica": bool(
                    atual.get("distribuicao_automatica", False)
                ),
                "distribuicao_intervalo_horas": int(
                    atual.get("distribuicao_intervalo_horas") or 24
                ),
                "ciencia_automatica": ciencia_desejada,
                "ambiente": alvo,
            }
        else:
            corpo = {
                "distribuicao_automatica": False,
                "ciencia_automatica": ciencia_desejada,
                "ambiente": alvo,
            }
        return self._requisicao("PUT", f"/empresas/{cnpj}/distnfe", json=corpo)


    def distribuir_nfe(
        self,
        cpf_cnpj: str,
        *,
        ambiente: Optional[str] = None,
        dist_nsu: Optional[int] = None,
        uf_autor: Optional[str] = None,
        ignorar_tempo_espera: bool = False,
    ) -> Dict[str, Any]:
        """Solicita documentos por ``dist-nsu`` (lote do último NSU).

        O contrato ``DistribuicaoNfePedido`` da ACBr API v3.1.18 **não possui
        campos de data**. Enviar ``data_inicial``/``data_final`` (ou
        ``dataInicio``/``dataFim``) faz a API responder HTTP 400
        ``InvalidJsonProperty``. O recorte por período é feito localmente,
        depois, sobre a ``data_recebimento``/XML de cada documento.

        ``dist_nsu`` é lido diretamente de ``empresas.ultimo_nsu_acbr``
        imediatamente antes do POST. O parâmetro existe apenas por
        compatibilidade e nunca sobrescreve o cursor persistido. ``0`` inicia
        do começo da fila (primeiro download).
        """
        cnpj = _so_digitos(cpf_cnpj)
        if len(cnpj) != 14:
            raise ValueError("Informe um CNPJ com 14 dígitos.")
        nsu_banco = _ler_nsu_empresa_local(cnpj)
        nsu_envio = int(nsu_banco or 0)
        logger.info(
            "ACBr distribuição %s: dist_nsu=%s lido de "
            "empresas.ultimo_nsu_acbr (sem datas no payload).",
            cnpj,
            nsu_envio,
        )
        corpo: Dict[str, Any] = {
            "cpf_cnpj": cnpj,
            "ambiente": ambiente or self.ambiente_distribuicao,
            "tipo_consulta": "dist-nsu",
            # Obrigatório no contrato da ACBr para `dist-nsu`; deve ser
            # exatamente o `ultNSU` devolvido na consulta anterior.
            "dist_nsu": nsu_envio,
            "ignorar_tempo_espera": bool(ignorar_tempo_espera),
        }
        if uf_autor:
            corpo["uf_autor"] = str(uf_autor).strip().upper()
        try:
            return self._requisicao("POST", "/distribuicao/nfe", json=corpo)
        except ErroAcbr as erro:
            nsu_esperado = _nsu_esperado_pela_acbr(erro)
            if not nsu_esperado or nsu_esperado == str(nsu_envio):
                raise ErroAcbr(
                    "Falha ao solicitar a Distribuição NF-e na ACBr API "
                    f"para o CNPJ {cnpj}: {erro}",
                    status=erro.status,
                    corpo=erro.corpo,
                ) from erro

            logger.warning(
                "ACBr distribuição %s: banco tinha %s, mas a ACBr exige %s; "
                "sincronizando empresas.ultimo_nsu_acbr e repetindo o POST.",
                cnpj,
                nsu_envio,
                nsu_esperado,
            )
            if not _salvar_nsu_empresa_local(cnpj, nsu_esperado):
                logger.warning(
                    "ACBr distribuição %s: não foi possível persistir o cursor "
                    "%s durante a sincronização.",
                    cnpj,
                    nsu_esperado,
                )
            corpo["dist_nsu"] = int(nsu_esperado)
            try:
                return self._requisicao("POST", "/distribuicao/nfe", json=corpo)
            except ErroAcbr as segundo_erro:
                raise ErroAcbr(
                    "Falha ao solicitar a Distribuição NF-e na ACBr API "
                    f"para o CNPJ {cnpj} após sincronizar o cursor para "
                    f"{nsu_esperado}: {segundo_erro}",
                    status=segundo_erro.status,
                    corpo=segundo_erro.corpo,
                ) from segundo_erro

    def _dados_cadastro_brasilapi(
        self, cnpj: str, dados_empresa: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Busca o CNPJ na BrasilAPI e monta o payload de ``POST /empresas``.

        A ACBr exige razão social, e-mail e endereço completo. O CNPJ informado
        continua sendo a chave da empresa; quando a Receita não devolve e-mail,
        usa-se um endereço de contato e, quando falta número, ``S/N``.

        Se a BrasilAPI falhar (429, timeout, offline) e a loja já tiver o
        endereço completo gravado na tabela ``empresas``, o payload é montado
        com esses dados. ``dados_empresa`` permite passar o cadastro local
        explicitamente (útil em testes); quando omitido, ele é lido do banco.
        """
        local = (
            dados_empresa
            if dados_empresa is not None
            else _dados_empresa_local(cnpj)
        ) or {}
        url = f"https://brasilapi.com.br/api/cnpj/v1/{cnpj}"
        try:
            resposta = requests.get(url, timeout=self.timeout)
        except requests.Timeout as exc:
            if _endereco_acbr_completo(local):
                return _payload_empresa_acbr(cnpj, local)
            raise ErroAcbr(
                f"Tempo limite ao consultar o CNPJ {cnpj} na BrasilAPI "
                f"({self.timeout:g}s)."
            ) from exc
        except requests.RequestException as exc:
            if _endereco_acbr_completo(local):
                return _payload_empresa_acbr(cnpj, local)
            raise ErroAcbr(
                f"Falha de conexão ao consultar o CNPJ {cnpj} na BrasilAPI: {exc}"
            ) from exc

        corpo = _json_ou_texto(resposta)
        if not resposta.ok:
            if _endereco_acbr_completo(local):
                return _payload_empresa_acbr(cnpj, local)
            detalhe = _detalhe_erro(corpo)
            mensagem = (
                f"BrasilAPI recusou a consulta do CNPJ {cnpj} "
                f"(HTTP {resposta.status_code})"
            )
            if detalhe:
                mensagem += f": {detalhe}"
            raise ErroAcbr(mensagem, status=resposta.status_code, corpo=corpo)
        if not isinstance(corpo, dict):
            if _endereco_acbr_completo(local):
                return _payload_empresa_acbr(cnpj, local)
            raise ErroAcbr(
                "A BrasilAPI devolveu uma resposta inesperada para o CNPJ "
                f"{cnpj}.",
                corpo=corpo,
            )

        # A resposta da Receita tem prioridade, mas campos vazios não apagam o
        # endereço já salvo no banco (resposta parcial não quebra o cadastro).
        dados = _mesclar_dados_empresa(local, corpo)
        return _payload_empresa_acbr(cnpj, dados)

    def consultar_ultimo_nsu(
        self,
        cnpj: str,
        dados_empresa: Optional[Dict[str, Any]] = None,
        dist_nsu: Optional[Any] = None,
    ) -> str:
        """Consulta o cursor atual da fila de Distribuição NF-e sem baixar XMLs.

        Antes da consulta, verifica se a empresa existe na ACBr. Se a API
        responder 404, busca os dados cadastrais do CNPJ na BrasilAPI e cadastra
        a empresa com razão social, e-mail e endereço completos; em seguida,
        prossegue com a Distribuição NF-e. Se a BrasilAPI estiver indisponível,
        usa o endereço já persistido na tabela ``empresas`` (ou o dicionário
        opcional ``dados_empresa``).

        O contrato da ACBr API v3 usa ``POST /distribuicao/nfe`` com
        ``tipo_consulta="dist-nsu"``. O campo ``dist_nsu`` deve ser **igual ao
        ``ultNSU`` devolvido na consulta anterior**. Imediatamente antes do
        POST, o valor é lido diretamente de ``empresas.ultimo_nsu_acbr`` em uma
        sessão curta do banco; loja inexistente, com ``NULL`` ou com ``"0"``
        inicia do começo (``0``). O parâmetro ``dist_nsu`` existe apenas por
        compatibilidade e nunca sobrescreve o cursor persistido.

        Se o banco estiver atrasado e a ACBr recusar o valor informando
        ``"dist_nsu deve ser igual a N"``, o NSU esperado é gravado em
        ``empresas.ultimo_nsu_acbr`` e a consulta é repetida uma única vez com
        esse cursor. Isso recupera lojas cujo último NSU avançou fora desta
        aplicação, sem exigir atualização manual do SQLite.

        A resposta traz ``ultNSU`` (cursor) e, em algumas versões, ``maxNSU``
        (teto da fila). O cursor devolvido é persistido em
        ``empresas.ultimo_nsu_acbr`` para a próxima chamada; quando a resposta
        só tem ``max_nsu``, ele é devolvido sem ser gravado como cursor.

        A autenticação OAuth2 é a mesma de :meth:`autenticar`, reutilizada por
        :meth:`_requisicao` (inclusive o token em cache).

        Args:
            cnpj: CNPJ da empresa, com ou sem máscara.
            dados_empresa: Cadastro local opcional (razão social, endereço...).
                Quando omitido, o cliente tenta ler a tabela ``empresas`` se a
                BrasilAPI falhar.
            dist_nsu: Mantido por compatibilidade. O valor enviado é sempre o
                lido de ``empresas.ultimo_nsu_acbr`` (ou ``0`` quando a loja
                ainda não tem NSU).

        Returns:
            O último NSU devolvido pela ACBr para o CNPJ, como string.

        Raises:
            ValueError: CNPJ fora do formato de 14 dígitos.
            ErroAcbr: falha de autenticação/rede/HTTP, consulta à BrasilAPI,
                cadastro automático recusado ou resposta sem NSU.
        """
        cnpj_limpo = _so_digitos(cnpj)
        if len(cnpj_limpo) != 14:
            raise ValueError("Informe um CNPJ com 14 dígitos.")
        # O helper lê o banco IMEDIATAMENTE antes do POST: o NSU pode ter
        # avançado em um download/automação concorrente desde que o chamador
        # montou a requisição. Falha de banco vira ErroAcbr, nunca 0 silencioso.
        nsu_banco = _ler_nsu_empresa_local(cnpj_limpo)
        nsu_atual = nsu_banco if nsu_banco not in ("", "0") else "0"
        logger.info(
            "ACBr NSU %s: lido do banco=%r; dist_nsu que será enviado=%s",
            cnpj_limpo,
            nsu_banco,
            nsu_atual,
        )

        # Algumas operações da Distribuição NF-e retornam 404 quando a empresa
        # ainda não foi cadastrada na ACBr. Nesse caso, busca os dados na
        # BrasilAPI e cria a empresa antes de consultar a fila.
        try:
            self.consultar_empresa(cnpj_limpo)
        except ErroAcbr as erro:
            if erro.status != 404:
                raise
            dados_basicos = self._dados_cadastro_brasilapi(
                cnpj_limpo, dados_empresa
            )
            try:
                self.cadastrar_empresa(dados_basicos)
            except ErroAcbr as falha:
                raise ErroAcbr(
                    "A empresa não existe na ACBr e o cadastro automático do "
                    f"CNPJ {cnpj_limpo} falhou: {falha}",
                    status=falha.status,
                    corpo=falha.corpo,
                ) from falha

        corpo = {
            "cpf_cnpj": cnpj_limpo,
            "ambiente": self.ambiente_distribuicao,
            "tipo_consulta": "dist-nsu",
            # A ACBr rejeita a consulta quando este valor não é exatamente o
            # `ultNSU` devolvido na chamada anterior. 0 inicia do começo.
            "dist_nsu": int(nsu_atual or 0),
            "ignorar_tempo_espera": False,
        }
        try:
            resposta = self._requisicao("POST", "/distribuicao/nfe", json=corpo)
        except ErroAcbr as erro:
            nsu_esperado = _nsu_esperado_pela_acbr(erro)
            if not nsu_esperado or nsu_esperado == nsu_atual:
                raise ErroAcbr(
                    "Falha ao consultar o último NSU na ACBr API "
                    f"para o CNPJ {cnpj_limpo}: {erro}",
                    status=erro.status,
                    corpo=erro.corpo,
                ) from erro

            logger.warning(
                "ACBr NSU %s: banco tinha %s, mas a ACBr exige %s; "
                "sincronizando empresas.ultimo_nsu_acbr e repetindo o POST.",
                cnpj_limpo,
                nsu_atual,
                nsu_esperado,
            )
            if not _salvar_nsu_empresa_local(cnpj_limpo, nsu_esperado):
                logger.warning(
                    "ACBr NSU %s: não foi possível persistir o cursor %s "
                    "durante a sincronização.",
                    cnpj_limpo,
                    nsu_esperado,
                )
            corpo["dist_nsu"] = int(nsu_esperado)
            nsu_atual = nsu_esperado
            try:
                resposta = self._requisicao("POST", "/distribuicao/nfe", json=corpo)
            except ErroAcbr as segundo_erro:
                raise ErroAcbr(
                    "Falha ao consultar o último NSU na ACBr API "
                    f"para o CNPJ {cnpj_limpo} após sincronizar o cursor "
                    f"para {nsu_esperado}: {segundo_erro}",
                    status=segundo_erro.status,
                    corpo=segundo_erro.corpo,
                ) from segundo_erro

        if not isinstance(resposta, dict):
            raise ErroAcbr(
                "A ACBr API devolveu uma resposta inesperada ao consultar o "
                f"último NSU do CNPJ {cnpj_limpo}.",
                corpo=resposta,
            )

        # `ultNSU` é o cursor que a próxima chamada deve reenviar. Só depois
        # vêm as variações de nome usadas por versões antigas da API.
        nsu_cursor = ""
        for campo in ("ultNSU", "ult_nsu", "ultimoNSU", "ultimo_nsu"):
            nsu_cursor = _nsu_texto(resposta.get(campo))
            if nsu_cursor:
                break
        if nsu_cursor:
            # Persiste o cursor para a próxima consulta/download não reenviar
            # um `dist_nsu` diferente do esperado pela ACBr.
            if not _salvar_nsu_empresa_local(cnpj_limpo, nsu_cursor):
                logger.warning(
                    "ACBr NSU %s: cursor %s recebido, mas não foi possível "
                    "gravar empresas.ultimo_nsu_acbr.",
                    cnpj_limpo,
                    nsu_cursor,
                )
            return nsu_cursor

        # Fallback de diagnóstico: algumas respostas só trazem o teto da fila.
        # Esse valor NÃO é o cursor, então não é gravado como se fosse `ultNSU`.
        for campo in ("max_nsu", "maxNSU"):
            nsu_teto = _nsu_texto(resposta.get(campo))
            if nsu_teto:
                return nsu_teto

        raise ErroAcbr(
            "A ACBr API não informou o último NSU do CNPJ "
            f"{cnpj_limpo} (campos ultNSU/ultimo_nsu/max_nsu ausentes).",
            corpo=resposta,
        )

    def listar_documentos_nfe(
        self,
        cpf_cnpj: str,
        *,
        ambiente: Optional[str] = None,
        top: int = 50,
        skip: int = 0,
        tipo_documento: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Lista documentos já distribuídos (paginado por ``$top``/``$skip``).

        Na ACBr API, ``$inlinecount`` é **booleano** e inclui ``@count`` na
        resposta. A string OData ``allpages`` é recusada pelo backend com
        "Cannot deserialize value ..."; por isso enviamos ``True``.
        """
        cnpj = _so_digitos(cpf_cnpj)
        if len(cnpj) != 14:
            raise ValueError("Informe um CNPJ com 14 dígitos.")
        params: Dict[str, Any] = {
            "cpf_cnpj": cnpj,
            "ambiente": ambiente or self.ambiente_distribuicao,
            "$top": max(1, int(top)),
            "$skip": max(0, int(skip)),
            "$inlinecount": True,
        }
        if tipo_documento:
            params["tipo_documento"] = str(tipo_documento)
        return self._requisicao("GET", "/distribuicao/nfe/documentos", params=params)

    def baixar_xml_documento(self, documento_id: str) -> bytes:
        """Baixa o XML de um documento da Distribuição NF-e."""
        identificador = str(documento_id or "").strip()
        if not identificador:
            raise ValueError("Informe o id do documento.")
        resposta = self._requisicao(
            "GET",
            f"/distribuicao/nfe/documentos/{quote(identificador, safe='')}/xml",
            retornar_resposta=True,
            accept="application/xml",
        )
        return _extrair_xml(resposta.content)

