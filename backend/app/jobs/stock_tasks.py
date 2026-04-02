"""Celery tasks for inventory — reacts to domain events."""

import asyncio
import uuid

from celery import shared_task

from app.observability.logger import get_logger

logger = get_logger(__name__)


@shared_task(name="events.sale_recorded", queue="default")
def on_sale_recorded(payload: dict) -> None:
    """
    Reacts to SALE_RECORDED event — decrements stock for every sold item.
    Runs synchronously via asyncio.run() since Celery workers have no event loop.
    """
    sale_id: str = payload.get("sale_id", "")
    tenant_id_str: str = payload.get("tenant_id", "")

    if not sale_id or not tenant_id_str:
        logger.warning("on_sale_recorded: missing sale_id or tenant_id", payload=payload)
        return

    asyncio.run(_handle_sale_recorded(sale_id, tenant_id_str))


async def _handle_sale_recorded(sale_id: str, tenant_id_str: str) -> None:
    from app.application.agents.stock.agent import AgentStock
    from app.persistence.db.session import async_session_factory

    tenant_id = uuid.UUID(tenant_id_str)
    agent = AgentStock()

    async with async_session_factory() as db:
        try:
            await agent.on_sale_recorded(sale_id=sale_id, tenant_id=str(tenant_id), db=db)
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception(
                "on_sale_recorded failed",
                sale_id=sale_id,
                tenant_id=tenant_id_str,
            )
            raise
