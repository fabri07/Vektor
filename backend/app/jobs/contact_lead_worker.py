"""Celery worker: notificación por email de leads de contacto.

Desacoplado de la creación del lead (regla cardinal: un fallo del correo nunca
pierde el lead ni afecta la respuesta al visitante). Reintenta ante fallos y
persiste el resultado en ``contact_leads.email_notification_status``.
"""

from __future__ import annotations

from typing import Any

from app.jobs.celery_app import celery_app
from app.observability.logger import get_logger, log_job

logger = get_logger(__name__)


@celery_app.task(  # type: ignore[misc]
    bind=True,
    name="jobs.notify_contact_lead",
    queue="notifications",
    max_retries=3,
    default_retry_delay=30,
    soft_time_limit=45,
    time_limit=60,
)
def notify_contact_lead(self: Any, lead_id: str) -> None:
    """Envía el email de aviso del lead y actualiza su estado de notificación."""
    import asyncio  # noqa: PLC0415
    import uuid  # noqa: PLC0415

    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: PLC0415
    from sqlalchemy.orm import sessionmaker  # noqa: PLC0415

    from app.application.services.contact_lead_service import build_lead_email  # noqa: PLC0415
    from app.config.settings import get_settings  # noqa: PLC0415
    from app.domain.contact_lead import EmailNotificationStatus  # noqa: PLC0415
    from app.integrations.smtp import SMTPClient  # noqa: PLC0415
    from app.persistence.models.contact_lead import ContactLead  # noqa: PLC0415

    s = get_settings()

    async def _load() -> tuple[str, str, str] | None:
        engine = create_async_engine(
            s.DATABASE_URL, pool_pre_ping=True, connect_args=s.pg_connect_args
        )
        factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)  # type: ignore[call-overload]
        try:
            async with factory() as session:
                lead = await session.get(ContactLead, uuid.UUID(lead_id))
                if lead is None:
                    logger.warning("contact_lead.notify_missing", lead_id=lead_id)
                    return None
                return build_lead_email(lead)
        finally:
            await engine.dispose()

    async def _mark(status: EmailNotificationStatus) -> None:
        engine = create_async_engine(
            s.DATABASE_URL, pool_pre_ping=True, connect_args=s.pg_connect_args
        )
        factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)  # type: ignore[call-overload]
        try:
            async with factory() as session:
                lead = await session.get(ContactLead, uuid.UUID(lead_id))
                if lead is not None:
                    lead.email_notification_status = status.value
                    await session.commit()
        finally:
            await engine.dispose()

    with log_job("jobs.notify_contact_lead", logger=logger):
        recipient = s.contact_lead_recipient
        if not recipient:
            # Producción sin CONTACT_LEAD_EMAIL: no perdemos el lead, marcamos
            # failed y logueamos para que se configure la casilla.
            logger.error("contact_lead.no_recipient", lead_id=lead_id)
            asyncio.run(_mark(EmailNotificationStatus.FAILED))
            return

        built = asyncio.run(_load())
        if built is None:
            return
        subject, html, text = built

        try:
            SMTPClient().send(recipient, subject, html, text, raise_on_error=True)
        except Exception as exc:  # noqa: BLE001 — reintenta; si se agota, marca failed
            logger.warning("contact_lead.email_failed", lead_id=lead_id, error=str(exc))
            try:
                raise self.retry(exc=exc)
            except self.MaxRetriesExceededError:
                asyncio.run(_mark(EmailNotificationStatus.FAILED))
                return

        asyncio.run(_mark(EmailNotificationStatus.SENT))
        logger.info("contact_lead.email_sent", lead_id=lead_id)
