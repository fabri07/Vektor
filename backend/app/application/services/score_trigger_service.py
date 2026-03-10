"""
Score trigger service — thin shim that dispatches Celery tasks.
Imported by API endpoints to schedule async recalculations.
"""

from app.jobs.celery_app import celery_app


@celery_app.task(name="jobs.trigger_score_recalculation", queue="scores")
def trigger_score_recalculation(tenant_id: str, triggered_by: str) -> None:
    """
    Celery task entrypoint.
    Delegates to HealthScoreService inside a sync DB session.
    """
    import asyncio  # noqa: PLC0415
    import uuid  # noqa: PLC0415

    from sqlalchemy import create_engine  # noqa: PLC0415
    from sqlalchemy.orm import Session  # noqa: PLC0415

    from app.config.settings import get_settings  # noqa: PLC0415
    from app.observability.logger import get_logger  # noqa: PLC0415

    log = get_logger(__name__)
    s = get_settings()

    # Use sync engine for Celery (psycopg2)
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession  # noqa: PLC0415
    from sqlalchemy.orm import sessionmaker  # noqa: PLC0415

    async def _run() -> None:
        engine = create_async_engine(s.DATABASE_URL, pool_pre_ping=True)
        async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with async_session() as session:
            from app.application.services.health_score_service import HealthScoreService  # noqa: PLC0415
            svc = HealthScoreService(session)
            await svc.recalculate_for_tenant(
                tenant_id=uuid.UUID(tenant_id),
                triggered_by=triggered_by,
            )
            await session.commit()
        await engine.dispose()

    asyncio.run(_run())
    log.info("score_trigger.complete", tenant_id=tenant_id, triggered_by=triggered_by)
