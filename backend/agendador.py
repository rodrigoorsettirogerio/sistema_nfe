"""Agendador dos relatórios de alerta por e-mail e da sincronização Omie.

Roda dentro do processo da API (thread daemon) e não depende de cron/serviço externo.
São **dois** laços independentes, cada um com sua variável de desligamento:

- **relatórios de alerta**: todo dia às 06:00 (``NFE_ALERTAS_HORA``) o diário,
  segunda-feira o semanal e dia 1º o mensal (``NFE_ALERTAS_AGENDADOR=0`` desliga);
- **sincronização Omie**: todo dia no horário ``NFE_OMIE_HORA`` (padrão 3, fora
  do horário comercial) roda UMA vez por loja que tem credencial Omie gravada,
  com pausa de ``NFE_OMIE_PAUSA_SEGUNDOS`` (padrão 2 s) entre as lojas
  (``NFE_OMIE_AGENDADOR=0`` desliga). A pausa existe porque a API do Omie
  responde **HTTP 425** (consumo indevido) quando as chamadas vão em rajada;
  uma loja sem notas no período não é erro (o cliente traduz o HTTP 500
  "Não existem registros" em lista vazia).

O "enviar"/"sincronizar" é injetado pelo `main.py` (``iniciar(callback)`` /
``iniciar_omie(callback)``): este módulo só decide QUEM e QUANDO, mantendo-se sem
import de `main` (evita import circular). A rotina de envio atualiza os
marcadores (``ultimo_envio_*``) e o cursor **depois** de o SMTP confirmar; por
isso uma falha não perde nota e uma segunda verificação no mesmo dia não reenvia
o mesmo relatório. No Omie o marcador é a própria data local de
``empresas.omie_sync_em`` (sobrevive a reinício da API) mais um controle em
memória para não repetir a tentativa no mesmo dia quando a loja falha.
"""

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from backend.database import SessionLocal
from backend.models import ConfiguracaoAlerta, Empresa, Usuario
from backend.omie_client import sem_registros

logger = logging.getLogger(__name__)

FUSO_BRASILIA = ZoneInfo("America/Sao_Paulo")
HORA_PADRAO = 6
# A thread acorda a cada minuto: barato e suficiente para um horário fixo.
INTERVALO_VERIFICACAO_SEGUNDOS = 60
# Omie: 3h da manhã (fora do horário da loja) e 2 s entre lojas (anti-HTTP 425).
HORA_OMIE_PADRAO = 3
PAUSA_OMIE_PADRAO_SEGUNDOS = 2.0

_thread = None
_parar = threading.Event()
_thread_omie = None
_parar_omie = threading.Event()
# Lojas já tentadas HOJE (CNPJ -> data local). O `omie_sync_em` só é gravado
# quando a sincronização conclui; sem este controle uma loja com erro seria
# chamada de novo a cada minuto durante a hora do agendador.
_tentativas_omie = {}


def agendador_ativo() -> bool:
    """False quando ``NFE_ALERTAS_AGENDADOR=0`` (testes/execução manual)."""
    valor = str(os.getenv("NFE_ALERTAS_AGENDADOR", "1") or "").strip().lower()
    return valor not in ("0", "false", "nao", "não", "off")


def hora_configurada() -> int:
    """Hora local (0-23) do disparo; valor inválido cai no padrão 6."""
    texto = str(os.getenv("NFE_ALERTAS_HORA", "") or "").strip()
    if not texto:
        return HORA_PADRAO
    try:
        hora = int(texto)
    except ValueError:
        logger.warning("NFE_ALERTAS_HORA inválido (%r); usando %s.", texto, HORA_PADRAO)
        return HORA_PADRAO
    return hora if 0 <= hora <= 23 else HORA_PADRAO


def agendador_omie_ativo() -> bool:
    """False quando ``NFE_OMIE_AGENDADOR=0`` (testes/execução manual).

    Separado de ``NFE_ALERTAS_AGENDADOR`` de propósito: desligar os relatórios
    de e-mail não deve impedir a sincronização Omie, e vice-versa.
    """
    valor = str(os.getenv("NFE_OMIE_AGENDADOR", "1") or "").strip().lower()
    return valor not in ("0", "false", "nao", "não", "off", "desligado")


def hora_omie_configurada() -> int:
    """Hora local (0-23) da sincronização Omie; valor inválido cai no padrão 3."""
    texto = str(os.getenv("NFE_OMIE_HORA", "") or "").strip()
    if not texto:
        return HORA_OMIE_PADRAO
    try:
        hora = int(texto)
    except ValueError:
        logger.warning("NFE_OMIE_HORA inválido (%r); usando %s.", texto, HORA_OMIE_PADRAO)
        return HORA_OMIE_PADRAO
    return hora if 0 <= hora <= 23 else HORA_OMIE_PADRAO


def pausa_entre_lojas(valor=None) -> float:
    """Segundos de espera entre duas lojas da sincronização Omie.

    A Omie responde HTTP 425 quando recebe uma rajada de chamadas: a pausa
    (padrão 2 s) dá tempo do limite da API resetar entre uma loja e outra.
    Valor inválido/negativo cai no padrão; ``valor`` explícito serve ao teste
    (``0`` = sem espera).
    """
    if valor is not None:
        try:
            return max(0.0, float(valor))
        except (TypeError, ValueError):
            return PAUSA_OMIE_PADRAO_SEGUNDOS
    texto = str(os.getenv("NFE_OMIE_PAUSA_SEGUNDOS", "") or "").strip()
    if not texto:
        return PAUSA_OMIE_PADRAO_SEGUNDOS
    try:
        return max(0.0, float(texto))
    except ValueError:
        logger.warning(
            "NFE_OMIE_PAUSA_SEGUNDOS inválido (%r); usando %s.",
            texto,
            PAUSA_OMIE_PADRAO_SEGUNDOS,
        )
        return PAUSA_OMIE_PADRAO_SEGUNDOS


def _sincronizado_hoje(marcador, agora: datetime) -> bool:
    """True quando o ``omie_sync_em`` (ISO UTC) cai no dia local de ``agora``.

    ``omie_sync_em`` é gravado em UTC, mas o agendamento é local: converter
    antes de comparar evita que uma sincronização das 22h (Brasília) seja
    contada como "de ontem" (já seria 01h UTC do dia seguinte).
    """
    texto = str(marcador or "").strip()
    if not texto:
        return False
    try:
        quando = datetime.fromisoformat(texto.replace("Z", "+00:00"))
    except ValueError:
        return False
    if quando.tzinfo is None:
        quando = quando.replace(tzinfo=timezone.utc)
    return quando.astimezone(FUSO_BRASILIA).date() == agora.date()


def _mes_fechado(hoje) -> str:
    """'AAAA-MM' do mês que acabou de fechar (usado no marcador do mensal)."""
    return (hoje - timedelta(days=1)).strftime("%Y-%m")


def tipos_devidos(config, agora: datetime) -> list:
    """Tipos de relatório que devem sair neste momento (lista vazia = nada).

    Um tipo só entra quando (a) está ligado na configuração, (b) o alerta da conta
    está ativo, (c) estamos DENTRO da hora configurada e (d) o marcador do período
    ainda não foi gravado — o marcador é atualizado apenas após o envio confirmado.

    A janela de 1 hora (em vez de "hora atual >= hora") evita o disparo fora de hora
    quando a API é reiniciada à tarde: o relatório daquele dia simplesmente não sai e
    as notas continuam no lote do próximo envio (o cursor não avança sozinho).
    """
    if config is None or not bool(getattr(config, "ativo", False)):
        return []
    if agora.hour != hora_configurada():
        return []
    hoje = agora.date()
    tipos = []
    if bool(getattr(config, "alerta_diario", True)):
        if str(getattr(config, "ultimo_envio_diario", "") or "") != hoje.isoformat():
            tipos.append("diario")
    if bool(getattr(config, "alerta_semanal", True)) and hoje.weekday() == 0:
        if str(getattr(config, "ultimo_envio_semanal", "") or "") != hoje.isoformat():
            tipos.append("semanal")
    if bool(getattr(config, "alerta_mensal", True)) and hoje.day == 1:
        if str(getattr(config, "ultimo_envio_mensal", "") or "") != _mes_fechado(hoje):
            tipos.append("mensal")
    return tipos


def executar_ciclo(agora=None, enviar=None) -> list:
    """Verifica as configurações e envia o que estiver devido. Devolve o que rodou.

    `enviar(db, usuario, tipo)` é o callback do `main.py`; ele levanta exceção quando
    o SMTP falha e devolve ``{"enviado": bool, "motivo": str}`` quando roda.
    """
    agora = agora or datetime.now(FUSO_BRASILIA)
    if enviar is None:
        logger.warning("Agendador chamado sem callback de envio; nada a fazer.")
        return []
    db = SessionLocal()
    resultados = []
    try:
        configs = (
            db.query(ConfiguracaoAlerta)
            .filter(ConfiguracaoAlerta.ativo.is_(True))
            .all()
        )
        for config in configs:
            tipos = tipos_devidos(config, agora)
            if not tipos:
                continue
            usuario = (
                db.query(Usuario).filter(Usuario.id == config.usuario_id).first()
            )
            if usuario is None or not str(config.email or "").strip():
                # Sem e-mail não há como entregar; o cliente vê o aviso na tela.
                continue
            for tipo in tipos:
                try:
                    resultado = enviar(db, usuario, tipo) or {}
                except Exception as erro:  # noqa: BLE001 - o agendador nunca cai
                    db.rollback()
                    logger.exception(
                        "Agendador: falha no relatório %s de %s", tipo, usuario.email
                    )
                    resultados.append(
                        {
                            "usuario": usuario.email,
                            "tipo": tipo,
                            "enviado": False,
                            "erro": str(erro)[:300],
                        }
                    )
                    continue
                resultados.append(
                    {"usuario": usuario.email, "tipo": tipo, **resultado}
                )
    finally:
        db.close()
    return resultados


def limpar_tentativas_omie() -> None:
    """Zera o controle em memória de lojas já tentadas hoje (teste/reenvio)."""
    _tentativas_omie.clear()


def executar_ciclo_omie(agora=None, sincronizar=None, pausa=None, tentativas=None):
    """Sincroniza UMA vez por dia cada loja que tem credencial Omie gravada.

    ``sincronizar(db, empresa)`` é o callback do ``main.py`` (mesma rotina da
    rota ``POST /api/omie/sincronizar``). Este laço só decide QUEM e QUANDO:

    - roda apenas dentro da hora ``NFE_OMIE_HORA`` (padrão 3), como o agendador
      de alertas — se a API estiver desligada nesse intervalo, a loja sincroniza
      no dia seguinte ou pelo botão da tela;
    - considera só lojas com ``omie_app_key`` **e** ``omie_app_secret``
      preenchidos (a credencial do ``.env`` não serve para o agendamento: sem a
      credencial na loja não há como saber a qual conta Omie ela pertence);
    - pula a loja cujo ``omie_sync_em`` já é de hoje (horário local) e também a
      que já foi tentada hoje, para uma falha não repetir a cada minuto;
    - espera ``pausa`` segundos entre uma loja e outra (HTTP 425 em rajadas);
    - HTTP 500 "Não existem registros" é **sem dados**, não erro: a loja entra
      no resultado com ``sem_dados: True`` e a sincronização continua.

    Args:
        agora: instante de referência (padrão: agora em Brasília) — testável.
        sincronizar: callback que executa a sincronização da loja.
        pausa: segundos entre lojas (padrão: ``pausa_entre_lojas()``).
        tentativas: dicionário CNPJ -> data local já tentada (padrão: o módulo;
            o teste passa um novo para não herdar estado).

    Returns:
        Lista com o resultado de cada loja tocada nesta rodada.
    """
    agora = agora or datetime.now(FUSO_BRASILIA)
    if sincronizar is None:
        logger.warning("Agendador Omie chamado sem callback; nada a fazer.")
        return []
    if agora.hour != hora_omie_configurada():
        return []
    pausa = pausa_entre_lojas() if pausa is None else pausa_entre_lojas(pausa)
    tentativas = _tentativas_omie if tentativas is None else tentativas

    db = SessionLocal()
    resultados = []
    chamadas = 0
    try:
        lojas = (
            db.query(Empresa)
            .filter(
                Empresa.omie_app_key.isnot(None),
                Empresa.omie_app_key != "",
                Empresa.omie_app_secret.isnot(None),
                Empresa.omie_app_secret != "",
            )
            .order_by(Empresa.id)
            .all()
        )
        for empresa in lojas:
            cnpj = "".join(
                caractere
                for caractere in str(getattr(empresa, "cnpj", "") or "")
                if caractere.isdigit()
            )
            loja = (
                str(getattr(empresa, "nome_loja", "") or "").strip()
                or str(getattr(empresa, "razao_social", "") or "").strip()
                or cnpj
            )
            if _sincronizado_hoje(getattr(empresa, "omie_sync_em", ""), agora):
                resultados.append(
                    {
                        "cnpj": cnpj,
                        "loja": loja,
                        "sincronizado": False,
                        "motivo": "já sincronizada hoje",
                    }
                )
                continue
            if tentativas.get(cnpj) == agora.date():
                resultados.append(
                    {
                        "cnpj": cnpj,
                        "loja": loja,
                        "sincronizado": False,
                        "motivo": "já tentada hoje",
                    }
                )
                continue

            if chamadas and pausa:
                time.sleep(pausa)
            chamadas += 1
            tentativas[cnpj] = agora.date()
            try:
                resultado = sincronizar(db, empresa) or {}
            except Exception as erro:  # noqa: BLE001 - uma loja não derruba as outras
                db.rollback()
                if sem_registros(erro):
                    # Etapa/período sem notas: é o estado normal de loja nova.
                    logger.info(
                        "Agendador Omie: loja %s sem notas no período (%s).",
                        cnpj,
                        erro,
                    )
                    resultados.append(
                        {
                            "cnpj": cnpj,
                            "loja": loja,
                            "sincronizado": True,
                            "sem_dados": True,
                            "motivo": str(erro)[:300],
                        }
                    )
                    continue
                logger.exception("Agendador Omie: falha na loja %s", cnpj)
                resultados.append(
                    {
                        "cnpj": cnpj,
                        "loja": loja,
                        "sincronizado": False,
                        "erro": str(erro)[:300],
                    }
                )
                continue
            resultados.append(
                {
                    "cnpj": cnpj,
                    "loja": loja,
                    "sincronizado": True,
                    "novas": resultado.get("novas"),
                    "itens": resultado.get("itens"),
                    "erros": resultado.get("erros") or [],
                }
            )
    finally:
        db.close()
    return resultados


def _laco(enviar) -> None:
    """Laço da thread: verifica a cada minuto e ignora erro de uma rodada."""
    logger.info(
        "Agendador de alertas iniciado (hora local %s, fuso %s).",
        hora_configurada(),
        FUSO_BRASILIA,
    )
    while not _parar.is_set():
        try:
            resultados = executar_ciclo(enviar=enviar)
            for resultado in resultados:
                logger.info("Agendador: %s", resultado)
        except Exception:  # noqa: BLE001 - a thread não pode morrer
            logger.exception("Agendador de alertas: falha na verificação")
        _parar.wait(INTERVALO_VERIFICACAO_SEGUNDOS)
    logger.info("Agendador de alertas encerrado.")


def _laco_omie(sincronizar) -> None:
    """Laço da thread Omie: verifica a cada minuto e ignora erro de uma rodada."""
    logger.info(
        "Agendador Omie iniciado (hora local %s, pausa %.1fs entre lojas).",
        hora_omie_configurada(),
        pausa_entre_lojas(),
    )
    while not _parar_omie.is_set():
        try:
            resultados = executar_ciclo_omie(sincronizar=sincronizar)
            for resultado in resultados:
                logger.info("Agendador Omie: %s", resultado)
        except Exception:  # noqa: BLE001 - a thread não pode morrer
            logger.exception("Agendador Omie: falha na verificação")
        _parar_omie.wait(INTERVALO_VERIFICACAO_SEGUNDOS)
    logger.info("Agendador Omie encerrado.")


def iniciar(enviar):
    """Sobe a thread daemon (uma única vez). Devolve a thread ou None."""
    global _thread
    if not agendador_ativo():
        logger.info("Agendador de alertas desligado (NFE_ALERTAS_AGENDADOR=0).")
        return None
    if _thread is not None and _thread.is_alive():
        return _thread
    _parar.clear()
    _thread = threading.Thread(
        target=_laco,
        args=(enviar,),
        name="agendador-alertas",
        daemon=True,
    )
    _thread.start()
    return _thread


def iniciar_omie(sincronizar):
    """Sobe a thread daemon da sincronização Omie. Devolve a thread ou None."""
    global _thread_omie
    if not agendador_omie_ativo():
        logger.info("Agendador Omie desligado (NFE_OMIE_AGENDADOR=0).")
        return None
    if _thread_omie is not None and _thread_omie.is_alive():
        return _thread_omie
    _parar_omie.clear()
    _thread_omie = threading.Thread(
        target=_laco_omie,
        args=(sincronizar,),
        name="agendador-omie",
        daemon=True,
    )
    _thread_omie.start()
    return _thread_omie


def parar() -> None:
    """Sinaliza as threads para terminar (teste e shutdown da API)."""
    _parar.set()
    _parar_omie.set()
    tempo = 0.0
    while _thread is not None and _thread.is_alive() and tempo < 2.0:
        time.sleep(0.05)
        tempo += 0.05
    tempo = 0.0
    while _thread_omie is not None and _thread_omie.is_alive() and tempo < 2.0:
        time.sleep(0.05)
        tempo += 0.05

