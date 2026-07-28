"""
Tests for Business State Service.

Uses:
  - SQLite in-memory DB (via conftest fixtures).
  - FakeRedis: a simple dict-backed mock that satisfies the get/set interface
    used by compute_business_state, so no real Redis connection is needed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.verticals import Vertical
from app.persistence.models.business import BusinessProfile
from app.persistence.models.product import Product
from app.persistence.models.transaction import SaleEntry
from app.state.business_state_service import (
    _cache_key,
    _hash_key,
    compute_business_state,
)

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
async def kiosco_profile(
    db_session: AsyncSession, sample_business_profile: BusinessProfile
) -> BusinessProfile:
    """El perfil de `sample_tenant` con las estimaciones de onboarding cargadas."""
    now = datetime.now(UTC)
    bp = sample_business_profile
    bp.monthly_sales_estimate_ars = Decimal("150000.00")
    bp.monthly_inventory_spend_estimate_ars = Decimal("90000.00")
    bp.monthly_fixed_expenses_estimate_ars = Decimal("20000.00")
    bp.cash_on_hand_estimate_ars = Decimal("15000.00")
    bp.supplier_count_estimate = 2
    bp.product_count_estimate = 3
    bp.onboarding_completed = True
    bp.updated_at = now
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
        redis=redis,  # type: ignore[arg-type]  # test double / fixture
    )

    assert state.tenant_id == sample_tenant.tenant_id
    assert state.vertical_code == Vertical.KIOSCO_ALMACEN.value
    assert state.monthly_sales_est == Decimal("150000.00")
    assert state.monthly_inventory_cost_est == Decimal("90000.00")
    assert state.monthly_fixed_expenses_est == Decimal("20000.00")
    assert state.cash_on_hand_est == Decimal("15000.00")
    assert state.supplier_count == 2
    assert state.product_count == 3  # estimate, < 5

    # completeness: 25+20+15+20+0+10 = 90
    assert state.data_completeness_score == 90.0
    assert state.confidence_level == "HIGH"
    assert state.main_concern is None

    # Redis should be populated
    store = redis.snapshot()
    # Las keys llevan el prefijo de versión v2 (los rulesets pasaron al código
    # canónico y los blobs viejos ya no deserializan).
    assert _cache_key(sample_tenant.tenant_id) in store
    assert _hash_key(sample_tenant.tenant_id) in store
    assert "business_state:v2:" in _cache_key(sample_tenant.tenant_id)
    assert "last_inputs_hash:v2:" in _hash_key(sample_tenant.tenant_id)


# ── Montos del onboarding sin contestar ──────────────────────────────────────


@pytest.mark.asyncio
async def test_montos_sin_contestar_no_se_leen_como_cero(
    db_session: AsyncSession,
    sample_tenant,
    kiosco_profile: BusinessProfile,
) -> None:
    """Con la caja sin contestar, la fuente no puede ser "onboarding".

    El tiering de caja ya distinguía `is not None` desde antes, así que este
    test **no protege un fix**: documenta un camino que hasta ahora era
    inalcanzable. El formulario mandaba `parseFloat(campo) || 0`, de modo que
    `cash_on_hand_estimate_ars` nunca quedaba NULL después de un onboarding y
    esta rama no se ejercitaba nunca en la práctica.

    Ahora sí se llega, y lo que hace es lo correcto: sin saldo declarado el
    score cae al tier siguiente en vez de operar sobre un $0 inventado.
    """
    bp = kiosco_profile
    bp.monthly_inventory_spend_estimate_ars = None
    bp.monthly_fixed_expenses_estimate_ars = None
    bp.cash_on_hand_estimate_ars = None
    await db_session.commit()

    state = await compute_business_state(
        tenant_id=sample_tenant.tenant_id,
        session=db_session,
        redis=FakeRedis(),  # type: ignore[arg-type]  # test double / fixture
    )

    assert state.cash_source != "onboarding"
    assert state.cash_source in ("flujo", "desconocido")


@pytest.mark.asyncio
async def test_cero_declarado_si_es_el_estimado_del_onboarding(
    db_session: AsyncSession,
    sample_tenant,
    kiosco_profile: BusinessProfile,
) -> None:
    """El espejo: un cero CONTESTADO sí es el saldo del negocio.

    Sin este test, el de arriba pasaría igual con un `or Decimal("0")` que
    tratara todo cero como ausencia. Los dos juntos fijan la distinción.
    """
    bp = kiosco_profile
    bp.cash_on_hand_estimate_ars = Decimal("0")
    await db_session.commit()

    state = await compute_business_state(
        tenant_id=sample_tenant.tenant_id,
        session=db_session,
        redis=FakeRedis(),  # type: ignore[arg-type]  # test double / fixture
    )

    assert state.cash_source == "onboarding"
    assert state.cash_on_hand_est == Decimal("0")


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
        redis=redis,  # type: ignore[arg-type]  # test double / fixture
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
        redis=redis,  # type: ignore[arg-type]  # test double / fixture
    )
    state2 = await compute_business_state(
        tenant_id=sample_tenant.tenant_id,
        session=db_session,
        redis=redis,  # type: ignore[arg-type]  # test double / fixture
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
        redis=redis,  # type: ignore[arg-type]  # test double / fixture
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
        redis=redis,  # type: ignore[arg-type]  # test double / fixture
    )

    assert state1.snapshot_id != state2.snapshot_id
    # A tiny amount of real data blends with onboarding instead of replacing it.
    assert state2.monthly_sales_est == Decimal("150000.000")


@pytest.mark.asyncio
async def test_blend_sales_few_transactions(
    db_session: AsyncSession,
    sample_tenant,
    kiosco_profile: BusinessProfile,
) -> None:
    today = datetime.now(UTC).date()
    for i in range(5):
        db_session.add(
            SaleEntry(
                tenant_id=sample_tenant.tenant_id,
                amount=Decimal("1000.00"),
                quantity=1,
                transaction_date=today - timedelta(days=4 - i),
                payment_method="cash",
            )
        )
    await db_session.commit()

    state = await compute_business_state(
        tenant_id=sample_tenant.tenant_id,
        session=db_session,
        redis=FakeRedis(),  # type: ignore[arg-type]  # test double / fixture
    )

    # 5 sales over 5 days projects to 30000 monthly, then blends 70/30.
    assert state.monthly_sales_est == Decimal("114000.000")


@pytest.mark.asyncio
async def test_blend_sales_many_transactions(
    db_session: AsyncSession,
    sample_tenant,
    kiosco_profile: BusinessProfile,
) -> None:
    today = datetime.now(UTC).date()
    for i in range(60):
        db_session.add(
            SaleEntry(
                tenant_id=sample_tenant.tenant_id,
                amount=Decimal("100.00"),
                quantity=1,
                transaction_date=today - timedelta(days=i % 30),
                payment_method="cash",
            )
        )
    await db_session.commit()

    state = await compute_business_state(
        tenant_id=sample_tenant.tenant_id,
        session=db_session,
        redis=FakeRedis(),  # type: ignore[arg-type]  # test double / fixture
    )

    assert state.monthly_sales_est == Decimal("6000.00")


@pytest.mark.asyncio
async def test_product_summaries_include_30_day_rotation_units(
    db_session: AsyncSession,
    sample_tenant,
    kiosco_profile: BusinessProfile,
) -> None:
    product = Product(
        tenant_id=sample_tenant.tenant_id,
        name="Yerba",
        sale_price_ars=Decimal("1200.00"),
        stock_units=12,
        low_stock_threshold_units=3,
        is_active=True,
    )
    db_session.add(product)
    await db_session.flush()
    db_session.add(
        SaleEntry(
            tenant_id=sample_tenant.tenant_id,
            product_id=product.id,
            amount=Decimal("2400.00"),
            quantity=2,
            transaction_date=datetime.now(UTC).date(),
            payment_method="cash",
        )
    )
    await db_session.commit()

    state = await compute_business_state(
        tenant_id=sample_tenant.tenant_id,
        session=db_session,
        redis=FakeRedis(),  # type: ignore[arg-type]  # test double / fixture
    )

    assert len(state.products) == 1
    assert state.products[0].units_sold_30d == 2
