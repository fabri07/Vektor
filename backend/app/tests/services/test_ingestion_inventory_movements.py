"""FASE 3: audit de inventario al importar productos.

Importar productos con stock deja un InventoryMovement (historial), que antes
quedaba vacío (el import seteaba stock_units directo sin rastro). Product.stock_units
sigue siendo la representación canónica.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant


def _stock_summary(rows: list[dict[str, object]]) -> dict[str, object]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "stock",
        "has_producto": True,
        "stock_detectado": rows,
    }


@pytest.mark.asyncio
async def test_new_product_import_records_purchase_movement(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    summary = _stock_summary(
        [{"producto": "Yerba 1kg", "precio": "2500", "costo": "1500", "stock": "10"}]
    )
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )

    movements = (await db_session.execute(select(InventoryMovement))).scalars().all()
    assert len(movements) == 1
    mv = movements[0]
    assert mv.movement_type == "purchase"
    assert mv.qty == 10
    assert mv.source_event_id == "import"

    product = (await db_session.execute(select(Product))).scalar_one()
    assert product.stock_units == 10  # representación canónica intacta


@pytest.mark.asyncio
async def test_stock_update_records_delta_adjustment(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    # Producto pre-existente con stock 10.
    product = Product(
        id=uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        name="Fideos",
        sale_price_ars=Decimal("100"),
        stock_units=10,
        provenance="REAL",
    )
    db_session.add(product)
    await db_session.commit()

    # Import sube el stock a 15 → movimiento de +5.
    summary = _stock_summary([{"producto": "Fideos", "stock": "15"}])
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )

    movements = (await db_session.execute(select(InventoryMovement))).scalars().all()
    assert len(movements) == 1
    assert movements[0].qty == 5
    assert movements[0].movement_type == "purchase"

    await db_session.refresh(product)
    assert product.stock_units == 15


@pytest.mark.asyncio
async def test_product_without_stock_records_no_movement(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    summary = _stock_summary([{"producto": "Solo catálogo", "precio": "500"}])
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )

    movements = (await db_session.execute(select(InventoryMovement))).scalars().all()
    assert movements == []  # sin stock → sin movimiento
