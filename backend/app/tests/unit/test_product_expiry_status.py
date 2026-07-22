"""F6-B4: expiry_status computed en ProductResponse — vencido / próximo a vencer.

Informativo a nivel producto (no por lote). Umbral: EXPIRY_WARNING_DAYS (30).
"""

from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from app.domain.business_time import now_ar_naive
from app.schemas.product import EXPIRY_WARNING_DAYS, ProductResponse


def _make(expiry: date | None) -> ProductResponse:
    return ProductResponse(
        id=uuid4(),
        tenant_id=uuid4(),
        name="Test",
        sku=None,
        description=None,
        category=None,
        sale_price_ars=Decimal("100"),
        unit_cost_ars=None,
        stock_units=5,
        low_stock_threshold_units=None,
        is_active=True,
        expiry_date=expiry,
        created_at=datetime(2026, 1, 1, 12, 0, 0),
    )


def test_expiry_status_none_cuando_no_hay_vencimiento() -> None:
    assert _make(None).expiry_status is None


def test_expiry_status_expired() -> None:
    today = now_ar_naive().date()
    assert _make(today - timedelta(days=1)).expiry_status == "expired"


def test_expiry_status_expiring_soon_dentro_del_umbral() -> None:
    today = now_ar_naive().date()
    # Justo en el umbral (30 días) → próximo a vencer.
    assert _make(today + timedelta(days=EXPIRY_WARNING_DAYS)).expiry_status == "expiring_soon"
    # Vence hoy → próximo a vencer (no "expired": todavía no pasó).
    assert _make(today).expiry_status == "expiring_soon"


def test_expiry_status_ok_mas_alla_del_umbral() -> None:
    today = now_ar_naive().date()
    assert _make(today + timedelta(days=EXPIRY_WARNING_DAYS + 1)).expiry_status == "ok"
