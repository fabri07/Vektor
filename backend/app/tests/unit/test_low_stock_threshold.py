"""Tests para la semántica de low_stock_threshold_units.

Reglas:
  NULL  = no configurado → usa DEFAULT_LOW_STOCK_THRESHOLD_UNITS (5)
  0     = umbral explícito → sólo sin-stock (0 unidades) aplica como crítico
  > 0   = umbral configurado por el tenant
"""

import pytest
from decimal import Decimal
from uuid import uuid4

from app.domain.product import DEFAULT_LOW_STOCK_THRESHOLD_UNITS, effective_threshold
from app.schemas.product import ProductResponse


def _make_product(stock_units: int, low_stock_threshold_units: int | None) -> ProductResponse:
    return ProductResponse(
        id=uuid4(),
        tenant_id=uuid4(),
        name="Test",
        sku=None,
        description=None,
        category=None,
        sale_price_ars=Decimal("100"),
        unit_cost_ars=None,
        stock_units=stock_units,
        low_stock_threshold_units=low_stock_threshold_units,
        is_active=True,
    )


# ── effective_threshold ───────────────────────────────────────────────────────

def test_effective_threshold_none_returns_default():
    assert effective_threshold(None) == DEFAULT_LOW_STOCK_THRESHOLD_UNITS


def test_effective_threshold_zero_returns_zero():
    # 0 es intención explícita del usuario — NO se sobreescribe con default
    assert effective_threshold(0) == 0


def test_effective_threshold_positive_returns_value():
    assert effective_threshold(10) == 10
    assert effective_threshold(1) == 1


# ── ProductResponse.is_low_stock ─────────────────────────────────────────────

def test_no_threshold_configured_stock_above_default_is_ok():
    # 10 unidades, sin threshold → effective = 5 → 10 > 5 → no es bajo
    p = _make_product(stock_units=10, low_stock_threshold_units=None)
    assert p.is_low_stock is False


def test_no_threshold_configured_stock_at_default_is_low():
    # 5 unidades (igual al default) → 5 <= 5 → es bajo
    p = _make_product(stock_units=5, low_stock_threshold_units=None)
    assert p.is_low_stock is True


def test_no_threshold_configured_stock_below_default_is_low():
    # 3 unidades, sin threshold → 3 <= 5 → es bajo
    p = _make_product(stock_units=3, low_stock_threshold_units=None)
    assert p.is_low_stock is True


def test_no_threshold_configured_zero_stock_is_low():
    # 0 unidades → frontend muestra "Sin stock" (captura stock_units === 0 primero)
    # pero is_low_stock también es True — correcto
    p = _make_product(stock_units=0, low_stock_threshold_units=None)
    assert p.is_low_stock is True


def test_explicit_zero_threshold_nonzero_stock_is_not_low():
    # umbral=0 explícito + stock=5 → solo sin-stock aplica; 5 > 0 → no es bajo
    p = _make_product(stock_units=5, low_stock_threshold_units=0)
    assert p.is_low_stock is False


def test_explicit_zero_threshold_zero_stock_is_low():
    # umbral=0 + stock=0 → 0 <= 0 → es bajo (sin stock)
    p = _make_product(stock_units=0, low_stock_threshold_units=0)
    assert p.is_low_stock is True


def test_explicit_threshold_respected_below():
    # umbral=10, stock=8 → 8 <= 10 → es bajo
    p = _make_product(stock_units=8, low_stock_threshold_units=10)
    assert p.is_low_stock is True


def test_explicit_threshold_respected_at():
    # umbral=10, stock=10 → 10 <= 10 → es bajo (igual al umbral = reponer)
    p = _make_product(stock_units=10, low_stock_threshold_units=10)
    assert p.is_low_stock is True


def test_explicit_threshold_respected_above():
    # umbral=10, stock=11 → 11 > 10 → ok
    p = _make_product(stock_units=11, low_stock_threshold_units=10)
    assert p.is_low_stock is False


def test_explicit_threshold_not_overridden_by_default():
    # umbral=20 > DEFAULT=5 → debe respetar el 20, no el 5
    p_with_threshold = _make_product(stock_units=10, low_stock_threshold_units=20)
    p_default = _make_product(stock_units=10, low_stock_threshold_units=None)
    # Con threshold=20: 10 <= 20 → bajo
    assert p_with_threshold.is_low_stock is True
    # Con threshold=None (effective=5): 10 > 5 → ok
    assert p_default.is_low_stock is False
