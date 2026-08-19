"""Diagnóstico read-only de la salud operativa de la cola de relectura.

El apply de una relectura corre en background (Celery, cola ``ingestion``).
Encolar una tarea con ``.delay()`` siempre devuelve éxito aunque no haya ningún
worker escuchando esa cola — el broker simplemente la deja esperando. Sin este
chequeo, un ``DataRepairRun`` queda en ``QUEUED`` para siempre, indistinguible
de "se está por tomar en cualquier momento" (incidente real: cuenta ASTERIA,
2026-08, dos runs exactamente en ese estado, sin que ningún worker los tomara
nunca).

Este módulo nunca bloquea ni escribe nada — solo reporta lo que puede
verificar desde el backend, siguiendo el mismo patrón que
``mcp_diagnostics_service.py`` (cada chequeo captura su propio error y se
reporta como check fallido, nunca propaga la excepción).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.observability.logger import get_logger
from app.persistence.models.repair import DataRepairRun

logger = get_logger(__name__)

_INGESTION_QUEUE = "ingestion"
_REPAIR_TYPE_REREAD = "REREAD_FILE"
# Umbral de diagnóstico, DELIBERADAMENTE más corto que el umbral de 15 min que
# usa el guard anti-duplicado de `reread_service._STALE_RUNNING_AFTER_SECONDS`:
# ese umbral decide cuándo dejar de BLOQUEAR una relectura nueva; este decide
# cuándo vale la pena AVISAR que nadie está consumiendo la cola — mucho antes.
_QUEUED_TOO_LONG_SECONDS = 2 * 60


def _check(name: str, ok: bool, severity: str, detail: str) -> dict[str, Any]:
    return {"check": name, "ok": ok, "severity": severity, "detail": detail}


def _inspect_celery(timeout: float) -> tuple[dict[str, Any], str | None]:
    """Bloqueante (publica al broker y espera respuestas hasta ``timeout``) —
    llamar vía ``asyncio.to_thread`` para no trabar el event loop.

    UNA sola consulta (``active_queues()``), a propósito: antes se hacían dos
    broadcasts independientes (``ping()`` + ``active_queues()``), cada uno
    sujeto al mismo ``timeout`` por separado. Bajo latencia normal del broker
    era posible que ``ping()`` respondiera a tiempo pero ``active_queues()``
    (una consulta más pesada — el worker tiene que introspeccionar sus
    bindings) no llegara a responder dentro de la ventana corta del chequeo
    pre-apply, dando `{}` — eso se interpretaba como "no hay workers" y
    bloqueaba un apply sobre un worker sano. ``active_queues()`` sola ya
    prueba vida (si un worker responde, aparece como key del dict) y qué
    colas escucha, en un solo round-trip — no hace falta el ping aparte.
    Devuelve ``({}, None)`` si nadie respondió dentro del timeout: eso es
    INDETERMINADO (broker lento/nadie escuchando), nunca una prueba de que no
    hay workers — el caller decide qué hacer con esa ambigüedad."""
    try:
        from app.jobs.celery_app import celery_app  # noqa: PLC0415

        insp = celery_app.control.inspect(timeout=timeout)
        active_queues = insp.active_queues() or {}
        return active_queues, None
    except Exception as exc:  # noqa: BLE001 — diagnóstico nunca rompe
        return {}, str(exc)


def _evaluate_ingestion_queue(active_queues: dict[str, Any]) -> tuple[bool, str]:
    """Solo se llama con ``active_queues`` NO vacío (al menos un worker
    respondió) — decide si alguno de los que respondieron escucha
    `ingestion`. Un dict vacío es responsabilidad del caller (indeterminado,
    no "ninguno la escucha")."""
    for worker_name, queues in active_queues.items():
        names = {q.get("name") for q in (queues or [])}
        if _INGESTION_QUEUE in names:
            return True, f"'{worker_name}' escucha la cola '{_INGESTION_QUEUE}'."
    return (
        False,
        f"Ningún worker respondiendo tiene la cola '{_INGESTION_QUEUE}' entre sus "
        "colas activas — revisar la variable CELERY_QUEUES del proceso worker "
        "(scripts/start_worker.sh) o si el proceso worker está corriendo.",
    )


async def _count_stuck_in_queue(session: AsyncSession) -> int:
    # Solo QUEUED — un run en APPLYING ya fue tomado por un worker y está
    # siendo procesado, no es huérfano de cola.
    result = await session.execute(
        select(DataRepairRun).where(
            DataRepairRun.repair_type == _REPAIR_TYPE_REREAD,
            DataRepairRun.status == "QUEUED",
        )
    )
    now = datetime.now(UTC)
    stuck = 0
    for r in result.scalars().all():
        created = r.created_at
        if created is not None and created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        age = (now - created).total_seconds() if created is not None else 0.0
        if age >= _QUEUED_TOO_LONG_SECONDS:
            stuck += 1
    return stuck


# Corto a propósito: en el camino feliz un worker real responde en
# milisegundos; este valor acota cuánto se espera cuando NADIE responde
# (broker sin consumidores), que es el caso que puede agregar latencia visible
# a un endpoint sincrónico — un timeout largo acá sería peor que el chequeo.
_PRE_APPLY_CHECK_TIMEOUT_SECONDS = 0.3


async def check_ingestion_workers_available() -> bool | None:
    """Chequeo liviano, best-effort, para usar ANTES de encolar un apply.

    Devuelve ``True``/``False`` SOLO cuando al menos un worker respondió y se
    pudo determinar con certeza si escucha `ingestion`; ``None`` en cualquier
    caso indeterminado — el chequeo falló, o nadie respondió dentro del
    timeout corto (broker lento, no necesariamente "no hay workers"). El
    caller debe encolar igual ante `None` (fail-open): un chequeo de salud
    que bloquea el flujo entero por su propia ambigüedad sería peor que no
    tenerlo.
    """
    try:
        active_queues, inspect_error = await asyncio.to_thread(
            _inspect_celery, _PRE_APPLY_CHECK_TIMEOUT_SECONDS
        )
    except Exception:  # noqa: BLE001 — fail-open ante cualquier fallo del chequeo
        return None
    if inspect_error is not None or not active_queues:
        return None
    ok, _detail = _evaluate_ingestion_queue(active_queues)
    return ok


async def run_reread_diagnostics(session: AsyncSession) -> dict[str, Any]:
    """Corre los chequeos de salud del worker/cola de relectura.

    Devuelve ``{overall_ok, checks[]}``. ``overall_ok`` es False si algún check
    de severidad ``error`` falla. Nunca lanza.
    """
    checks: list[dict[str, Any]] = []

    active_queues, inspect_error = await asyncio.to_thread(_inspect_celery, 2.0)

    if inspect_error is not None:
        checks.append(
            _check(
                "workers_responding",
                False,
                "error",
                f"No se pudo consultar el broker/workers de Celery: {inspect_error}.",
            )
        )
    else:
        responding_ok = bool(active_queues)
        checks.append(
            _check(
                "workers_responding",
                responding_ok,
                "error",
                f"{len(active_queues)} worker(s) respondieron: "
                f"{', '.join(active_queues)}."
                if responding_ok
                else "Ningún worker de Celery respondió — el proceso "
                "vektor-worker puede estar caído o inalcanzable.",
            )
        )
        if responding_ok:
            queue_ok, queue_detail = _evaluate_ingestion_queue(active_queues)
            checks.append(_check("ingestion_queue_consumed", queue_ok, "error", queue_detail))

    stuck = await _count_stuck_in_queue(session)
    stuck_ok = stuck == 0
    checks.append(
        _check(
            "no_stale_queued_runs",
            stuck_ok,
            "warning",
            "Sin relecturas encoladas hace más de "
            f"{_QUEUED_TOO_LONG_SECONDS // 60} min sin tomar."
            if stuck_ok
            else f"{stuck} relectura(s) llevan más de "
            f"{_QUEUED_TOO_LONG_SECONDS // 60} min encoladas sin que ningún worker "
            "las tomara — señal de que la cola 'ingestion' no se está consumiendo "
            "(mismo síntoma detectado en la cuenta ASTERIA).",
        )
    )

    overall_ok = all(c["ok"] for c in checks if c["severity"] == "error")
    if not overall_ok:
        logger.error(
            "reread_diagnostics.unhealthy",
            checks=[c for c in checks if c["severity"] == "error" and not c["ok"]],
        )
    return {"overall_ok": overall_ok, "checks": checks}
