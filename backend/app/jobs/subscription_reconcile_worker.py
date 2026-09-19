"""Celery task: conciliación periódica de reservas de cupo colgadas.

La política (con qué evidencia se confirma, se libera o no se toca) vive en
``subscription_reconciliation``; esto sólo la corre sobre todos los tenants.

**La autorización no depende de este job.** Una reserva colgada sólo le resta
cupo a su cliente hasta que se cierre; el acceso y el consumo se deciden en
caliente. Y Beat no está desplegado como servicio todavía: mientras tanto, lo
mismo se corre a mano con ``scripts/subscriptions.py reconcile-reservations``.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

from app.jobs.celery_app import celery_app
from app.observability.logger import get_logger, log_job
from app.observability.metrics import record_job_run

logger = get_logger(__name__)

_JOB_NAME = "jobs.reconcile_subscription_reservations"


async def _run() -> dict[str, int]:
    from app.application.services import subscription_reconciliation as rec  # noqa: PLC0415
    from app.config.settings import get_settings  # noqa: PLC0415
    from app.jobs.ingestion_worker import _build_async_session  # noqa: PLC0415

    engine, factory = _build_async_session(get_settings().DATABASE_URL)
    started_at = datetime.now(UTC)
    t0 = time.monotonic()
    try:
        async with factory() as session:
            resultado = await rec.reconcile(session, apply=True)
            contadores = {
                accion: resultado.contar(accion)
                for accion in (rec.CONFIRMAR, rec.LIBERAR, rec.NO_TOCAR, rec.AMBIGUA)
            }
            # Se registra SIEMPRE, también en cero: sin la corrida vacía, un Beat
            # caído y un sistema sano se ven iguales desde la base.
            await record_job_run(
                session,
                _JOB_NAME,
                started_at=started_at,
                duration_ms=int((time.monotonic() - t0) * 1000),
                success=True,
                counters=contadores,
            )
            await session.commit()
            return contadores
    finally:
        await engine.dispose()


@celery_app.task(  # type: ignore[misc]
    name=_JOB_NAME,
    queue="scores",
    max_retries=0,
    soft_time_limit=120,
    time_limit=180,
)
def reconcile_subscription_reservations() -> None:
    with log_job(_JOB_NAME, logger=logger):
        contadores = asyncio.run(_run())
        logger.info("subscription.reservations.reconciled", **contadores)
