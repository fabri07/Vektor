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


# ── list_by_tenant: marcas colapsadas ocultas incluso con include_inactive ────


async def test_list_include_inactive_excludes_brand_collapsed_string_flag(
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    collapsed = Supplier(
        tenant_id=sample_tenant.tenant_id,
        name="Marca Colapsada",
        deactivated_at=datetime.now(UTC),
        custom_fields={"_brand_collapsed": "true"},
    )
    normal_baja = Supplier(
        tenant_id=sample_tenant.tenant_id,
        name="Proveedor Real de Baja",
        deactivated_at=datetime.now(UTC),
    )
    db_session.add_all([collapsed, normal_baja])
    await db_session.commit()

    repo = SupplierRepository(db_session)
    listed = await repo.list_by_tenant(sample_tenant.tenant_id, include_inactive=True)
    ids = {s.id for s in listed}
    assert collapsed.id not in ids
    # La baja de negocio real sigue apareciendo (tachada en la UI, reactivable).
    assert normal_baja.id in ids


async def test_list_include_inactive_excludes_brand_collapsed_bool_flag(
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    collapsed = Supplier(
        tenant_id=sample_tenant.tenant_id,
        name="Marca Colapsada Bool",
        deactivated_at=datetime.now(UTC),
        custom_fields={"_brand_collapsed": True},
    )
    db_session.add(collapsed)
    await db_session.commit()

    repo = SupplierRepository(db_session)
    listed = await repo.list_by_tenant(sample_tenant.tenant_id, include_inactive=True)
    assert collapsed.id not in {s.id for s in listed}


async def test_list_active_supplier_with_residual_flag_still_listed(
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Un proveedor ACTIVO con flag residual no se oculta (el filtro solo aplica a bajas)."""
    active_flagged = Supplier(
        tenant_id=sample_tenant.tenant_id,
        name="Restaurado con Flag Residual",
        custom_fields={"_brand_collapsed": "true"},
    )
    db_session.add(active_flagged)
    await db_session.commit()

    repo = SupplierRepository(db_session)
    listed_all = await repo.list_by_tenant(sample_tenant.tenant_id, include_inactive=True)
    listed_active = await repo.list_by_tenant(sample_tenant.tenant_id)
    assert active_flagged.id in {s.id for s in listed_all}
    assert active_flagged.id in {s.id for s in listed_active}


async def test_list_include_inactive_empty_custom_fields_not_discarded(
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Regresión del coalesce: key ausente → NULL — sin coalesce se descartaría todo."""
    plain = Supplier(tenant_id=sample_tenant.tenant_id, name="Proveedor Común")
    db_session.add(plain)
    await db_session.commit()

    repo = SupplierRepository(db_session)
    listed = await repo.list_by_tenant(sample_tenant.tenant_id, include_inactive=True)
    assert plain.id in {s.id for s in listed}
