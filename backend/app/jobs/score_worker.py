"""
Celery worker: health score recalculation tasks.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

from app.jobs.celery_app import celery_app
from app.observability.logger import get_logger, log_job

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = get_logger(__name__)


async def rebuild_all_tenants(factory: Any, tenant_ids: Sequence[uuid.UUID]) -> int:
    """Recalcula el score de cada tenant y devuelve cuántos fallaron.

    Vive a nivel de módulo —y no adentro de ``_run``— para que el aislamiento por
    tenant sea testeable de verdad: un test que reimplementa el cuerpo del job no
    puede detectar que al loop real le falta el ``try/except``.

    El aislamiento es el punto: el recálculo de un tenant NO puede llevarse puesto
    el de los que vienen después. Antes el loop no atrapaba nada, así que un solo
    tenant con datos o configuración rotos dejaba a toda la cola sin score semanal,
    y el fallo se veía como un retry de Celery sin decir de qué tenant era.
    """
    from app.application.services.health_score_service import (
        HealthScoreService,  # noqa: PLC0415
    )

    fallidos = 0
    for tid in tenant_ids:
        try:
            async with factory() as session:
                svc = HealthScoreService(session)
                await svc.recalculate_for_tenant(
                    tenant_id=tid,
                    triggered_by="scheduled_rebuild",
                )
                await session.commit()
        except Exception:
            fallidos += 1
            logger.exception("score_worker.rebuild_weekly.tenant_failed", tenant_id=str(tid))

    if fallidos:
        logger.warning(
            "score_worker.rebuild_weekly.partial",
            failed=fallidos,
            total=len(tenant_ids),
        )
    return fallidos


@celery_app.task(  # type: ignore[misc]
    name="jobs.rebuild_weekly_history",
    queue="scores",
    max_retries=3,
    default_retry_delay=60,
)
def rebuild_weekly_history() -> None:
    """
    Periodic task: rebuild WeeklyScoreHistory for all active tenants.
    Runs daily via Celery Beat.
    """
    from app.config.settings import get_settings  # noqa: PLC0415

    s = get_settings()

    async def _run() -> None:
        from sqlalchemy import select  # noqa: PLC0415
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: PLC0415
        from sqlalchemy.orm import sessionmaker  # noqa: PLC0415

        from app.persistence.models.tenant import Tenant  # noqa: PLC0415

        engine = create_async_engine(
            s.DATABASE_URL,
            pool_pre_ping=True,
            connect_args=s.pg_connect_args,
        )
        factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)  # type: ignore[call-overload]

        async with factory() as session:
            result = await session.execute(
                select(Tenant.tenant_id).where(Tenant.status.in_(["active", "trial"]))
            )
            tenant_ids = result.scalars().all()

        logger.info("score_worker.rebuild_weekly", tenant_count=len(tenant_ids))

        await rebuild_all_tenants(factory, tenant_ids)

        await engine.dispose()

    with log_job("jobs.rebuild_weekly_history", logger=logger):
        asyncio.run(_run())


@celery_app.task(  # type: ignore[misc]
    name="jobs.trigger_score_recalculation",
    queue="scores",
    max_retries=3,
    default_retry_delay=30,
)
def trigger_score_recalculation(tenant_id: str, snapshot_id: str) -> None:
    """
    On-demand task: recalculate health score for a single tenant.
    Triggered after onboarding or any business data write.
    """
    from app.config.settings import get_settings  # noqa: PLC0415

    s = get_settings()

    async def _run() -> None:
        import uuid as _uuid  # noqa: PLC0415

        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: PLC0415
        from sqlalchemy.orm import sessionmaker  # noqa: PLC0415

        engine = create_async_engine(
            s.DATABASE_URL,
            pool_pre_ping=True,
            connect_args=s.pg_connect_args,
        )
        factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)  # type: ignore[call-overload]

        async with factory() as session:
            from app.application.services.health_score_service import (  # noqa: PLC0415
                HealthScoreService,
            )

            svc = HealthScoreService(session)
            await svc.recalculate_for_tenant(
                tenant_id=_uuid.UUID(tenant_id),
                triggered_by=f"onboarding:{snapshot_id}",
            )
            await session.commit()

        await engine.dispose()

    with log_job("jobs.trigger_score_recalculation", tenant_id=tenant_id, logger=logger):
        asyncio.run(_run())
