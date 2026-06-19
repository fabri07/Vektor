"""Celery worker: apply de relectura (REREAD_FILE) en background.

El apply de un libro de compras grande inserta miles de filas (gastos + productos
+ movimientos + auditoría) y puede tardar minutos — demasiado para un request HTTP
(timeout → el usuario reintenta → duplicados). Esta task lo corre fuera del request:
el endpoint crea el ``DataRepairRun`` (status RUNNING) y encola; la task ejecuta
``apply_reread`` reusando ese run y deja status APPLIED/FAILED. El frontend hace
polling del estado.

Idempotente ante re-entrega (``task_acks_late``): si el run ya no está RUNNING, no
hace nada. Un crash a mitad deja la transacción sin commitear (rollback) → el
re-run arranca limpio, sin duplicar.
"""

from __future__ import annotations

import asyncio
import uuid as _uuid
from datetime import UTC, datetime
from typing import Any

from app.jobs.celery_app import celery_app
from app.jobs.ingestion_worker import _build_async_session
from app.observability.logger import bind_request_context, get_logger

logger = get_logger(__name__)


@celery_app.task(  # type: ignore[misc]
    name="jobs.reread_apply",
    queue="ingestion",
    max_retries=0,
    soft_time_limit=270,
    time_limit=300,
)
def reread_apply(run_id: str, file_id: str, tenant_id: str) -> None:
    """Ejecuta ``reread_service.apply_reread`` sobre un run ya creado."""

    async def _run() -> dict[str, Any]:
        from app.application.services import reread_service  # noqa: PLC0415
        from app.config.settings import get_settings  # noqa: PLC0415
        from app.persistence.models.repair import DataRepairRun  # noqa: PLC0415

        bind_request_context(tenant_id=tenant_id)
        engine, factory = _build_async_session(get_settings().DATABASE_URL)
        try:
            async with factory() as session:
                run = await session.get(DataRepairRun, _uuid.UUID(run_id))
                if run is None:
                    logger.warning("reread.apply.run_missing", run_id=run_id)
                    return {"status": "MISSING"}
                # Idempotencia ante re-entrega: si ya no está RUNNING, no re-aplicar.
                if run.status != "RUNNING":
                    logger.info(
                        "reread.apply.skip_not_running", run_id=run_id, status=run.status
                    )
                    return {"status": run.status}
                try:
                    result = await reread_service.apply_reread(
                        session,
                        _uuid.UUID(file_id),
                        _uuid.UUID(tenant_id),
                        run=run,
                    )
                    await session.commit()
                    logger.info(
                        "reread.apply.done",
                        run_id=run_id,
                        file_id=file_id,
                        voided=result.voided,
                        inserted=result.inserted,
                    )
                    return {"status": "APPLIED", "voided": result.voided}
                except Exception as exc:  # noqa: BLE001
                    await session.rollback()
                    logger.error(
                        "reread.apply.failed", run_id=run_id, error=str(exc)
                    )
                    # Marcar FAILED en una transacción nueva (la anterior se revirtió).
                    async with factory() as s2:
                        r = await s2.get(DataRepairRun, _uuid.UUID(run_id))
                        if r is not None and r.status == "RUNNING":
                            r.status = "FAILED"
                            r.completed_at = datetime.now(UTC)
                            details = dict(r.details_json or {})
                            details["error"] = str(exc)[:500]
                            r.details_json = details
                            await s2.commit()
                    return {"status": "FAILED", "error": str(exc)[:200]}
        finally:
            await engine.dispose()

    asyncio.run(_run())
