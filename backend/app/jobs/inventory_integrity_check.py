"""Celery task: chequeo periódico de integridad de inventario (fase 2).

Fase 1 (endpoint SUPERADMIN `GET /admin/inventory-integrity/{tenant_id}`) valida el
umbral contra datos reales sin persistir nada. Esta fase 2 corre sola por tenant,
persiste lo que encuentra (Notification a los OWNER activos + DecisionAuditLog) y
NUNCA escribe `products.stock_units` — la corrección la decide un humano, per la
regla de no-invención del proyecto.

Flow
----
1. check_tenant_inventory_integrity(tenant_id) — puramente de lectura.
2. Si hay divergencias: una Notification por OWNER activo (agrupando todos los
   productos divergentes del tenant en un solo mensaje) + un DecisionAuditLog
   (decision_type="INVENTORY_INTEGRITY_DIVERGENCE").
3. Structured log.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from app.jobs.celery_app import celery_app
from app.observability.logger import get_logger, log_job

logger = get_logger(__name__)

_DECISION_TYPE = "INVENTORY_INTEGRITY_DIVERGENCE"
_TRIGGERED_BY = "celery:inventory_integrity_check"


async def _run(tenant_id_str: str) -> None:
    from sqlalchemy import select  # noqa: PLC0415
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: PLC0415
    from sqlalchemy.orm import sessionmaker  # noqa: PLC0415

    from app.application.services.inventory_integrity_service import (  # noqa: PLC0415
        check_tenant_inventory_integrity,
    )
    from app.config.settings import get_settings  # noqa: PLC0415
    from app.persistence.models.audit import DecisionAuditLog  # noqa: PLC0415
    from app.persistence.models.notification import Notification  # noqa: PLC0415
    from app.persistence.models.user import User  # noqa: PLC0415

    s = get_settings()
    tenant_id = uuid.UUID(tenant_id_str)

    engine = create_async_engine(s.DATABASE_URL, pool_pre_ping=True, connect_args=s.pg_connect_args)
    session_factory = sessionmaker(  # type: ignore[call-overload]
        engine, class_=AsyncSession, expire_on_commit=False
    )

    try:
        with log_job("jobs.inventory_integrity_check", tenant_id=tenant_id, logger=logger):
            async with session_factory() as session:
                result = await check_tenant_inventory_integrity(session, tenant_id)
                divergences = result["divergences"]

                if not divergences:
                    logger.info(
                        "inventory_integrity_check.clean",
                        tenant_id=tenant_id_str,
                        checked=result["checked"],
                        skipped_no_anchor=result["skipped_no_anchor"],
                        skipped_complex_ledger=result["skipped_complex_ledger"],
                    )
                    return

                now = datetime.now(UTC)
                body = "\n".join(
                    f"- {d['product_name']}: sistema muestra {d['stock_units']}, "
                    f"esperado {d['stock_esperado']} (diferencia {d['diff']})"
                    for d in divergences
                )
                title = f"Posible inconsistencia de stock en {len(divergences)} producto(s)"

                owner_result = await session.execute(
                    select(User).where(
                        User.tenant_id == tenant_id,
                        User.role_code == "OWNER",
                        User.is_active.is_(True),
                    )
                )
                for owner in owner_result.scalars().all():
                    session.add(
                        Notification(
                            id=uuid.uuid4(),
                            tenant_id=tenant_id,
                            user_id=owner.user_id,
                            title=title,
                            body=body,
                            notification_type=_DECISION_TYPE,
                            channel="in_app",
                            is_read=False,
                            metadata_={"divergences": divergences},
                            created_at=now,
                            updated_at=now,
                        )
                    )

                session.add(
                    DecisionAuditLog(
                        id=uuid.uuid4(),
                        tenant_id=tenant_id,
                        decision_type=_DECISION_TYPE,
                        decision_data={
                            "divergences": divergences,
                            "skipped_no_anchor": result["skipped_no_anchor"],
                            "skipped_complex_ledger": result["skipped_complex_ledger"],
                            "threshold": result["threshold"],
                        },
                        triggered_by=_TRIGGERED_BY,
                        actor_user_id=None,
                        created_at=now,
                    )
                )
                await session.commit()

                logger.info(
                    "inventory_integrity_check.divergences_found",
                    tenant_id=tenant_id_str,
                    divergence_count=len(divergences),
                )
    finally:
        await engine.dispose()


@celery_app.task(  # type: ignore[misc]
    bind=True,
    name="jobs.inventory_integrity_check",
    queue="scores",
    max_retries=3,
    default_retry_delay=60,
    soft_time_limit=90,
    time_limit=120,
)
def inventory_integrity_check(self: Any, tenant_id: str) -> None:
    try:
        asyncio.run(_run(tenant_id))
    except Exception as exc:
        logger.warning(
            "inventory_integrity_check.retry",
            tenant_id=tenant_id,
            attempt=self.request.retries,
            error=str(exc),
        )
        raise self.retry(exc=exc) from exc


@celery_app.task(  # type: ignore[misc]
    name="jobs.inventory_integrity_check_all_tenants",
    queue="scores",
    max_retries=2,
    default_retry_delay=120,
)
def inventory_integrity_check_all_tenants() -> None:
    """Periodic task dispatched by Celery Beat (semanal). Fan-out de un
    inventory_integrity_check por tenant activo."""
    from app.config.settings import get_settings  # noqa: PLC0415

    s = get_settings()

    async def _collect_tenants() -> list[str]:
        from sqlalchemy import select  # noqa: PLC0415
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: PLC0415
        from sqlalchemy.orm import sessionmaker  # noqa: PLC0415

        from app.persistence.models.tenant import Tenant  # noqa: PLC0415

        engine = create_async_engine(
            s.DATABASE_URL, pool_pre_ping=True, connect_args=s.pg_connect_args
        )
        factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)  # type: ignore[call-overload]

        async with factory() as session:
            # Canónico uppercase: la app solo escribe 'ACTIVE'/'TRIAL' (ver deps.py).
            result = await session.execute(
                select(Tenant.tenant_id).where(Tenant.status.in_(["ACTIVE", "TRIAL"]))
            )
            ids = [str(tid) for tid in result.scalars().all()]

        await engine.dispose()
        return ids

    with log_job("jobs.inventory_integrity_check_all_tenants", logger=logger):
        tenant_ids = asyncio.run(_collect_tenants())
        logger.info("inventory_integrity_check.fan_out", tenant_count=len(tenant_ids))

        for tid in tenant_ids:
            inventory_integrity_check.delay(tid)
