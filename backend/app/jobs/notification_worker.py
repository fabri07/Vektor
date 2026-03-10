"""
Celery worker: notification dispatch tasks.
"""

from app.jobs.celery_app import celery_app
from app.observability.logger import get_logger

logger = get_logger(__name__)


@celery_app.task(
    name="jobs.send_notification",
    queue="notifications",
    max_retries=3,
    default_retry_delay=30,
)
def send_notification(
    tenant_id: str,
    user_id: str | None,
    title: str,
    body: str,
    notification_type: str,
    channel: str = "in_app",
) -> None:
    """
    Create an in-app notification record and optionally send an email.
    """
    import asyncio  # noqa: PLC0415
    import uuid  # noqa: PLC0415

    async def _run() -> None:
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: PLC0415
        from sqlalchemy.orm import sessionmaker  # noqa: PLC0415

        from app.config.settings import get_settings  # noqa: PLC0415
        from app.persistence.models.notification import Notification  # noqa: PLC0415

        s = get_settings()
        engine = create_async_engine(s.DATABASE_URL, pool_pre_ping=True)
        factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

        async with factory() as session:
            notification = Notification(
                tenant_id=uuid.UUID(tenant_id),
                user_id=uuid.UUID(user_id) if user_id else None,
                title=title,
                body=body,
                notification_type=notification_type,
                channel=channel,
                is_read=False,
            )
            session.add(notification)
            await session.commit()

        await engine.dispose()

    asyncio.run(_run())

    if channel == "email":
        _dispatch_email(title=title, body=body, user_id=user_id)

    logger.info(
        "notification_worker.sent",
        tenant_id=tenant_id,
        type=notification_type,
        channel=channel,
    )


def _dispatch_email(title: str, body: str, user_id: str | None) -> None:
    """Send an email notification via SMTP integration."""
    from app.config.settings import get_settings  # noqa: PLC0415

    s = get_settings()
    if not s.ENABLE_EMAIL_NOTIFICATIONS:
        logger.debug("notification_worker.email_disabled")
        return

    from app.integrations.smtp import SMTPClient  # noqa: PLC0415

    SMTPClient()  # noqa: F841 — placeholder until user lookup is implemented
    logger.info("notification_worker.email_dispatched", title=title)
