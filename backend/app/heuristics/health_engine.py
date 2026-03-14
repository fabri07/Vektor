"""
Health Engine v1.

Converts a BusinessState (from the Business State Layer) into a
HealthScoreResult by computing four subscores and combining them.

Scoring formula
---------------
    health_score = cash*0.30 + margin*0.30 + stock*0.25 + supplier*0.15

All subscores use strict linear interpolation within each band so that
a value at the midpoint of a band produces the midpoint of its score range.

Primary risk
------------
The weakest subscore's associated risk code. Tie-break priority:
    CASH_LOW > MARGIN_LOW > STOCK_CRITICAL > SUPPLIER_DEPENDENCY
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.heuristics.verticals import MarginBenchmark
from app.heuristics.verticals.deco_hogar import BENCHMARK as DECO_BENCHMARK
from app.heuristics.verticals.kiosco import BENCHMARK as KIOSCO_BENCHMARK
from app.heuristics.verticals.limpieza import BENCHMARK as LIMPIEZA_BENCHMARK
from app.state.business_state_service import BusinessState, ProductSummary

# ── Vertical registry ─────────────────────────────────────────────────────────

_MARGIN_BENCHMARKS: dict[str, MarginBenchmark] = {
    "kiosco": KIOSCO_BENCHMARK,
    "decoracion_hogar": DECO_BENCHMARK,
    "limpieza": LIMPIEZA_BENCHMARK,
}

# ── Risk metadata ─────────────────────────────────────────────────────────────

_RISK_DESCRIPTIONS: dict[str, str] = {
    "CASH_LOW": "Caja insuficiente para cubrir gastos operativos",
    "MARGIN_LOW": "Margen estimado por debajo del umbral saludable para el vertical",
    "STOCK_CRITICAL": "Más del 30% de productos con stock crítico",
    "SUPPLIER_DEPENDENCY": "Alta dependencia de un único proveedor",
}

# Tie-break order: earlier index wins
_DIMENSION_PRIORITY = ["cash", "margin", "stock", "supplier"]

_DIMENSION_RISK_CODE: dict[str, str] = {
    "cash": "CASH_LOW",
    "margin": "MARGIN_LOW",
    "stock": "STOCK_CRITICAL",
    "supplier": "SUPPLIER_DEPENDENCY",
}


# ── Result dataclass ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HealthScoreResult:
    score_total: int
    score_cash: int
    score_margin: int
    score_stock: int
    score_supplier: int
    primary_risk_code: str
    risk_description: str
    confidence_level: str
    data_completeness_score: float


# ── Band interpolation ────────────────────────────────────────────────────────

# Each band is (low, high, score_low, score_high).
# Invariant: bands must be sorted by low, non-overlapping.
_Band = tuple[float, float, int, int]


def _band_score(value: float, bands: list[_Band]) -> int:
    """
    Strict linear interpolation within the matching band.

    - value < first band low  → score_low of first band.
    - value >= last band high → score_high of last band.
    - value at band boundary  → score_low of the band it enters.

    Example (cash, ratio=1.6 in band [1.2, 2.0) → [70, 89]):
        pos  = (1.6 - 1.2) / (2.0 - 1.2) = 0.5
        score = int(70 + 0.5 * (89 - 70)) = int(79.5) = 79
    """
    for low, high, s_low, s_high in bands:
        if value < high:
            if value <= low:
                return s_low
            span = high - low
            pos = (value - low) / span
            return int(s_low + pos * (s_high - s_low))
    # Above all bands → cap at last band's maximum
    return bands[-1][3]


# ── Cash score ────────────────────────────────────────────────────────────────

_CASH_BANDS: list[_Band] = [
    (0.0, 0.3,  0,  14),
    (0.3, 0.7, 15,  39),
    (0.7, 1.2, 40,  69),
    (1.2, 2.0, 70,  89),
    (2.0, 4.0, 90, 100),  # ratio >= 4.0 → 100
]


def _score_cash(cash_on_hand: Decimal, monthly_fixed_expenses: Decimal) -> int:
    """
    cash_ratio = cash_on_hand / monthly_fixed_expenses.
    If expenses are zero (no data), return 75 as a neutral default.
    """
    if monthly_fixed_expenses <= 0:
        return 75
    ratio = float(cash_on_hand / monthly_fixed_expenses)
    return _band_score(ratio, _CASH_BANDS)


# ── Margin score ──────────────────────────────────────────────────────────────


def _margin_bands(b: MarginBenchmark) -> list[_Band]:
    """
    Five bands built from the vertical's MarginBenchmark.
    When warning_below == healthy_min (all current verticals) the middle band
    has zero width and no value falls into it — the transition is sharp.
    """
    cap = b.healthy_max + 0.30  # reasonable upper anchor for top band
    return [
        (-1.0,              b.critical_below, 0,  14),
        (b.critical_below,  b.warning_below, 15,  39),
        (b.warning_below,   b.healthy_min,   40,  69),  # zero-width for current verticals
        (b.healthy_min,     b.healthy_max,   70,  89),
        (b.healthy_max,     cap,             90, 100),
    ]


def _score_margin(state: BusinessState, benchmark: MarginBenchmark) -> int:
    """
    estimated_margin = (ventas - inventario - gastos_fijos) / ventas
    If no sales data → score 0.
    """
    sales = state.monthly_sales_est
    if sales <= 0:
        return 0
    margin = float(
        (sales - state.monthly_inventory_cost_est - state.monthly_fixed_expenses_est) / sales
    )
    return _band_score(margin, _margin_bands(benchmark))


# ── Stock score ───────────────────────────────────────────────────────────────


def _score_stock(products: list[ProductSummary]) -> int:
    """
    If real product data exists: stock_health = 1 - (below_threshold / total).
    If no products loaded (only estimate): return 50 (neutral).
    """
    if not products:
        return 50
    total = len(products)
    below = sum(
        1 for p in products if p.stock_units <= p.low_stock_threshold_units
    )
    stock_health = 1.0 - (below / total)
    return int(stock_health * 100)


def _stock_is_critical(products: list[ProductSummary]) -> bool:
    if not products:
        return False
    total = len(products)
    below = sum(
        1 for p in products if p.stock_units <= p.low_stock_threshold_units
    )
    return below > total * 0.3


# ── Supplier score ────────────────────────────────────────────────────────────

_SUPPLIER_BANDS: list[_Band] = [
    (1,  2, 15, 44),
    (2,  3, 45, 69),
    (3,  4, 70, 84),
    (4, 10, 85, 100),  # count >= 10 → 100
]


def _score_supplier(supplier_count: int) -> int:
    """
    Discrete integer count mapped via bands.
    count=0 → 0 (always).
    count=1 → 15 (bottom of its band, pos=0).
    count=4 → 85 (bottom of top band).
    count>=10 → 100.
    """
    if supplier_count <= 0:
        return 0
    return _band_score(float(supplier_count), _SUPPLIER_BANDS)


# ── Primary risk ──────────────────────────────────────────────────────────────


def _primary_risk(scores: dict[str, int]) -> str:
    """
    Return the dimension name with the lowest score.
    Ties broken by _DIMENSION_PRIORITY order (cash > margin > stock > supplier).
    """
    min_score = min(scores.values())
    for dim in _DIMENSION_PRIORITY:
        if scores[dim] == min_score:
            return dim
    # unreachable, but satisfies type checker
    return _DIMENSION_PRIORITY[0]


# ── Public API ────────────────────────────────────────────────────────────────


def calculate_health_score(state: BusinessState) -> HealthScoreResult:
    """
    Compute HealthScoreResult from a BusinessState.

    Raises
    ------
    ValueError
        If state.vertical_code is not registered.
    """
    benchmark = _MARGIN_BENCHMARKS.get(state.vertical_code)
    if benchmark is None:
        raise ValueError(f"Unknown vertical_code: {state.vertical_code!r}")

    s_cash = _score_cash(state.cash_on_hand_est, state.monthly_fixed_expenses_est)
    s_margin = _score_margin(state, benchmark)
    s_stock = _score_stock(state.products)
    s_supplier = _score_supplier(state.supplier_count)

    scores = {
        "cash": s_cash,
        "margin": s_margin,
        "stock": s_stock,
        "supplier": s_supplier,
    }

    total = round(
        s_cash * 0.30
        + s_margin * 0.30
        + s_stock * 0.25
        + s_supplier * 0.15
    )

    weakest_dim = _primary_risk(scores)
    risk_code = _DIMENSION_RISK_CODE[weakest_dim]

    return HealthScoreResult(
        score_total=total,
        score_cash=s_cash,
        score_margin=s_margin,
        score_stock=s_stock,
        score_supplier=s_supplier,
        primary_risk_code=risk_code,
        risk_description=_RISK_DESCRIPTIONS[risk_code],
        confidence_level=state.confidence_level,
        data_completeness_score=state.data_completeness_score,
    )
