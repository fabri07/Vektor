"""
Health Engine v2 (Stage 5a).

Converts a BusinessState (from the Business State Layer) into a
HealthScoreResult by computing five subscores and combining them.

Scoring formula (v2)
--------------------
    health_score = cash*0.30 + stock*0.20 + supplier*0.10 + margin*0.20 + growth*0.20

v1 formula (pre-Stage-5a, kept for reference):
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

from app.domain.product import effective_threshold
from app.domain.verticals import parse_vertical
from app.heuristics.verticals import (
    BenchmarkProvenance,
    MarginBenchmark,
    VerticalHeuristicConfig,
    weakest_confidence,
)
from app.heuristics.verticals.loader import load_vertical_heuristics
from app.state.business_state_service import BusinessState, ProductSummary

# ── Risk metadata ─────────────────────────────────────────────────────────────

_RISK_DESCRIPTIONS: dict[str, str] = {
    "CASH_LOW": "Caja insuficiente para cubrir gastos operativos",
    "MARGIN_LOW": "Margen por debajo del umbral saludable para el vertical",
    "STOCK_CRITICAL": "Más del 30% de productos con stock crítico",
    "SUPPLIER_DEPENDENCY": "Alta dependencia de un único proveedor",
    "GROWTH_STAGNANT": "Las ventas son la dimensión más floja: crecen poco, no crecen o caen",
}

# Tie-break order: earlier index wins
_DIMENSION_PRIORITY = ["cash", "margin", "stock", "supplier", "growth"]

_DIMENSION_RISK_CODE: dict[str, str] = {
    "cash": "CASH_LOW",
    "margin": "MARGIN_LOW",
    "stock": "STOCK_CRITICAL",
    "supplier": "SUPPLIER_DEPENDENCY",
    "growth": "GROWTH_STAGNANT",
}


# ── Result dataclass ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HealthScoreResult:
    score_total: int
    score_cash: int
    #: ``None`` = margen AUSENTE (M·2b-3): sin costo suficiente no hay subscore;
    #: se excluye del total y del riesgo, igual que la caja desconocida.
    score_margin: int | None
    score_stock: int
    score_supplier: int
    score_growth: int  # Stage 5a — crecimiento de ventas 0-100
    primary_risk_code: str
    risk_description: str
    #: Confianza EFECTIVA = la más baja entre `data_confidence` y
    #: `benchmark_confidence`. Es el valor que gatea la regla de no-invención, y
    #: se lo hace más estricto a propósito: antes copiaba solo la completitud de
    #: los datos del tenant, así que un negocio con datos impecables medido
    #: contra un umbral que nadie fundamentó salía `HIGH`.
    confidence_level: str
    data_completeness_score: float
    #: Confianza por completitud de los datos del negocio (la de siempre).
    data_confidence: str
    #: Confianza por procedencia de la vara contra la que se lo mide.
    benchmark_confidence: str
    benchmark_provenance: BenchmarkProvenance
    # Fuente de la caja: "arqueo" (medido) | "onboarding" (estimado) |
    # "flujo" (cobertura líquida, sin saldo) | "desconocido" (sin dato → excluida del total).
    cash_source: str = "desconocido"
    #: Estado del margen (``domain/margen.py``): confirmado | estimado | ausente.
    margin_status: str = "ausente"
    margin_reason: str | None = None
    #: (ingreso − costo vendido − gastos fijos) / ingreso; ``None`` si ausente.
    margin_net_ratio: float | None = None


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


# ── Cash coverage score (sin saldo: modo flujo) ────────────────────────────────
# Cuando NO hay un saldo medido (arqueo) ni estimado, no se pueden calcular días
# de runway (runway = saldo ÷ gasto, y no hay saldo). En su lugar puntuamos la
# COBERTURA de flujo líquido: ¿los ingresos líquidos cubren los egresos líquidos
# en la ventana? Es honesto — no inventa un saldo — y sí es calculable de flujos.
# Líquido = cash + transfer + qr + debit (excluye fiado/cuenta corriente y
# crédito de acreditación diferida). Ver business_state_service._LIQUID_METHODS.
_COVERAGE_BANDS: list[_Band] = [
    # (ratio_low, ratio_high, score_low, score_high)
    (0.0, 0.7, 0, 14),  # quema caja fuerte (gasta mucho más de lo que entra)
    (0.7, 0.9, 15, 39),  # quema caja
    (0.9, 1.1, 40, 69),  # apenas cubre / breakeven
    (1.1, 1.5, 70, 89),  # genera caja sana
    (1.5, 3.0, 90, 100),  # fuerte generación de caja
]


def _score_cash_coverage(liquid_inflow: Decimal, liquid_outflow: Decimal) -> int:
    """
    ratio = ingresos_líquidos / egresos_líquidos en la ventana.
    Si no hay egresos líquidos no se puede calcular el ratio: devolvemos un valor
    sano-conservador cuando hay ingresos (no excelente, falta evidencia de egreso),
    o 50 neutro si tampoco hay ingresos.
    """
    if liquid_outflow <= 0:
        return 75 if liquid_inflow > 0 else 50
    ratio = float(liquid_inflow / liquid_outflow)
    return _band_score(ratio, _COVERAGE_BANDS)


# ── Margin score ──────────────────────────────────────────────────────────────


def _margin_bands(b: MarginBenchmark) -> list[_Band]:
    """
    Five bands built from the vertical's MarginBenchmark.
    When warning_below == healthy_min (all current verticals) the middle band
    has zero width and no value falls into it — the transition is sharp.
    """
    cap = b.healthy_max + 0.30  # reasonable upper anchor for top band
    return [
        (-1.0, b.critical_below, 0, 14),
        (b.critical_below, b.warning_below, 15, 39),
        (b.warning_below, b.healthy_min, 40, 69),  # zero-width for current verticals
        (b.healthy_min, b.healthy_max, 70, 89),
        (b.healthy_max, cap, 90, 100),
    ]


def margin_net_ratio(state: BusinessState) -> float | None:
    """Margen neto del subscore.

    Con ``margin_net_source == "mes_cerrado"`` (M·3-1, cuenta habilitada) es el
    margen neto del último mes cerrado de FactsService. Si no, la fórmula previa:
    (ingreso − costo vendido − gastos fijos) / ingreso.

    El ingreso y el costo vendido son los de FactsService (``domain/margen.py``,
    costo congelado por venta); los fijos, los del estado. ``None`` si el margen
    está ausente: sin costo suficiente no hay número, ni 0 ni un neutral (antes,
    sin ventas, daba 50; y restaba la mercadería COMPRADA, no la vendida: el
    −776% de ASTERIA).
    """
    if state.margin_net_source == "mes_cerrado":
        # M·3-1: el margen neto del último mes cerrado, tal cual lo midió
        # FactsService (el de la tarjeta y el chat). Sin él, no hay número.
        if state.margin_status == "ausente" or state.margin_net_pct is None:
            return None
        return state.margin_net_pct / 100
    base = state.margin_sales_base
    if state.margin_status == "ausente" or state.cogs_sold_est is None or base <= 0:
        return None
    return float((base - state.cogs_sold_est - state.monthly_fixed_expenses_est) / base)


def _score_margin(state: BusinessState, benchmark: MarginBenchmark) -> int | None:
    ratio = margin_net_ratio(state)
    if ratio is None:
        return None
    return _band_score(ratio, _margin_bands(benchmark))


_CONFIDENCE_ORDER = ["LOW", "MEDIUM", "HIGH"]


def _bajar_por_margen_ausente(confidence: str) -> str:
    """Un margen ausente baja la confianza un nivel, pero nunca a LOW por sí solo:
    LOW es la compuerta de no-invención y la define la completitud de los datos
    (decisión del 2026-10-11; la cobertura de costo no es completeness)."""
    if confidence not in _CONFIDENCE_ORDER:
        return confidence
    return "MEDIUM" if confidence == "HIGH" else confidence


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
        1 for p in products if p.stock_units <= effective_threshold(p.low_stock_threshold_units)
    )
    no_rotation = sum(
        1
        for p in products
        if p.units_sold_30d is not None
        and p.units_sold_30d <= 0
        and p.stock_units > effective_threshold(p.low_stock_threshold_units)
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
        1 for p in products if p.stock_units <= effective_threshold(p.low_stock_threshold_units)
    )
    return below > total * 0.3


# ── Supplier score ────────────────────────────────────────────────────────────


def _supplier_bands(config: VerticalHeuristicConfig) -> list[_Band]:
    # El mínimo sano lo declara el rubro. Antes se deducía de
    # `stockout_sensitivity` (alta/muy_alta → 4), que mezcla cuánto duele un
    # quiebre con en cuántas manos está la reposición: son cosas distintas y hay
    # rubros donde van en direcciones opuestas.
    healthy_min = config.supplier.min_healthy_suppliers
    warning_min = max(2, healthy_min - 1)
    return [
        (1, 2, 15, 44),
        (2, warning_min, 45, 69),
        (warning_min, healthy_min, 70, 84),
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


# ── Growth score ─────────────────────────────────────────────────────────────


_GROWTH_BANDS: list[_Band] = [
    # (low_pct, high_pct, score_low, score_high)
    (-100.0, -30.0, 0, 14),  # caída fuerte
    (-30.0, -10.0, 15, 39),  # caída moderada
    (-10.0, 5.0, 40, 69),  # estancamiento / leve variación
    (5.0, 25.0, 70, 89),  # crecimiento sano
    (25.0, 75.0, 90, 100),  # crecimiento fuerte
]


def _score_growth(current_sales: Decimal, prev_sales: Decimal) -> int:
    """
    Compara ventas del período actual vs período anterior (días 31-60).
    Si no hay historial (prev_sales == 0), retorna 50 (neutro — sin historia suficiente).
    """
    if prev_sales <= 0:
        return 50
    growth_pct = float((current_sales - prev_sales) / prev_sales * 100)
    return _band_score(growth_pct, _GROWTH_BANDS)


# ── Primary risk ──────────────────────────────────────────────────────────────


def _primary_risk(scores: dict[str, int]) -> str:
    """
    Return the dimension name with the lowest score.
    Ties broken by _DIMENSION_PRIORITY order (cash > margin > stock > supplier).
    Dimensiones ausentes (ej. caja desconocida) no se consideran como riesgo.
    """
    min_score = min(scores.values())
    for dim in _DIMENSION_PRIORITY:
        if scores.get(dim) == min_score:
            return dim
    return next(iter(scores))


# ── Public API ────────────────────────────────────────────────────────────────


def calculate_health_score(
    state: BusinessState,
    benchmark: MarginBenchmark | None = None,
    config: VerticalHeuristicConfig | None = None,
) -> HealthScoreResult:
    """Compute HealthScoreResult from a BusinessState.

    Si se pasa `benchmark` explícitamente (ej. data-driven desde analytics_events),
    se usa ese. De lo contrario carga el JSON del vertical (que levanta si el
    `vertical_code` del estado no es uno de los canónicos).
    """
    if config is None:
        config = load_vertical_heuristics(parse_vertical(state.vertical_code))
    if benchmark is None:
        benchmark = config.margin

    # ── Caja: 3 modos según la fuente del dato (ver business_state_service) ──────
    #   arqueo / onboarding → días de runway (hay saldo)
    #   flujo               → cobertura líquida (no hay saldo, sí movimientos)
    #   desconocido         → sin dato: caja excluida del total y del riesgo
    cash_source = getattr(state, "cash_source", "desconocido")
    if cash_source == "flujo":
        s_cash = _score_cash_coverage(
            getattr(state, "liquid_inflow_est", Decimal("0")),
            getattr(state, "liquid_outflow_est", Decimal("0")),
        )
        cash_known = True
    elif cash_source in ("arqueo", "onboarding"):
        s_cash = _score_cash(state.cash_on_hand_est, state.monthly_fixed_expenses_est, config)
        cash_known = True
    else:  # desconocido — placeholder neutro, NO se muestra (gated por cash_source)
        s_cash = 50
        cash_known = False

    s_margin = _score_margin(state, benchmark)
    net_ratio = margin_net_ratio(state)
    s_stock = _score_stock(state.products, config)
    s_supplier = _score_supplier(state.supplier_count, config)
    s_growth = _score_growth(state.monthly_sales_est, state.prev_monthly_sales_est)

    scores: dict[str, int | None] = {
        "cash": s_cash,
        "margin": s_margin,
        "stock": s_stock,
        "supplier": s_supplier,
        "growth": s_growth,
    }

    # Fórmula v2 (Stage 5a): cash×0.30 + stock×0.20 + supplier×0.10 + margin×0.20 + growth×0.20.
    # Una dimensión AUSENTE (caja desconocida, margen sin costo suficiente) se
    # excluye y se re-normalizan los pesos restantes (no-invention: no metemos un
    # 0/50 falso que arrastre el total).
    weights = {"cash": 0.30, "stock": 0.20, "supplier": 0.10, "margin": 0.20, "growth": 0.20}
    ausentes = {d for d, v in scores.items() if v is None}
    if not cash_known:
        ausentes.add("cash")
    present: dict[str, int] = {
        d: v for d, v in scores.items() if d not in ausentes and v is not None
    }
    total_weight = sum(weights[d] for d in present)
    total = round(sum(v * weights[d] for d, v in present.items()) / total_weight)
    if state.data_completeness_score < 40:
        total = min(total, 60)
    elif state.data_completeness_score < 60:
        total = min(total, 75)

    # Sin período anterior el crecimiento es un 50 neutro, no una medición: no
    # puede ser el riesgo principal (diría «caída» sin con qué comparar).
    candidatos = (
        {d: v for d, v in present.items() if d != "growth"}
        if state.prev_monthly_sales_est <= 0 and len(present) > 1
        else present
    )
    weakest_dim = _primary_risk(candidatos)
    risk_code = _DIMENSION_RISK_CODE[weakest_dim]

    benchmark_confidence = benchmark.confidence
    confidence = weakest_confidence(state.confidence_level, benchmark_confidence)
    if s_margin is None:
        confidence = _bajar_por_margen_ausente(confidence)
    return HealthScoreResult(
        score_total=total,
        score_cash=s_cash,
        score_margin=s_margin,
        score_stock=s_stock,
        score_supplier=s_supplier,
        score_growth=s_growth,
        primary_risk_code=risk_code,
        risk_description=_RISK_DESCRIPTIONS[risk_code],
        confidence_level=confidence,
        data_completeness_score=state.data_completeness_score,
        data_confidence=state.confidence_level,
        benchmark_confidence=benchmark_confidence,
        benchmark_provenance=benchmark.provenance,
        cash_source=cash_source,
        margin_status=state.margin_status if s_margin is not None else "ausente",
        margin_reason=state.margin_reason,
        margin_net_ratio=net_ratio,
    )
