"""Agendador dos relatórios de alerta por e-mail.

Roda dentro do processo da API (thread daemon) e não depende de cron/serviço externo:

- **todo dia às 06:00** (``NFE_ALERTAS_HORA``): relatório diário com as notas
  importadas desde o último envio;
- **segunda-feira às 06:00**: relatório semanal da semana anterior;
- **dia 1º às 06:00**: relatório mensal do mês anterior fechado.

O "enviar" é injetado pelo `main.py` (``iniciar(callback)``): este módulo só decide
QUEM e QUANDO, mantendo-se sem import de `main` (evita import circular). A rotina de
envio atualiza os marcadores (`ultimo_envio_*`) e o cursor **depois** de o SMTP
confirmar; por isso uma falha não perde nota e uma segunda verificação no mesmo dia
não reenvia o mesmo relatório.

``NFE_ALERTAS_AGENDADOR=0`` desliga o fio (ambiente de teste); ``NFE_ALERTAS_HORA``
aceita 0-23 (padrão 6).
"""

import logging
import os
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from backend.database import SessionLocal
from backend.models import ConfiguracaoAlerta, Usuario

logger = logging.getLogger(__name__)

FUSO_BRASILIA = ZoneInfo("America/Sao_Paulo")
HORA_PADRAO = 6
# A thread acorda a cada minuto: barato e suficiente para um horário fixo.
INTERVALO_VERIFICACAO_SEGUNDOS = 60

_thread = None
_parar = threading.Event()


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


def parar() -> None:
    """Sinaliza a thread para terminar (usado em teste e no shutdown)."""
    _parar.set()
    tempo = 0.0
    while _thread is not None and _thread.is_alive() and tempo < 2.0:
        time.sleep(0.05)
        tempo += 0.05

