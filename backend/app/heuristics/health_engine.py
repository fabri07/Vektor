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

from app.heuristics.verticals import MarginBenchmark, VerticalHeuristicConfig
from app.heuristics.verticals.loader import load_vertical_heuristics
from app.state.business_state_service import BusinessState, ProductSummary

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

def _cash_bands(config: VerticalHeuristicConfig) -> list[_Band]:
    critical = config.cash_health.critical_days_below
    warning = config.cash_health.warning_days_min
    healthy = config.cash_health.healthy_days_min
    excellent = max(healthy * 2, healthy + 1)
    return [
        (0.0, critical, 0, 14),
        (critical, warning, 15, 39),
        (warning, healthy, 40, 69),
        (healthy, excellent, 70, 89),
        (excellent, excellent * 2, 90, 100),
    ]


def _score_cash(
    cash_on_hand: Decimal,
    monthly_fixed_expenses: Decimal,
    config: VerticalHeuristicConfig,
) -> int:
    """
    cash_days = cash_on_hand / daily fixed expenses.
    If expenses are zero (no data), return 60 as a neutral-low default.
    """
    if monthly_fixed_expenses <= 0:
        return 60
    daily_fixed_expenses = monthly_fixed_expenses / Decimal("30")
    if daily_fixed_expenses <= 0:
        return 60
    cash_days = float(cash_on_hand / daily_fixed_expenses)
    return _band_score(cash_days, _cash_bands(config))


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
    If no sales estimate is available, return 50 as a neutral default.
    """
    sales = state.monthly_sales_est
    if sales <= 0:
        return 50
    margin = float(
        (sales - state.monthly_inventory_cost_est - state.monthly_fixed_expenses_est) / sales
    )
    return _band_score(margin, _margin_bands(benchmark))


# ── Stock score ───────────────────────────────────────────────────────────────


def _score_stock(products: list[ProductSummary], config: VerticalHeuristicConfig) -> int:
    """
    If real product data exists: combine low-stock pressure with no-rotation pressure.
    If no products loaded (only estimate): return 50 (neutral).
    """
    if not products:
        return 50
    total = len(products)
    below = sum(
        1 for p in products if p.stock_units <= p.low_stock_threshold_units
    )
    no_rotation = sum(
        1
        for p in products
        if p.units_sold_30d is not None
        and p.units_sold_30d <= 0
        and p.stock_units > p.low_stock_threshold_units
    )
    rotation_pressure = 0.0
    if config.inventory.rotation_days_max > 0:
        rotation_pressure = min(no_rotation / total, 1.0)

    stock_health = 1.0 - min((below / total) * 0.70 + rotation_pressure * 0.30, 1.0)
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

def _supplier_bands(config: VerticalHeuristicConfig) -> list[_Band]:
    sensitivity = config.supplier.stockout_sensitivity.lower()
    healthy_min = 4 if sensitivity in {"alta", "muy_alta"} else 3
    warning_min = max(2, healthy_min - 1)
    return [
        (1,  2, 15, 44),
        (2,  warning_min, 45, 69),
        (warning_min,  healthy_min, 70, 84),
        (healthy_min, healthy_min + 6, 85, 100),
    ]


def _score_supplier(supplier_count: int, config: VerticalHeuristicConfig) -> int:
    """
    Discrete integer count mapped via bands.
    count=0 → 0 (always).
    count=1 → 15 (bottom of its band, pos=0).
    count=4 → 85 (bottom of top band).
    count>=10 → 100.
    """
    if supplier_count <= 0:
        return 0
    return _band_score(float(supplier_count), _supplier_bands(config))


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


def calculate_health_score(
    state: BusinessState,
    benchmark: MarginBenchmark | None = None,
    config: VerticalHeuristicConfig | None = None,
) -> HealthScoreResult:
    """Compute HealthScoreResult from a BusinessState.

    Si se pasa `benchmark` explícitamente (ej. data-driven desde analytics_events),
    se usa ese. De lo contrario carga desde el JSON del vertical con fallback a hardcode.
    """
    if config is None:
        config = load_vertical_heuristics(state.vertical_code)
    if benchmark is None:
        benchmark = config.margin

    s_cash = _score_cash(
        state.cash_on_hand_est,
        state.monthly_fixed_expenses_est,
        config,
    )
    s_margin = _score_margin(state, benchmark)
    s_stock = _score_stock(state.products, config)
    s_supplier = _score_supplier(state.supplier_count, config)

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
    if state.data_completeness_score < 40:
        total = min(total, 60)
    elif state.data_completeness_score < 60:
        total = min(total, 75)

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
