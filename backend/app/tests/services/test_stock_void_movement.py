"""Regresión de ``void_movement``/``unvoid_movement`` (code review del fix de inflación).

Dos bugs que el review destapó y que estaban SIN cubrir (por eso pasaban ocultos):

- rev-A#1: el clamp ``max(0, ...)`` en la reversa se comía las ventas posteriores a una
  compra que se relee → el reread inflaba el stock en la cantidad vendida. La reversa
  debe ser EXACTA (puede dar transitoriamente negativo; el reimport la vuelve a sumar).
- rev-A#2: ``void_movement`` mutaba ``stock_units`` ANTES de sembrar el balance, así que
  un balance recién creado arrancaba ya descontado y la resta siguiente descontaba dos
  veces. Debe sembrarse el balance primero.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.stock_service import unvoid_movement, void_movement
from app.persistence.models.inventory import InventoryBalance, InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant


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


@pytest.mark.asyncio
async def test_void_reversal_is_exact_and_not_clamped(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """rev-A#1: void de una compra +100 sobre stock 60 (por ventas intermedias) ⇒ −40,
    NO 0. Sin clamp, el reimport (+100) reconverge a 60; con clamp daría 100 (inflado)."""
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=60)
    db_session.add(
        InventoryBalance(tenant_id=tid, product_id=product.id, current_qty=60, reserved_qty=0)
    )
    mov = InventoryMovement(
        tenant_id=tid, product_id=product.id, movement_type="purchase", qty=100
    )
    db_session.add(mov)
    await db_session.flush()

    await void_movement(mov, db_session)

    assert product.stock_units == -40  # reversa exacta, sin clamp a 0
    balance = (
        await db_session.execute(
            select(InventoryBalance).where(InventoryBalance.product_id == product.id)
        )
    ).scalar_one()
    assert balance.current_qty == -40
    assert mov.voided_at is not None


@pytest.mark.asyncio
async def test_void_without_balance_row_does_not_double_subtract(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """rev-A#2: producto con stock 10 y SIN fila de balance. Void de una compra qty=10 ⇒
    stock 0 y balance 0 (no −10 por doble resta al sembrar el balance)."""
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=10)
    mov = InventoryMovement(
        tenant_id=tid, product_id=product.id, movement_type="purchase", qty=10
    )
    db_session.add(mov)
    await db_session.flush()

    await void_movement(mov, db_session)

    assert product.stock_units == 0
    balance = (
        await db_session.execute(
            select(InventoryBalance).where(InventoryBalance.product_id == product.id)
        )
    ).scalar_one()
    assert balance.current_qty == 0  # sembrado desde 10, restado UNA sola vez


@pytest.mark.asyncio
async def test_void_is_idempotent(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Void dos veces no descuenta dos veces (idempotente)."""
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=10)
    mov = InventoryMovement(
        tenant_id=tid, product_id=product.id, movement_type="purchase", qty=10
    )
    db_session.add(mov)
    await db_session.flush()

    await void_movement(mov, db_session)
    await void_movement(mov, db_session)

    assert product.stock_units == 0


@pytest.mark.asyncio
async def test_unvoid_restores_effect(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """unvoid_movement revierte el void: re-aplica la qty y re-activa el movimiento."""
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=10)
    db_session.add(
        InventoryBalance(tenant_id=tid, product_id=product.id, current_qty=10, reserved_qty=0)
    )
    mov = InventoryMovement(
        tenant_id=tid, product_id=product.id, movement_type="purchase", qty=10
    )
    db_session.add(mov)
    await db_session.flush()

    await void_movement(mov, db_session)
    assert product.stock_units == 0

    await unvoid_movement(mov, db_session)
    assert product.stock_units == 10
    assert mov.voided_at is None
    balance = (
        await db_session.execute(
            select(InventoryBalance).where(InventoryBalance.product_id == product.id)
        )
    ).scalar_one()
    assert balance.current_qty == 10
