"""Tests for SupplierRepository.find_by_name (sqlite in-memory)."""

from datetime import UTC, datetime

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant
from app.persistence.repositories.supplier_repository import SupplierRepository


@pytest_asyncio.fixture
async def suppliers(db_session: AsyncSession, sample_tenant: Tenant) -> list[Supplier]:
    """Crea 2 proveedores activos del tenant principal."""
    norte = Supplier(tenant_id=sample_tenant.tenant_id, name="Distribuidora Norte")
    otro = Supplier(tenant_id=sample_tenant.tenant_id, name="OTRO")
    db_session.add_all([norte, otro])
    await db_session.commit()
    return [norte, otro]


async def test_find_by_name_matches_normalized(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    suppliers: list[Supplier],
) -> None:
    repo = SupplierRepository(db_session)
    found = await repo.find_by_name("distribuidora norte", sample_tenant.tenant_id)
    assert found is not None
    assert found.id == suppliers[0].id
    assert found.name == "Distribuidora Norte"


async def test_find_by_name_missing_returns_none(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    suppliers: list[Supplier],
) -> None:
    repo = SupplierRepository(db_session)
    found = await repo.find_by_name("inexistente", sample_tenant.tenant_id)
    assert found is None


async def test_find_by_name_other_tenant_returns_none(
    db_session: AsyncSession,
    second_tenant: Tenant,
    suppliers: list[Supplier],
) -> None:
    repo = SupplierRepository(db_session)
    found = await repo.find_by_name("distribuidora norte", second_tenant.tenant_id)
    assert found is None


async def test_find_by_name_soft_deleted_returns_none(
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    deleted = Supplier(
        tenant_id=sample_tenant.tenant_id,
        name="Distribuidora Norte",
        deactivated_at=datetime.now(UTC),
    )
    db_session.add(deleted)
    await db_session.commit()

    repo = SupplierRepository(db_session)
    found = await repo.find_by_name("distribuidora norte", sample_tenant.tenant_id)
    assert found is None
