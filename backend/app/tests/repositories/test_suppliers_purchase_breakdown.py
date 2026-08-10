"""Tests del desglose contable de compras por proveedor real.

Cubre: importe histórico (qty*unit_cost), cobertura de costo, NULL→"Sin proveedor
asignado" vs sentinela con nombre, "Sin marca", y orden estable.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant
from app.persistence.repositories.inventory_repository import InventoryRepository
from app.tests.repositories._helpers import _product, _purchase


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
    assert s.supplier_name == "No identificado"
    assert s.total_purchased == pytest.approx(1000.0)  # la fila sin costo no suma
    assert s.coverage_pct == 50.0


async def test_null_and_sentinel_supplier_merge_into_one(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Compras sin proveedor (NULL) + el sentinela 'No identificado' se unifican."""
    tid = sample_tenant.tenant_id
    sentinel = Supplier(
        id=uuid.uuid4(),
        tenant_id=tid,
        name="No identificado",
        custom_fields={"_sentinel": "true"},
    )
    db_session.add(sentinel)
    await db_session.flush()
    p1 = await _product(db_session, tid, "Yerba")
    await _purchase(db_session, tid, p1.id, None, 5, Decimal("100"))  # sin proveedor
    await _purchase(db_session, tid, p1.id, sentinel.id, 3, Decimal("100"))  # sentinela
    await db_session.commit()

    result = await InventoryRepository(db_session).suppliers_purchase_breakdown(tid)

    # Un único bucket "No identificado" con ambas compras sumadas (8*100=800) y el
    # producto fusionado en una sola fila.
    assert len(result) == 1
    s = result[0]
    assert s.supplier_name == "No identificado"
    assert s.is_unassigned is True
    assert s.total_purchased == pytest.approx(800.0)
    assert len(s.products) == 1
    assert s.products[0].total_qty == pytest.approx(8.0)


async def test_breakdown_excludes_voided_movements(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Un movimiento con voided_at seteado (dedup/reread) NO cuenta para lo comprado."""
    tid = sample_tenant.tenant_id
    supplier = Supplier(id=uuid.uuid4(), tenant_id=tid, name="Distribuidora Sur")
    db_session.add(supplier)
    await db_session.flush()
    p1 = await _product(db_session, tid, "Azúcar")
    # Compra válida (10*100=1000) + compra voidada (duplicado anulado) que NO debe sumar.
    await _purchase(db_session, tid, p1.id, supplier.id, 10, Decimal("100"))
    await _purchase(db_session, tid, p1.id, supplier.id, 10, Decimal("100"), voided=True)
    await db_session.commit()

    result = await InventoryRepository(db_session).suppliers_purchase_breakdown(tid)

    assert len(result) == 1
    s = result[0]
    assert s.total_purchased == pytest.approx(1000.0)
    assert len(s.products) == 1
    assert s.products[0].total_qty == pytest.approx(10.0)


async def test_products_purchased_excludes_voided_movements(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """products_purchased_from_supplier ignora movimientos voidados en la cantidad."""
    tid = sample_tenant.tenant_id
    supplier = Supplier(id=uuid.uuid4(), tenant_id=tid, name="Mayorista Centro")
    db_session.add(supplier)
    await db_session.flush()
    p1 = await _product(db_session, tid, "Harina")
    await _purchase(db_session, tid, p1.id, supplier.id, 7, Decimal("50"))
    await _purchase(db_session, tid, p1.id, supplier.id, 7, Decimal("50"), voided=True)
    await db_session.commit()

    result = await InventoryRepository(db_session).products_purchased_from_supplier(
        tid, supplier.id
    )

    assert len(result) == 1
    assert result[0].total_qty == pytest.approx(7.0)
