"""Celery task: housekeeping periódico de sesiones/jobs de relectura colgados
(F-RR Fase 5).

Los guards reactivos de ``reread_service`` (``start_background_apply``,
``_expire_stale_preview_sessions``) solo cierran un run colgado cuando
alguien vuelve a tocar ESE archivo/tenant — "Volver a leer" de nuevo, o un
apply nuevo. Si nadie reintenta nunca, un run zombie queda RUNNING o
PREVIEWING para siempre en la auditoría, sin que nada lo note. Esta task
corre ``reread_service.sweep_stale_reread_runs`` sobre TODOS los tenants,
periódicamente, sin depender de que un usuario haga algo.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

from app.jobs.celery_app import celery_app
from app.observability.logger import get_logger, log_job
from app.observability.metrics import record_job_run

logger = get_logger(__name__)

#: El nombre viaja a Celery, al log y a la traza en `job_runs`: una constante
#: para que la consulta operativa no dependa de que tres literales coincidan.
_JOB_NAME = "jobs.sweep_stale_reread_runs"


async def _registrar_fallo(
    factory: Any, started_at: datetime, duration_ms: int, exc: BaseException
) -> None:
    """Deja la traza del fallo en su PROPIA sesión, y nunca tapa el error original.

    Sesión propia porque la del job ya está rota: después de la excepción no se
    puede escribir en esa transacción. Y ``try/except`` alrededor de todo porque
    el escenario que hace fallar al barrido —la base caída— es el mismo que va a
    hacer fallar a este registro: dejar propagar reemplazaría la causa real por
    el error de una operación auxiliar, que es justamente lo que el caller
    necesita ver. Mismo criterio que ``_record_failure`` del worker de ingestión.
    """
    try:
        async with factory() as session:
            await record_job_run(
                session,
                _JOB_NAME,
                started_at=started_at,
                duration_ms=duration_ms,
                success=False,
                error=str(exc),
            )
            await session.commit()
    except Exception:  # noqa: BLE001 — el error original tiene que ganar
        logger.warning("reread.sweep.job_run_not_recorded", job=_JOB_NAME)


async def _run() -> dict[str, int]:
    from app.application.services import reread_service  # noqa: PLC0415
    from app.config.settings import get_settings  # noqa: PLC0415
    from app.jobs.ingestion_worker import _build_async_session  # noqa: PLC0415

    engine, factory = _build_async_session(get_settings().DATABASE_URL)
    started_at = datetime.now(UTC)
    t0 = time.monotonic()
    try:
        try:
            async with factory() as session:
                closed = await reread_service.sweep_stale_reread_runs(session)
                # La traza va en la MISMA transacción que el barrido: si el commit
                # falla, no queda una fila afirmando un trabajo que no se guardó.
                # Y se escribe SIEMPRE, incluso con todos los contadores en cero —
                # sin la ejecución vacía, un Beat caído y un sistema sano se ven
                # exactamente igual desde la base, que es la pregunta que esta
                # traza existe para responder (H16).
                await record_job_run(
                    session,
                    _JOB_NAME,
                    started_at=started_at,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    success=True,
                    counters=dict(closed),
                )
                await session.commit()
                return closed
        except Exception as exc:
            await _registrar_fallo(
                factory, started_at, int((time.monotonic() - t0) * 1000), exc
            )
            raise
    finally:
        await engine.dispose()


@celery_app.task(  # type: ignore[misc]
    name=_JOB_NAME,
    queue="ingestion",
    max_retries=0,
    soft_time_limit=60,
    time_limit=90,
)
def sweep_stale_reread_runs() -> None:
    with log_job(_JOB_NAME, logger=logger):
        closed = asyncio.run(_run())
        if closed["apply_stuck"] or closed["preview_session_abandoned"]:
            logger.warning("reread.sweep.closed_stale_runs", **closed)
        else:
            logger.info("reread.sweep.clean", **closed)
