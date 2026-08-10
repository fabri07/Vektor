"""FASE 3: audit de inventario al importar productos.

Importar productos con stock deja un InventoryMovement (historial), que antes
quedaba vacío (el import seteaba stock_units directo sin rastro). Product.stock_units
sigue siendo la representación canónica.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.persistence.models.inventory import InventoryBalance, InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant


def _stock_summary(rows: list[dict[str, object]]) -> dict[str, object]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "stock",
        "has_producto": True,
        "stock_detectado": rows,
    }


async def test_new_product_import_records_movement(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    # Import de catálogo SIN stock_treatment ⇒ saldo de apertura por default: el stock
    # entra al inventario como AJUSTE (no compra) y deja su movimiento (historial).
    summary = _stock_summary(
        [{"producto": "Yerba 1kg", "precio": "2500", "costo": "1500", "stock": "10"}]
    )
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )

    movements = (await db_session.execute(select(InventoryMovement))).scalars().all()
    assert len(movements) == 1
    mv = movements[0]
    assert mv.movement_type == "adjustment"  # saldo de apertura, no compra
    assert mv.qty == 10

    product = (await db_session.execute(select(Product))).scalar_one()
    assert product.stock_units == 10  # representación canónica intacta


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
    assert movements[0].movement_type == "adjustment"  # saldo de apertura por default

    await db_session.refresh(product)
    assert product.stock_units == 15


async def test_product_without_stock_records_no_movement(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    summary = _stock_summary([{"producto": "Solo catálogo", "precio": "500"}])
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )

    movements = (await db_session.execute(select(InventoryMovement))).scalars().all()
    assert movements == []  # sin stock → sin movimiento
    balances = (await db_session.execute(select(InventoryBalance))).scalars().all()
    assert balances == []  # qty 0 → sin balance


# ── FASE 3 (B3): sincronización de InventoryBalance en el import ───────────────


async def test_new_product_import_creates_balance(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    summary = _stock_summary(
        [{"producto": "Azúcar 1kg", "precio": "1200", "costo": "700", "stock": "8"}]
    )
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )

    balance = (await db_session.execute(select(InventoryBalance))).scalar_one()
    assert balance.current_qty == 8  # consistente con Product.stock_units
    assert balance.tenant_id == sample_tenant.tenant_id


async def test_reimport_sums_delta_into_balance(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    # Producto pre-existente con stock 10 y balance sincronizado en 10.
    product = Product(
        id=uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        name="Té",
        sale_price_ars=Decimal("100"),
        stock_units=10,
        provenance="REAL",
    )
    db_session.add(product)
    db_session.add(
        InventoryBalance(
            tenant_id=sample_tenant.tenant_id, product_id=product.id, current_qty=10, reserved_qty=0
        )
    )
    await db_session.commit()

    # Import sube el stock a 14 → delta +4.
    summary = _stock_summary([{"producto": "Té", "stock": "14"}])
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )

    balance = (await db_session.execute(select(InventoryBalance))).scalar_one()
    assert balance.current_qty == 14  # 10 + delta(4)
    await db_session.refresh(product)
    assert product.stock_units == 14  # canónico consistente


async def test_balance_isolated_by_tenant(
    db_session: AsyncSession, sample_tenant: Tenant, second_tenant: Tenant
) -> None:
    summary = _stock_summary([{"producto": "Solo tenant 1", "precio": "500", "stock": "3"}])
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )

    balances = (await db_session.execute(select(InventoryBalance))).scalars().all()
    assert len(balances) == 1
    assert balances[0].tenant_id == sample_tenant.tenant_id
    # El segundo tenant no tiene balance.
    other = (
        await db_session.execute(
            select(InventoryBalance).where(InventoryBalance.tenant_id == second_tenant.tenant_id)
        )
    ).scalars().all()
    assert other == []
