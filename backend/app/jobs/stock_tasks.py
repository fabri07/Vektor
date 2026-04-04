"""Celery tasks for inventory — reacts to domain events."""

import asyncio
import uuid

from celery import shared_task

from app.observability.logger import get_logger

logger = get_logger(__name__)


@shared_task(name="events.stock_decreased", queue="default")
def on_stock_decreased(payload: dict) -> None:
    """
    Reacts to STOCK_DECREASED event emitted by stock_service.decrement_stock().
    Triggers health score recalculation since stock levels affect scoring.
    """
    tenant_id: str = payload.get("tenant_id", "")
    if not tenant_id:
        logger.warning("on_stock_decreased: missing tenant_id", payload=payload)
        return

    from app.jobs.celery_app import celery_app  # noqa: PLC0415
    celery_app.send_task(
        "jobs.trigger_score_recalculation",
        args=[tenant_id, "event:stock_decreased"],
        queue="scores",
    )
    logger.info("on_stock_decreased: score recalculation queued", tenant_id=tenant_id)


@shared_task(name="events.stock_alert_created", queue="default")
def on_stock_alert_created(payload: dict) -> None:
    """
    Reacts to STOCK_ALERT_CREATED event (stockout risk detected).
    Logs the alert and triggers score recalculation.
    """
    tenant_id: str = payload.get("tenant_id", "")
    product_id: str = payload.get("product_id", "")
    current_qty: int = payload.get("current_qty", 0)

    logger.warning(
        "stock_alert_created",
        tenant_id=tenant_id,
        product_id=product_id,
        alert_type=payload.get("alert_type", "unknown"),
        current_qty=current_qty,
    )

    if tenant_id:
        from app.jobs.celery_app import celery_app  # noqa: PLC0415
        celery_app.send_task(
            "jobs.trigger_score_recalculation",
            args=[tenant_id, "event:stock_alert"],
            queue="scores",
        )


@shared_task(name="events.stock_increased", queue="default")
def on_stock_increased(payload: dict) -> None:
    """
    Reacts to STOCK_INCREASED event emitted by stock_service.increment_stock().
    Triggers score recalculation since restocking improves health metrics.
    """
    tenant_id: str = payload.get("tenant_id", "")
    if not tenant_id:
        logger.warning("on_stock_increased: missing tenant_id", payload=payload)
        return

    from app.jobs.celery_app import celery_app  # noqa: PLC0415
    celery_app.send_task(
        "jobs.trigger_score_recalculation",
        args=[tenant_id, "event:stock_increased"],
        queue="scores",
    )
    logger.info("on_stock_increased: score recalculation queued", tenant_id=tenant_id)


@shared_task(name="events.cash_health_updated", queue="default")
def on_cash_health_updated(payload: dict) -> None:
    """
    Reacts to CASH_HEALTH_UPDATED event emitted by AgentCash.
    Triggers health score recalculation.
    """
    tenant_id: str = payload.get("tenant_id", "") or payload.get("business_id", "")
    if not tenant_id:
        logger.warning("on_cash_health_updated: missing tenant_id", payload=payload)
        return

    from app.jobs.celery_app import celery_app  # noqa: PLC0415
    celery_app.send_task(
        "jobs.trigger_score_recalculation",
        args=[tenant_id, "event:cash_health_updated"],
        queue="scores",
    )
    logger.info("on_cash_health_updated: score recalculation queued", tenant_id=tenant_id)


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
