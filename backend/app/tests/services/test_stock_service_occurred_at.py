"""``occurred_at`` (fecha de NEGOCIO) en los writers de ``stock_service``.

Contexto: ``created_at`` es la fecha de CARGA (cuándo se insertó la fila);
``occurred_at`` es cuándo pasó realmente la venta/compra/merma. El dedup por timing
(``scripts/repair_inventory_ledger.py``) agrupaba por ``date(created_at)`` y voideó
compras reales de meses distintos cargadas el mismo día (incidente "don pedro",
2026-07). Estos tests cubren que cada writer estampe ``occurred_at`` correctamente.

``EventBus.emit`` se patchea (igual que ``test_stock_workflow.py``): intenta publicar a
Celery/Redis, que no está disponible en el entorno de test.
"""

from __future__ import annotations

import unittest.mock
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.stock.agent import AgentStock
from app.application.services.stock_service import (
    decrement_stock,
    increment_stock,
    register_stock_loss,
    unvoid_movement,
    void_movement,
)
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry


async def _make_product(
    db: AsyncSession, tenant_id: uuid.UUID, stock_units: int
) -> Product:
    product = Product(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name="Yerba 1kg",
        sale_price_ars=Decimal("2500"),
        unit_cost_ars=Decimal("1500"),
        stock_units=stock_units,
    )
    db.add(product)
    await db.flush()
    return product


async def test_decrement_stock_persists_explicit_occurred_at_aware(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=10)
    sale_date = datetime(2026, 3, 10, 12, 0, tzinfo=UTC)

    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        movement = await decrement_stock(
            product.id, tid, 2, "src", db_session, occurred_at=sale_date
        )

    assert movement.occurred_at is not None
    assert movement.occurred_at.replace(tzinfo=None) == sale_date.replace(tzinfo=None)


async def test_decrement_stock_persists_explicit_occurred_at_naive(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """``transaction_date`` de ventas/gastos se persiste NAIVE — decrement_stock debe
    normalizarlo a UTC (no rechazarlo ni reinterpretarlo)."""
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=10)
    sale_date_naive = datetime(2026, 3, 10, 12, 0)

    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        movement = await decrement_stock(
            product.id, tid, 2, "src", db_session, occurred_at=sale_date_naive
        )

    assert movement.occurred_at is not None
    assert movement.occurred_at.replace(tzinfo=None) == sale_date_naive


async def test_decrement_stock_without_occurred_at_falls_back_to_now(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=10)

    before = datetime.now(UTC) - timedelta(seconds=5)
    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        movement = await decrement_stock(product.id, tid, 2, "src", db_session)
    after = datetime.now(UTC) + timedelta(seconds=5)

    assert movement.occurred_at is not None
    occurred = movement.occurred_at
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=UTC)
    assert before <= occurred <= after


async def test_increment_stock_persists_explicit_occurred_at(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=0)
    purchase_date = datetime(2026, 1, 15, 9, 30, tzinfo=UTC)

    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        movement = await increment_stock(
            product.id,
            tid,
            5,
            Decimal("1000"),
            "src",
            db_session,
            occurred_at=purchase_date,
        )

    assert movement.occurred_at is not None
    assert movement.occurred_at.replace(tzinfo=None) == purchase_date.replace(tzinfo=None)


async def test_increment_stock_without_occurred_at_falls_back_to_now(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=0)

    before = datetime.now(UTC) - timedelta(seconds=5)
    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        movement = await increment_stock(
            product.id, tid, 5, Decimal("1000"), "src", db_session
        )
    after = datetime.now(UTC) + timedelta(seconds=5)

    assert movement.occurred_at is not None
    occurred = movement.occurred_at
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=UTC)
    assert before <= occurred <= after


async def test_register_stock_loss_sets_occurred_at_to_now(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """La merma se registra cuando se DESCUBRE — occurred_at = momento del registro."""
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=10)

    before = datetime.now(UTC) - timedelta(seconds=5)
    movement = await register_stock_loss(product.id, tid, 3, "merma", None, db_session)
    after = datetime.now(UTC) + timedelta(seconds=5)

    assert movement.occurred_at is not None
    occurred = movement.occurred_at
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=UTC)
    assert before <= occurred <= after


async def test_on_sale_recorded_stamps_movement_with_sale_transaction_date(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """AgentStock.on_sale_recorded (path SALE_RECORDED del chat) debe estampar el
    movimiento de inventario con la fecha de NEGOCIO de la venta (transaction_date),
    no con la fecha de carga (now())."""
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=10)
    backdated = datetime(2026, 2, 14, 10, 0)  # NAIVE, como se persiste transaction_date
    sale = SaleEntry(
        id=uuid.uuid4(),
        tenant_id=tid,
        product_id=product.id,
        amount=Decimal("100"),
        quantity=3,
        transaction_date=backdated,
    )
    db_session.add(sale)
    await db_session.flush()

    agent = AgentStock(db=db_session)
    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        await agent.on_sale_recorded(str(sale.id), str(tid), db=db_session)

    movement = (
        await db_session.execute(
            select(InventoryMovement).where(
                InventoryMovement.tenant_id == tid,
                InventoryMovement.product_id == product.id,
                InventoryMovement.movement_type == "sale",
            )
        )
    ).scalar_one()
    assert movement.occurred_at is not None
    assert movement.occurred_at.replace(tzinfo=None) == backdated


async def test_void_and_unvoid_do_not_modify_occurred_at(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=10)
    purchase_date = datetime(2026, 2, 1, tzinfo=UTC)

    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        movement = await increment_stock(
            product.id,
            tid,
            4,
            Decimal("500"),
            "src",
            db_session,
            occurred_at=purchase_date,
        )
    original_occurred_at = movement.occurred_at

    await void_movement(movement, db_session)
    assert movement.occurred_at == original_occurred_at

    await unvoid_movement(movement, db_session)
    assert movement.occurred_at == original_occurred_at
