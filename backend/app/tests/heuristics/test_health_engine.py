"""
Tests for Health Engine v2 (Stage 5a).

All tests use hardcoded BusinessState objects — no DB, no Redis.
Numeric assertions are derived from the strict linear interpolation spec:
    score = int(s_low + pos * (s_high - s_low))
    where pos = (value - band_low) / (band_high - band_low)

Formula v2: cash×0.30 + stock×0.20 + supplier×0.10 + margin×0.20 + growth×0.20
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from app.heuristics.health_engine import HealthScoreResult, calculate_health_score
from app.heuristics.verticals.kiosco import BENCHMARK as KIOSCO_BENCHMARK
from app.heuristics.verticals.loader import load_vertical_heuristics
from app.state.business_state_service import BusinessState, ProductSummary

# ── Helpers ───────────────────────────────────────────────────────────────────


def _product(stock: int, threshold: int) -> ProductSummary:
    return ProductSummary(
        product_id=uuid.uuid4(),
        name="Producto Test",
        stock_units=stock,
        low_stock_threshold_units=threshold,
        sale_price_ars=Decimal("1000.00"),
    )


def _make_state(
    vertical_code: str = "kiosco",
    monthly_sales_est: Decimal = Decimal("100000"),
    monthly_inventory_cost_est: Decimal = Decimal("60000"),
    monthly_fixed_expenses_est: Decimal = Decimal("17000"),
    cash_on_hand_est: Decimal = Decimal("40000"),
    prev_monthly_sales_est: Decimal = Decimal("0"),
    supplier_count: int = 3,
    products: list[ProductSummary] | None = None,
    data_completeness_score: float = 75.0,
    confidence_level: str = "MEDIUM",
) -> BusinessState:
    return BusinessState(
        snapshot_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        vertical_code=vertical_code,
        data_completeness_score=data_completeness_score,
        confidence_level=confidence_level,
        # ruleset no lo lee el engine (usa vertical_code); benchmark como test double
        ruleset=KIOSCO_BENCHMARK,  # type: ignore[arg-type]
        monthly_sales_est=monthly_sales_est,
        monthly_inventory_cost_est=monthly_inventory_cost_est,
        monthly_fixed_expenses_est=monthly_fixed_expenses_est,
        cash_on_hand_est=cash_on_hand_est,
        prev_monthly_sales_est=prev_monthly_sales_est,
        product_count=len(products) if products else 0,
        supplier_count=supplier_count,
        products=products or [],
        main_concern=None,
    )


# ── Test 1: kiosco healthy margin scores high ─────────────────────────────────


def test_kiosco_healthy_margin_scores_high() -> None:
    """
    Margin = (100000 - 60000 - 17000) / 100000 = 23000 / 100000 = 0.23

    Kiosco benchmark: healthy_min=0.18, healthy_max=0.28 → band [70, 89].
    pos = (0.23 - 0.18) / (0.28 - 0.18) = 0.05 / 0.10 = 0.5
    score_margin = int(70 + 0.5 * (89 - 70)) = int(79.5) = 79
    """
    state = _make_state(
        monthly_sales_est=Decimal("100000"),
        monthly_inventory_cost_est=Decimal("60000"),
        monthly_fixed_expenses_est=Decimal("17000"),
    )
    result: HealthScoreResult = calculate_health_score(state)

    assert (
        70 <= result.score_margin <= 89
    ), f"Expected score_margin in [70, 89], got {result.score_margin}"
    assert result.score_margin == 79


# ── Test 2: critical cash scores low ─────────────────────────────────────────


def test_kiosco_critical_cash_scores_low() -> None:
    """
    cash_days = 1000 / (20000 / 30) = 1.5.
    Kiosco JSON: critical_days_below=5 → score_cash in critical band.

    primary_risk must be CASH_LOW (lowest subscore).
    """
    state = _make_state(
        cash_on_hand_est=Decimal("1000"),
        monthly_fixed_expenses_est=Decimal("20000"),
        supplier_count=3,
        products=[],
    )
    result: HealthScoreResult = calculate_health_score(state)

    assert result.score_cash <= 14, f"Expected score_cash <= 14, got {result.score_cash}"
    assert result.primary_risk_code == "CASH_LOW"


# ── Test 3: single supplier penalizes supplier score ─────────────────────────


def test_single_supplier_penalizes_supplier_score() -> None:
    """
    supplier_count=1 → band [1, 2) → [15, 44].
    pos = (1 - 1) / (2 - 1) = 0  →  score_supplier = int(15 + 0) = 15

    risk: supplier_count <= 1 → SUPPLIER_DEPENDENCY.
    Scores for other dimensions set high to isolate supplier effect.
    """
    state = _make_state(
        cash_on_hand_est=Decimal("50000"),  # ratio >> 2 → score_cash = 90+
        monthly_fixed_expenses_est=Decimal("10000"),
        monthly_sales_est=Decimal("100000"),
        monthly_inventory_cost_est=Decimal("40000"),  # margin=0.50 → excellent
        supplier_count=1,
        products=[],
    )
    result: HealthScoreResult = calculate_health_score(state)

    assert (
        15 <= result.score_supplier <= 44
    ), f"Expected score_supplier in [15, 44], got {result.score_supplier}"
    assert result.score_supplier == 15
    assert result.primary_risk_code == "SUPPLIER_DEPENDENCY"


# ── Test 4: total score formula ───────────────────────────────────────────────


def test_score_total_formula_correct() -> None:
    """
    Construct a state that produces predictable exact subscores (fórmula v2):

    cash_days = 6666.67 / (20000 / 30) ~= 10 → healthy boundary → score_cash = 70

    margin = (100000 - 55000 - 20000) / 100000 = 25000/100000 = 0.25
        kiosco band [0.18, 0.28) → pos=(0.25-0.18)/(0.28-0.18)=0.7
        score_margin = int(70 + 0.7*19) = int(83.3) = 83

    products: 4 products all healthy → score_stock = 100

    supplier_count=4 → band [4, 10) → pos=0 → score_supplier = 85

    prev_monthly_sales_est=0 → score_growth = 50 (sin historial, neutro)

    Formula v2: round(70*0.30 + 100*0.20 + 85*0.10 + 83*0.20 + 50*0.20)
              = round(21.0 + 20.0 + 8.5 + 16.6 + 10.0)
              = round(76.1) = 76
    """
    products = [_product(stock=50, threshold=5) for _ in range(4)]
    state = _make_state(
        cash_on_hand_est=Decimal("6666.67"),
        monthly_fixed_expenses_est=Decimal("20000"),
        monthly_sales_est=Decimal("100000"),
        monthly_inventory_cost_est=Decimal("55000"),
        prev_monthly_sales_est=Decimal("0"),  # sin historial → growth=50
        supplier_count=4,
        products=products,
    )
    result: HealthScoreResult = calculate_health_score(state)

    assert result.score_cash == 70
    assert result.score_margin == 83
    assert result.score_stock == 100
    assert result.score_supplier == 85
    assert result.score_growth == 50
    assert result.score_total == 76


def test_score_cap_with_low_completeness() -> None:
    products = [_product(stock=50, threshold=5) for _ in range(4)]
    state = _make_state(
        cash_on_hand_est=Decimal("50000"),
        monthly_fixed_expenses_est=Decimal("10000"),
        monthly_sales_est=Decimal("100000"),
        monthly_inventory_cost_est=Decimal("40000"),
        supplier_count=4,
        products=products,
        data_completeness_score=35.0,
        confidence_level="LOW",
    )

    result = calculate_health_score(state)

    assert result.score_total == 60


def test_health_engine_margin_no_sales_neutral() -> None:
    state = _make_state(monthly_sales_est=Decimal("0"))

    result = calculate_health_score(state)

    assert result.score_margin == 50


# ── Test 5: cash wins tie-break over margin ───────────────────────────────────


def test_primary_risk_cash_wins_when_cash_is_lower_than_margin() -> None:
    """
    cash_days = 3000 / (20000 / 30) = 4.5 → critical cash.
    margin = (100000 - 70000 - 20000) / 100000 = 0.10 → margin warning floor.

    stock and supplier are high (score_stock=50 neutral, score_supplier=70)
    so the tie is strictly between cash and margin.

    Tie-break: CASH > MARGIN → primary_risk_code == 'CASH_LOW'
    """
    state = _make_state(
        cash_on_hand_est=Decimal("3000"),
        monthly_fixed_expenses_est=Decimal("20000"),
        monthly_sales_est=Decimal("100000"),
        monthly_inventory_cost_est=Decimal("70000"),  # margin=0.10
        supplier_count=3,  # score_supplier = 70
        products=[],  # score_stock = 50 (neutral, no real data)
    )
    result: HealthScoreResult = calculate_health_score(state)

    assert result.score_cash < result.score_margin
    assert result.score_margin == 15, f"score_margin={result.score_margin}"
    assert result.primary_risk_code == "CASH_LOW"


def test_vertical_json_loader_reads_complete_config() -> None:
    config = load_vertical_heuristics("kiosco")

    assert config.business_type == "kiosco_almacen"
    assert config.cash_health.healthy_days_min == 10
    assert config.margin.healthy_min == 0.18
    assert config.inventory.rotation_days_max == 21
    assert config.supplier.stockout_sensitivity == "muy_alta"


def test_vertical_json_loader_falls_back_to_kiosco_for_unknown_vertical() -> None:
    config = load_vertical_heuristics("vertical_inexistente")

    assert config.business_type == "kiosco_almacen"
    assert config.margin.healthy_max == 0.28
