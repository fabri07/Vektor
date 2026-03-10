"""
Celery worker: health score recalculation tasks.
"""

import asyncio

from app.jobs.celery_app import celery_app
from app.observability.logger import get_logger

logger = get_logger(__name__)


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

        engine = create_async_engine(s.DATABASE_URL, pool_pre_ping=True)
        factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)  # type: ignore[call-overload]

        async with factory() as session:
            result = await session.execute(
                select(Tenant.id).where(Tenant.status.in_(["active", "trial"]))
            )
            tenant_ids = result.scalars().all()

        logger.info("score_worker.rebuild_weekly", tenant_count=len(tenant_ids))

        for tid in tenant_ids:
            async with factory() as session:
                from app.application.services.health_score_service import (
                    HealthScoreService,  # noqa: PLC0415
                )
                svc = HealthScoreService(session)
                await svc.recalculate_for_tenant(
                    tenant_id=tid,
                    triggered_by="scheduled_rebuild",
                )
                await session.commit()

        await engine.dispose()

    asyncio.run(_run())
