"""Tests de MaintenanceLockService — lease por tenant + advisory locks (F3-T2).

Corridos contra SQLite in-memory (single-thread): el comportamiento real de
bloqueo shared/exclusive de los advisory locks de Postgres se cubre en un
test de integración PostgreSQL en una tarea posterior — acá solo se verifica
que las funciones ``acquire_write_lock_shared``/``acquire_maintenance_lock_exclusive``
no rompen en dialectos sin soporte (no-op documentado).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import maintenance_lock_service as svc
from app.persistence.models.maintenance_lock import TenantMaintenanceLock
from app.persistence.models.tenant import Tenant

_REASON = "product_dedup"
_CREATED_BY = "dedup_script"


async def test_acquire_on_free_tenant_returns_lease_and_locks(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    lease_id = await svc.acquire(
        db_session, sample_tenant.tenant_id, _REASON, ttl_seconds=1800, created_by=_CREATED_BY
    )

    assert lease_id is not None
    assert isinstance(lease_id, uuid.UUID)
    assert await svc.is_locked(db_session, sample_tenant.tenant_id) is True


async def test_second_acquire_on_live_lease_does_not_steal(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    first = await svc.acquire(
        db_session, sample_tenant.tenant_id, _REASON, ttl_seconds=1800, created_by=_CREATED_BY
    )
    assert first is not None

    second = await svc.acquire(
        db_session, sample_tenant.tenant_id, _REASON, ttl_seconds=1800, created_by=_CREATED_BY
    )

    assert second is None


async def test_acquire_steals_expired_lease(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    first = await svc.acquire(
        db_session, sample_tenant.tenant_id, _REASON, ttl_seconds=1800, created_by=_CREATED_BY
    )
    assert first is not None

    # Forzamos expires_at al pasado con un UPDATE directo (simula lease vencido).
    past = datetime.now(UTC) - timedelta(minutes=5)
    await db_session.execute(
        update(TenantMaintenanceLock)
        .where(TenantMaintenanceLock.tenant_id == sample_tenant.tenant_id)
        .values(expires_at=past)
    )
    await db_session.flush()

    second = await svc.acquire(
        db_session, sample_tenant.tenant_id, _REASON, ttl_seconds=1800, created_by=_CREATED_BY
    )

    assert second is not None
    assert second != first


async def test_renew_with_correct_lease_extends_expiry(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    lease_id = await svc.acquire(
        db_session, sample_tenant.tenant_id, _REASON, ttl_seconds=60, created_by=_CREATED_BY
    )
    assert lease_id is not None

    renewed = await svc.renew(db_session, lease_id, ttl_seconds=3600)

    assert renewed is True


async def test_renew_with_wrong_lease_returns_false(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    lease_id = await svc.acquire(
        db_session, sample_tenant.tenant_id, _REASON, ttl_seconds=60, created_by=_CREATED_BY
    )
    assert lease_id is not None

    renewed = await svc.renew(db_session, uuid.uuid4(), ttl_seconds=3600)

    assert renewed is False


async def test_release_with_correct_lease_frees_tenant(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    lease_id = await svc.acquire(
        db_session, sample_tenant.tenant_id, _REASON, ttl_seconds=1800, created_by=_CREATED_BY
    )
    assert lease_id is not None

    released = await svc.release(db_session, lease_id)

    assert released is True
    assert await svc.is_locked(db_session, sample_tenant.tenant_id) is False


async def test_release_with_unknown_lease_returns_false(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    released = await svc.release(db_session, uuid.uuid4())

    assert released is False


async def test_is_locked_false_when_no_lease(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    assert await svc.is_locked(db_session, sample_tenant.tenant_id) is False


async def test_advisory_lock_helpers_are_noop_on_sqlite(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    # No deben lanzar en SQLite — el lock real solo existe en Postgres.
    await svc.acquire_write_lock_shared(db_session, sample_tenant.tenant_id)
    await svc.acquire_maintenance_lock_exclusive(db_session, sample_tenant.tenant_id)
