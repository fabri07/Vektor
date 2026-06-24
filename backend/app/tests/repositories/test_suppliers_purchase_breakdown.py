"""Tests del desglose contable de compras por proveedor real.

Cubre: importe histórico (qty*unit_cost), cobertura de costo, NULL→"Sin proveedor
asignado" vs sentinela con nombre, "Sin marca", y orden estable.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant
from app.persistence.repositories.inventory_repository import InventoryRepository


async def _product(
    session: AsyncSession, tenant_id: uuid.UUID, name: str, marca: str | None = None
) -> Product:
    p = Product(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name=name,
        sale_price_ars=Decimal("100"),
        stock_units=5,
        provenance="REAL",
        custom_fields={"marca": marca} if marca else {},
    )
    session.add(p)
    await session.flush()
    return p


async def _purchase(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    product_id: uuid.UUID,
    supplier_id: uuid.UUID | None,
    qty: int,
    unit_cost: Decimal | None,
) -> None:
    session.add(
        InventoryMovement(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            product_id=product_id,
            supplier_id=supplier_id,
            movement_type="purchase",
            qty=qty,
            unit_cost=unit_cost,
        )
    )


@pytest.mark.asyncio
async def test_breakdown_groups_by_real_supplier_with_historical_amount(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    supplier = Supplier(id=uuid.uuid4(), tenant_id=tid, name="Distribuidora Norte")
    db_session.add(supplier)
    await db_session.flush()

    p1 = await _product(db_session, tid, "Yerba 1kg", marca="Playadito")
    p2 = await _product(db_session, tid, "Fideos", marca=None)
    # p1: 10 * 1500 = 15000 ; p2: 5 * 800 = 4000  → total 19000
    await _purchase(db_session, tid, p1.id, supplier.id, 10, Decimal("1500"))
    await _purchase(db_session, tid, p2.id, supplier.id, 5, Decimal("800"))
    await db_session.commit()

    result = await InventoryRepository(db_session).suppliers_purchase_breakdown(tid)

    assert len(result) == 1
    s = result[0]
    assert s.supplier_name == "Distribuidora Norte"
    assert s.is_unassigned is False
    assert s.total_purchased == pytest.approx(19000.0)
    assert s.coverage_pct == 100.0
    # Orden por importe desc: Yerba (15000) antes que Fideos (4000).
    assert [p.name for p in s.products] == ["Yerba 1kg", "Fideos"]
    assert s.products[0].brand == "Playadito"
    assert s.products[1].brand == "Sin marca"


@pytest.mark.asyncio
async def test_breakdown_null_supplier_is_unassigned_and_coverage_partial(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    p1 = await _product(db_session, tid, "Galletitas")
    # Una compra con costo (5*200=1000) y otra sin costo → cobertura 50%.
    await _purchase(db_session, tid, p1.id, None, 5, Decimal("200"))
    await _purchase(db_session, tid, p1.id, None, 3, None)
    await db_session.commit()

    result = await InventoryRepository(db_session).suppliers_purchase_breakdown(tid)

    assert len(result) == 1
    s = result[0]
    assert s.is_unassigned is True
    assert s.supplier_name == "Sin proveedor asignado"
    assert s.total_purchased == pytest.approx(1000.0)  # la fila sin costo no suma
    assert s.coverage_pct == 50.0
