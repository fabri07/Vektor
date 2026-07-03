"""Guard de procedencia: un movimiento 'adjustment' sin source_type debe rechazarse
antes de tocar la sesión (defensa en profundidad junto al CHECK constraint de DB
ck_inventory_movements_adjustment_source_type, migración 20260728_0001)."""

from __future__ import annotations

import uuid

import pytest

from app.application.services.ingestion_import_service import _record_stock_movement


async def test_adjustment_without_source_type_raises() -> None:
    with pytest.raises(ValueError, match="source_type"):
        await _record_stock_movement(
            session=None,  # nunca se toca: el guard corta antes de cualquier uso
            tenant_id=uuid.uuid4(),
            product_id=uuid.uuid4(),
            qty=-5,
            unit_cost=None,
            movement_type="adjustment",
            final_qty=10,
            source_type=None,
        )


async def test_adjustment_with_source_type_does_not_raise_from_guard() -> None:
    # No debe levantar por el guard (puede fallar más adelante al tocar la sesión
    # real, pero eso está fuera del alcance de este test — solo valida el guard).
    with pytest.raises(AttributeError):
        await _record_stock_movement(
            session=None,
            tenant_id=uuid.uuid4(),
            product_id=uuid.uuid4(),
            qty=-5,
            unit_cost=None,
            movement_type="adjustment",
            final_qty=10,
            source_type="catalog_initial_stock",
        )


async def test_non_adjustment_without_source_type_does_not_raise_from_guard() -> None:
    # purchase/sale/loss no exigen source_type hoy (fuera de alcance de este fix).
    with pytest.raises(AttributeError):
        await _record_stock_movement(
            session=None,
            tenant_id=uuid.uuid4(),
            product_id=uuid.uuid4(),
            qty=5,
            unit_cost=None,
            movement_type="purchase",
            final_qty=10,
            source_type=None,
        )
