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

from app.jobs.celery_app import celery_app
from app.observability.logger import get_logger, log_job

logger = get_logger(__name__)


async def _run() -> dict[str, int]:
    from app.application.services import reread_service  # noqa: PLC0415
    from app.config.settings import get_settings  # noqa: PLC0415
    from app.jobs.ingestion_worker import _build_async_session  # noqa: PLC0415

    engine, factory = _build_async_session(get_settings().DATABASE_URL)
    try:
        async with factory() as session:
            closed = await reread_service.sweep_stale_reread_runs(session)
            await session.commit()
            return closed
    finally:
        await engine.dispose()


@celery_app.task(  # type: ignore[misc]
    name="jobs.sweep_stale_reread_runs",
    queue="ingestion",
    max_retries=0,
    soft_time_limit=60,
    time_limit=90,
)
def sweep_stale_reread_runs() -> None:
    with log_job("jobs.sweep_stale_reread_runs", logger=logger):
        closed = asyncio.run(_run())
        if closed["apply_stuck"] or closed["preview_session_abandoned"]:
            logger.warning("reread.sweep.closed_stale_runs", **closed)
        else:
            logger.info("reread.sweep.clean", **closed)
