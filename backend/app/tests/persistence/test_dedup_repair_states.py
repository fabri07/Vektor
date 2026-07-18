"""Smoke test — F3-T1: CHECK ampliados de repair + tabla tenant_maintenance_locks.

No prueba lógica de negocio (eso es de tasks posteriores del dedup de
productos). Solo confirma que el modelo y el CHECK ampliado en SQLite
aceptan los valores nuevos de la Fase 3.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.maintenance_lock import TenantMaintenanceLock
from app.persistence.models.repair import DataRepairItem, DataRepairRun
from app.persistence.models.tenant import Tenant


async def test_repair_run_accepts_partially_applied_status(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    run = DataRepairRun(
        tenant_id=sample_tenant.tenant_id,
        repair_type="PRODUCT_DEDUP",
        status="PARTIALLY_APPLIED",
        dry_run=False,
    )
    db_session.add(run)
    await db_session.flush()

    assert run.status == "PARTIALLY_APPLIED"


async def test_repair_run_accepts_completed_with_errors_status(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    run = DataRepairRun(
        tenant_id=sample_tenant.tenant_id,
        repair_type="PRODUCT_DEDUP",
        status="COMPLETED_WITH_ERRORS",
        dry_run=False,
    )
    db_session.add(run)
    await db_session.flush()

    assert run.status == "COMPLETED_WITH_ERRORS"


async def test_repair_item_accepts_merge_product_action(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    run = DataRepairRun(
        tenant_id=sample_tenant.tenant_id,
        repair_type="PRODUCT_DEDUP",
        status="RUNNING",
        dry_run=False,
    )
    db_session.add(run)
    await db_session.flush()

    item = DataRepairItem(
        run_id=run.id,
        tenant_id=sample_tenant.tenant_id,
        action="MERGE_PRODUCT",
        confidence="HIGH",
    )
    db_session.add(item)
    await db_session.flush()

    assert item.action == "MERGE_PRODUCT"


async def test_repair_item_accepts_new_dedup_actions(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    run = DataRepairRun(
        tenant_id=sample_tenant.tenant_id,
        repair_type="PRODUCT_DEDUP",
        status="RUNNING",
        dry_run=False,
    )
    db_session.add(run)
    await db_session.flush()

    for action in (
        "DEACTIVATE_DUPLICATE",
        "REPOINT_FK",
        "CONSOLIDATE_BALANCE",
        "DELETE_BALANCE",
    ):
        item = DataRepairItem(
            run_id=run.id,
            tenant_id=sample_tenant.tenant_id,
            action=action,
            confidence="MEDIUM",
        )
        db_session.add(item)
    await db_session.flush()


async def test_tenant_maintenance_lock_insert(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    now = datetime.now(UTC)
    lock = TenantMaintenanceLock(
        tenant_id=sample_tenant.tenant_id,
        lease_id=uuid.uuid4(),
        reason="product_dedup",
        acquired_at=now,
        expires_at=now + timedelta(minutes=30),
        created_by="dedup_script",
    )
    db_session.add(lock)
    await db_session.flush()

    assert lock.id is not None
    assert lock.heartbeat_at is None
