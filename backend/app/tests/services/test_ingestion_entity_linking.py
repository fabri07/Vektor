"""FASE 3: vínculo de entidades en la ingestión.

Al importar ventas/gastos, si la fila referencia un producto del catálogo
(por nombre o SKU), la transacción queda vinculada vía `product_id`.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry


async def _make_product(
    db: AsyncSession, tenant_id: uuid.UUID, name: str, sku: str | None = None
) -> Product:
    product = Product(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name=name,
        sku=sku,
        sale_price_ars=Decimal("100"),
        stock_units=10,
        provenance="REAL",
    )
    db.add(product)
    await db.commit()
    return product


@pytest.mark.asyncio
async def test_sale_linked_to_product_by_name(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    product = await _make_product(db_session, sample_tenant.tenant_id, "Coca Cola 500ml")

    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "has_venta": True,
        "ventas_detectadas": [
            {"fecha": "2024-01-15", "monto": "1500", "producto": "Coca-Cola 500ml"},
        ],
    }
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"ventas": True}
    )

    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.product_id == product.id  # match por nombre normalizado (guion ≈ espacio)


@pytest.mark.asyncio
async def test_sale_linked_to_product_by_sku(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    product = await _make_product(
        db_session, sample_tenant.tenant_id, "Producto X", sku="SKU-123"
    )

    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "has_venta": True,
        "ventas_detectadas": [
            {"fecha": "2024-01-15", "monto": "2000", "nombre": "otra cosa", "sku": "SKU-123"},
        ],
    }
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"ventas": True}
    )

    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.product_id == product.id  # SKU exacto gana sobre el nombre


@pytest.mark.asyncio
async def test_unknown_product_leaves_product_id_none(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "has_venta": True,
        "ventas_detectadas": [
            {"fecha": "2024-01-15", "monto": "1500", "producto": "No existe en catálogo"},
        ],
    }
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"ventas": True}
    )

    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.product_id is None


@pytest.mark.asyncio
async def test_ambiguous_name_not_linked(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    # Dos productos con el mismo nombre normalizado → ambiguo, no se vincula.
    await _make_product(db_session, sample_tenant.tenant_id, "Agua", sku="A1")
    await _make_product(db_session, sample_tenant.tenant_id, "agua", sku="A2")

    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "has_venta": True,
        "ventas_detectadas": [{"fecha": "2024-01-15", "monto": "800", "producto": "Agua"}],
    }
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"ventas": True}
    )

    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.product_id is None


@pytest.mark.asyncio
async def test_quantity_summed_does_not_break_linking(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Varias ventas del mismo producto en un archivo → todas vinculadas."""
    product = await _make_product(db_session, sample_tenant.tenant_id, "Alfajor", sku="ALF")

    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "has_venta": True,
        "ventas_detectadas": [
            {"fecha": "2024-01-15", "monto": "300", "producto": "Alfajor"},
            {"fecha": "2024-01-16", "monto": "300", "producto": "Alfajor"},
        ],
    }
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"ventas": True}
    )

    sales = (await db_session.execute(select(SaleEntry))).scalars().all()
    assert len(sales) == 2
    assert all(s.product_id == product.id for s in sales)
