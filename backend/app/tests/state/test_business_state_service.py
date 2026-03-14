"""
Tests for Business State Service.

Uses:
  - SQLite in-memory DB (via conftest fixtures).
  - FakeRedis: a simple dict-backed mock that satisfies the get/set interface
    used by compute_business_state, so no real Redis connection is needed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.business import BusinessProfile
from app.persistence.models.product import Product
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.state.business_state_service import compute_business_state


# ── Fake Redis ────────────────────────────────────────────────────────────────


class FakeRedis:
    """Minimal in-memory Redis stub (get / set with optional ex)."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._store[key] = value

    def snapshot(self) -> dict[str, str]:
        """Return a copy of the current store (for assertion)."""
        return dict(self._store)


# ── Shared fixtures ───────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def kiosco_profile(db_session: AsyncSession, sample_tenant) -> BusinessProfile:
    """BusinessProfile for sample_tenant with onboarding estimates only."""
    now = datetime.now(UTC)
    bp = BusinessProfile(
        tenant_id=sample_tenant.tenant_id,
        vertical_code="kiosco",
        data_mode="M0",
        data_confidence="LOW",
        monthly_sales_estimate_ars=Decimal("150000.00"),
        monthly_inventory_spend_estimate_ars=Decimal("90000.00"),
        monthly_fixed_expenses_estimate_ars=Decimal("20000.00"),
        cash_on_hand_estimate_ars=Decimal("15000.00"),
        supplier_count_estimate=2,
        product_count_estimate=3,
        onboarding_completed=True,
        updated_at=now,
        created_at=now,
    )
    db_session.add(bp)
    await db_session.commit()
    return bp


# ── Test 1: onboarding-only data ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compute_business_state_from_onboarding_only(
    db_session: AsyncSession,
    sample_tenant,
    kiosco_profile: BusinessProfile,
) -> None:
    """
    When there are no real transactions, estimates from BusinessProfile are used.
    Expected completeness (onboarding < 7 days):
      ventas     25  (estimate > 0)
      mercaderia 20  (estimate > 0)
      gastos     15  (estimate > 0)
      caja       20  (onboarding < 7 days, cash_on_hand_estimate > 0)
      productos   0  (estimate = 3, < 5)
      proveedores 10 (estimate = 2, >= 1)
      total      90 → HIGH
    """
    redis = FakeRedis()
    state = await compute_business_state(
        tenant_id=sample_tenant.tenant_id,
        session=db_session,
        redis=redis,
    )

    assert state.tenant_id == sample_tenant.tenant_id
    assert state.vertical_code == "kiosco"
    assert state.monthly_sales_est == Decimal("150000.00")
    assert state.monthly_inventory_cost_est == Decimal("90000.00")
    assert state.monthly_fixed_expenses_est == Decimal("20000.00")
    assert state.cash_on_hand_est == Decimal("15000.00")
    assert state.supplier_count == 2
    assert state.product_count == 3          # estimate, < 5

    # completeness: 25+20+15+20+0+10 = 90
    assert state.data_completeness_score == 90.0
    assert state.confidence_level == "HIGH"
    assert state.main_concern is None

    # Redis should be populated
    store = redis.snapshot()
    assert f"business_state:{sample_tenant.tenant_id}" in store
    assert f"last_inputs_hash:{sample_tenant.tenant_id}" in store


# ── Test 2: completeness increases with ≥5 active products ───────────────────


@pytest.mark.asyncio
async def test_completeness_increases_with_products(
    db_session: AsyncSession,
    sample_tenant,
    kiosco_profile: BusinessProfile,
) -> None:
    """
    Adding ≥5 active products should add 10 pts to completeness vs. baseline.
    Baseline (test_1): 90 pts (product_count_estimate = 3, no +10).
    After adding 5 real products: 90 + 10 = 100.
    """
    # Add 5 active products
    for i in range(5):
        p = Product(
            tenant_id=sample_tenant.tenant_id,
            name=f"Producto {i}",
            sale_price_ars=Decimal("1000.00"),
            stock_units=10,
            is_active=True,
        )
        db_session.add(p)
    await db_session.commit()

    redis = FakeRedis()
    state = await compute_business_state(
        tenant_id=sample_tenant.tenant_id,
        session=db_session,
        redis=redis,
    )

    # product_count now = 5 (real), so +10 pts
    assert state.product_count == 5
    assert state.data_completeness_score == 100.0
    assert state.confidence_level == "HIGH"
    assert len(state.products) == 5


# ── Test 3: cache is used when no new inputs ─────────────────────────────────


@pytest.mark.asyncio
async def test_cache_is_used_when_no_new_inputs(
    db_session: AsyncSession,
    sample_tenant,
    kiosco_profile: BusinessProfile,
) -> None:
    """
    Calling compute_business_state twice without any DB changes should return
    the cached state on the second call (same snapshot_id).
    """
    redis = FakeRedis()

    state1 = await compute_business_state(
        tenant_id=sample_tenant.tenant_id,
        session=db_session,
        redis=redis,
    )
    state2 = await compute_business_state(
        tenant_id=sample_tenant.tenant_id,
        session=db_session,
        redis=redis,
    )

    # Same snapshot — no recomputation
    assert state1.snapshot_id == state2.snapshot_id
    assert state1.data_completeness_score == state2.data_completeness_score


# ── Test 4: cache invalidates when new sale is added ─────────────────────────


@pytest.mark.asyncio
async def test_cache_invalidates_when_new_sale_added(
    db_session: AsyncSession,
    sample_tenant,
    kiosco_profile: BusinessProfile,
) -> None:
    """
    After a new SaleEntry is inserted the fingerprint changes, so the second
    call must recompute and produce a different (newer) snapshot_id.
    """
    redis = FakeRedis()

    # First call — populates cache
    state1 = await compute_business_state(
        tenant_id=sample_tenant.tenant_id,
        session=db_session,
        redis=redis,
    )

    # Insert a new sale
    sale = SaleEntry(
        tenant_id=sample_tenant.tenant_id,
        amount=Decimal("5000.00"),
        quantity=1,
        transaction_date=datetime.now(UTC).date(),
        payment_method="cash",
    )
    db_session.add(sale)
    await db_session.commit()

    # Second call — fingerprint is different → full recompute
    state2 = await compute_business_state(
        tenant_id=sample_tenant.tenant_id,
        session=db_session,
        redis=redis,
    )

    assert state1.snapshot_id != state2.snapshot_id
    # Real sales now present → monthly_sales_est = 5000 (not the estimate)
    assert state2.monthly_sales_est == Decimal("5000.00")
