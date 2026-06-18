"""Mejora A: captura de proveedor (find-or-create) en gastos importados.

Al importar gastos con una columna de proveedor, el ``ExpenseEntry`` queda
vinculado por ``supplier_id`` + ``supplier_name`` y, si el proveedor no existía,
se crea UN solo ``Supplier`` (dedup por nombre normalizado, sin importar casing).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.persistence.models.product import Product
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry


@pytest.mark.asyncio
async def test_expense_captures_and_creates_supplier(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "has_gasto": True,
        "gastos_detectados": [
            {"fecha": "2024-01-15", "gasto": "5000", "proveedor": "Distribuidora Norte"},
        ],
    }
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"gastos": True}
    )

    expense = (await db_session.execute(select(ExpenseEntry))).scalar_one()
    suppliers = (await db_session.execute(select(Supplier))).scalars().all()
    assert len(suppliers) == 1
    assert suppliers[0].name == "Distribuidora Norte"
    assert expense.supplier_id == suppliers[0].id
    assert expense.supplier_name == "Distribuidora Norte"


@pytest.mark.asyncio
async def test_same_supplier_different_casing_dedups_to_one(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "has_gasto": True,
        "gastos_detectados": [
            {"fecha": "2024-01-15", "gasto": "5000", "proveedor": "La Mayorista"},
            {"fecha": "2024-01-16", "gasto": "7000", "proveedor": "la mayorista"},
        ],
    }
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"gastos": True}
    )

    suppliers = (await db_session.execute(select(Supplier))).scalars().all()
    assert len(suppliers) == 1  # dedup por nombre normalizado (casing-insensitive)
    expenses = (await db_session.execute(select(ExpenseEntry))).scalars().all()
    assert len(expenses) == 2
    assert all(e.supplier_id == suppliers[0].id for e in expenses)


@pytest.mark.asyncio
async def test_existing_supplier_linked_without_creating(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    existing = Supplier(
        id=uuid.uuid4(), tenant_id=sample_tenant.tenant_id, name="Proveedor Viejo"
    )
    db_session.add(existing)
    await db_session.commit()

    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "has_gasto": True,
        "gastos_detectados": [
            # casing distinto del existente → debe matchear, no duplicar.
            {"fecha": "2024-01-15", "gasto": "5000", "proveedor": "proveedor viejo"},
        ],
    }
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"gastos": True}
    )

    suppliers = (await db_session.execute(select(Supplier))).scalars().all()
    assert len(suppliers) == 1  # no se creó uno nuevo
    expense = (await db_session.execute(select(ExpenseEntry))).scalar_one()
    assert expense.supplier_id == existing.id


@pytest.mark.asyncio
async def test_empty_supplier_cell_leaves_null(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "has_gasto": True,
        "gastos_detectados": [
            {"fecha": "2024-01-15", "gasto": "5000", "proveedor": ""},
        ],
    }
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"gastos": True}
    )

    expense = (await db_session.execute(select(ExpenseEntry))).scalar_one()
    suppliers = (await db_session.execute(select(Supplier))).scalars().all()
    assert suppliers == []
    assert expense.supplier_id is None
    assert expense.supplier_name is None


@pytest.mark.asyncio
async def test_deactivated_supplier_not_reused(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Un proveedor desactivado (soft-delete) no se reusa: se crea uno nuevo."""
    from datetime import UTC, datetime

    dead = Supplier(
        id=uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        name="Inactivo SA",
        deactivated_at=datetime.now(UTC),
    )
    db_session.add(dead)
    await db_session.commit()

    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "has_gasto": True,
        "gastos_detectados": [
            {"fecha": "2024-01-15", "gasto": "5000", "proveedor": "Inactivo SA"},
        ],
    }
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"gastos": True}
    )

    expense = (await db_session.execute(select(ExpenseEntry))).scalar_one()
    active = (
        (
            await db_session.execute(
                select(Supplier).where(Supplier.deactivated_at.is_(None))
            )
        )
        .scalars()
        .all()
    )
    assert len(active) == 1  # nuevo proveedor activo
    assert expense.supplier_id == active[0].id
    assert expense.supplier_id != dead.id


# ── Captura de proveedor desde CATÁLOGO de productos (columna "Tienda") ───────
# Los productos NO llevan supplier_id: solo se CREA el Supplier para poblar la
# sección Proveedores; el nombre de la tienda queda en custom_fields.


@pytest.mark.asyncio
async def test_catalog_tienda_creates_suppliers(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Catálogo (inferred_type='stock') con columna 'Tienda' y 2 tiendas distintas
    → se crean 2 Supplier; los productos se crean ok (sin supplier_id)."""
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "stock",
        "has_producto": True,
        "stock_detectado": [
            {"producto": "Vela aromática", "precio": "3000", "Tienda": "Deco Norte"},
            {"producto": "Portarretrato", "precio": "5000", "Tienda": "Bazar Sur"},
        ],
    }
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )

    suppliers = (await db_session.execute(select(Supplier))).scalars().all()
    assert {s.name for s in suppliers} == {"Deco Norte", "Bazar Sur"}
    products = (await db_session.execute(select(Product))).scalars().all()
    assert len(products) == 2
    # Referencia de la tienda persistida en custom_fields (no hay supplier_id).
    by_name = {p.name: p for p in products}
    assert by_name["Vela aromática"].custom_fields["proveedor"] == "Deco Norte"
    assert by_name["Portarretrato"].custom_fields["proveedor"] == "Bazar Sur"


@pytest.mark.asyncio
async def test_catalog_same_tienda_different_casing_dedups(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """2 productos de la misma tienda (distinto casing) → 1 solo Supplier."""
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "stock",
        "has_producto": True,
        "stock_detectado": [
            {"producto": "Vela A", "precio": "3000", "Tienda": "Deco Norte"},
            {"producto": "Vela B", "precio": "3500", "Tienda": "deco norte"},
        ],
    }
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )

    suppliers = (await db_session.execute(select(Supplier))).scalars().all()
    assert len(suppliers) == 1  # dedup por nombre normalizado
    products = (await db_session.execute(select(Product))).scalars().all()
    assert len(products) == 2


@pytest.mark.asyncio
async def test_catalog_without_tienda_creates_no_supplier(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Catálogo sin columna de tienda → no se crea ningún Supplier (no rompe)."""
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "stock",
        "has_producto": True,
        "stock_detectado": [
            {"producto": "Vela aromática", "precio": "3000"},
        ],
    }
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )

    suppliers = (await db_session.execute(select(Supplier))).scalars().all()
    assert suppliers == []
    products = (await db_session.execute(select(Product))).scalars().all()
    assert len(products) == 1
