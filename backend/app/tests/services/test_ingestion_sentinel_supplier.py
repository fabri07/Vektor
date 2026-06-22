"""Reforma Proveedores (Fase 1): sentinela "No identificado".

Una compra de mercadería (libro de compras con cantidad>0) SIN proveedor
informado se agrupa en un único proveedor sentinela "No identificado" por tenant.
Una compra con proveedor genuino crea el proveedor real (regresión). El sentinela
es por-tenant (dos tenants → dos sentinelas separados).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry


def _purchase_summary(rows: list[dict]) -> dict:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "has_gasto": True,
        "gastos_detectados": rows,
    }


async def _make_tenant(db_session: AsyncSession) -> Tenant:
    t = Tenant(
        tenant_id=uuid.uuid4(),
        legal_name="Otro Tenant",
        display_name="Otro Tenant",
        currency="ARS",
        pricing_reference_mode="MEP",
        status="ACTIVE",
    )
    db_session.add(t)
    await db_session.flush()
    return t


@pytest.mark.asyncio
async def test_merch_purchase_without_supplier_uses_sentinel(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Libro de compras sin proveedor + qty>0 → UN solo "No identificado"."""
    summary = _purchase_summary(
        [
            {"fecha": "2024-01-15", "gasto": "5000", "producto": "Yerba", "cantidad": "10"},
        ]
    )
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"gastos": True}
    )

    suppliers = (await db_session.execute(select(Supplier))).scalars().all()
    assert len(suppliers) == 1
    assert suppliers[0].name == "No identificado"
    assert suppliers[0].custom_fields.get("_sentinel") == "true"
    expense = (await db_session.execute(select(ExpenseEntry))).scalar_one()
    assert expense.supplier_id == suppliers[0].id
    # Compra de mercadería → COGS + producto.
    assert expense.expense_type == "COGS"
    assert expense.product_id is not None


@pytest.mark.asyncio
async def test_two_imports_without_supplier_reuse_one_sentinel(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Dos imports sin proveedor (mismo tenant) → NO dos sentinelas."""
    for prod in ("Yerba", "Azúcar"):
        summary = _purchase_summary(
            [{"fecha": "2024-01-15", "gasto": "5000", "producto": prod, "cantidad": "5"}]
        )
        await importer.insert_confirmed_data(
            db_session, sample_tenant.tenant_id, summary, {"gastos": True}
        )

    sentinels = (
        (
            await db_session.execute(
                select(Supplier).where(Supplier.name == "No identificado")
            )
        )
        .scalars()
        .all()
    )
    assert len(sentinels) == 1  # un único sentinela reusado


@pytest.mark.asyncio
async def test_sentinel_is_per_tenant(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Mismo "No identificado" en dos tenants → dos entidades separadas."""
    other = await _make_tenant(db_session)
    summary = _purchase_summary(
        [{"fecha": "2024-01-15", "gasto": "5000", "producto": "Yerba", "cantidad": "5"}]
    )
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"gastos": True}
    )
    await importer.insert_confirmed_data(
        db_session, other.tenant_id, summary, {"gastos": True}
    )

    sentinels = (
        (
            await db_session.execute(
                select(Supplier).where(Supplier.name == "No identificado")
            )
        )
        .scalars()
        .all()
    )
    assert len(sentinels) == 2
    assert {s.tenant_id for s in sentinels} == {sample_tenant.tenant_id, other.tenant_id}


@pytest.mark.asyncio
async def test_genuine_supplier_creates_real_not_sentinel(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Libro de compras con proveedor genuino → crea el proveedor real (regresión)."""
    summary = _purchase_summary(
        [
            {
                "fecha": "2024-01-15",
                "gasto": "5000",
                "producto": "Yerba",
                "cantidad": "10",
                "proveedor": "Distribuidora Norte",
            },
        ]
    )
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"gastos": True}
    )

    suppliers = (await db_session.execute(select(Supplier))).scalars().all()
    assert len(suppliers) == 1
    assert suppliers[0].name == "Distribuidora Norte"
    assert suppliers[0].custom_fields.get("_sentinel") is None
    expense = (await db_session.execute(select(ExpenseEntry))).scalar_one()
    assert expense.supplier_id == suppliers[0].id


@pytest.mark.asyncio
async def test_opex_without_supplier_does_not_use_sentinel(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Un gasto OPEX (sin producto / sin cantidad) sin proveedor NO usa sentinela."""
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "has_gasto": True,
        "gastos_detectados": [
            {"fecha": "2024-01-15", "gasto": "5000", "categoria": "Alquiler"},
        ],
    }
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"gastos": True}
    )

    suppliers = (await db_session.execute(select(Supplier))).scalars().all()
    assert suppliers == []  # OPEX sin proveedor no crea sentinela
    expense = (await db_session.execute(select(ExpenseEntry))).scalar_one()
    assert expense.supplier_id is None
    assert expense.expense_type == "OPEX"
