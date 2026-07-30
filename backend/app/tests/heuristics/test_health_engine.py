"""
Tests for Health Engine v2 (Stage 5a).

All tests use hardcoded BusinessState objects — no DB, no Redis.
Numeric assertions are derived from the strict linear interpolation spec:
    score = int(s_low + pos * (s_high - s_low))
    where pos = (value - band_low) / (band_high - band_low)

Formula v2: cash×0.30 + stock×0.20 + supplier×0.10 + margin×0.20 + growth×0.20
"""

from __future__ import annotations

import shutil
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from app.domain.verticals import Vertical
from app.heuristics.health_engine import HealthScoreResult, calculate_health_score
from app.heuristics.verticals import BenchmarkProvenance, MarginBenchmark, loader
from app.heuristics.verticals.loader import load_vertical_heuristics
from app.state.business_state_service import BusinessState, ProductSummary

# Benchmark de kiosco desde la MISMA fuente que el engine (el módulo estático
# app/heuristics/verticals/kiosco.py se eliminó: era una 4ª copia de los JSON).

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
    vertical_code: str = Vertical.KIOSCO_ALMACEN.value,
    monthly_sales_est: Decimal = Decimal("100000"),
    monthly_inventory_cost_est: Decimal = Decimal("60000"),
    monthly_fixed_expenses_est: Decimal = Decimal("17000"),
    cash_on_hand_est: Decimal = Decimal("40000"),
    prev_monthly_sales_est: Decimal = Decimal("0"),
    supplier_count: int = 3,
    products: list[ProductSummary] | None = None,
    data_completeness_score: float = 75.0,
    confidence_level: str = "MEDIUM",
    # Default "onboarding" = modo balance (días de runway sobre cash_on_hand_est),
    # que es lo que asumen los tests de caja con saldo.
    cash_source: str = "onboarding",
    liquid_inflow_est: Decimal = Decimal("0"),
    liquid_outflow_est: Decimal = Decimal("0"),
) -> BusinessState:
    return BusinessState(
        snapshot_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        vertical_code=vertical_code,
        data_completeness_score=data_completeness_score,
        confidence_level=confidence_level,
        monthly_sales_est=monthly_sales_est,
        monthly_inventory_cost_est=monthly_inventory_cost_est,
        monthly_fixed_expenses_est=monthly_fixed_expenses_est,
        cash_on_hand_est=cash_on_hand_est,
        prev_monthly_sales_est=prev_monthly_sales_est,
        product_count=len(products) if products else 0,
        supplier_count=supplier_count,
        products=products or [],
        main_concern=None,
        cash_source=cash_source,
        liquid_inflow_est=liquid_inflow_est,
        liquid_outflow_est=liquid_outflow_est,
    )


# ── Test 1: kiosco healthy margin scores high ─────────────────────────────────


def test_kiosco_healthy_margin_scores_high() -> None:
    """Un margen dentro del rango sano del rubro cae en la banda [70, 89].

    El margen objetivo se DERIVA del benchmark vigente en vez de fijarse a mano:
    con números escritos en el test, recalibrar el rubro dejaba este caso fuera
    de la banda sana y el test fallaba sin que hubiera ninguna regresión — es lo
    que pasó al corregir los umbrales de kiosco contra la fuente de INDEC/CAME.
    """
    benchmark = load_vertical_heuristics(Vertical.KIOSCO_ALMACEN).margin
    objetivo = (benchmark.healthy_min + benchmark.healthy_max) / 2  # centro del rango sano

    # margen = (ventas - mercadería - gastos) / ventas  →  se fija mercadería
    # para que el margen resultante sea exactamente `objetivo`.
    ventas = Decimal("100000")
    gastos = Decimal("17000")
    mercaderia = ventas - gastos - (ventas * Decimal(str(objetivo)))
    state = _make_state(
        monthly_sales_est=ventas,
        monthly_inventory_cost_est=mercaderia,
        monthly_fixed_expenses_est=gastos,
    )
    result: HealthScoreResult = calculate_health_score(state)

    assert (
        70 <= result.score_margin <= 89
    ), f"Expected score_margin in [70, 89], got {result.score_margin}"


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
        banda [0.18, 0.28) del benchmark INYECTADO abajo → pos=0.7
        score_margin = int(70 + 0.7*19) = int(83.3) = 83

    El benchmark va explícito y NO sale del JSON de kiosco: este test mide la
    fórmula ponderada, no la calibración del rubro. Cuando dependía del JSON, una
    recalibración de kiosco lo hacía fallar sin que la fórmula hubiera cambiado.

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
    result: HealthScoreResult = calculate_health_score(
        state,
        benchmark=MarginBenchmark(
            critical_below=0.10,
            warning_below=0.18,
            healthy_min=0.18,
            healthy_max=0.28,
            provenance=BenchmarkProvenance.TENANT_OVERRIDE,
        ),
    )

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
    margin = (100000 - 70000 - 20000) / 100000 = 0.10

    stock and supplier are high (score_stock=50 neutral, score_supplier=70)
    so the tie is strictly between cash and margin.

    Tie-break: CASH > MARGIN → primary_risk_code == 'CASH_LOW'

    Lo que se afirma es la RELACIÓN entre las dos dimensiones, no el valor
    absoluto del margen: cuál es el riesgo principal no depende de la
    calibración del rubro, y fijar el subscore exacto acá ataba este test a los
    umbrales de kiosco sin agregar nada a lo que mide.
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
    assert result.primary_risk_code == "CASH_LOW"


def test_vertical_json_loader_reads_complete_config() -> None:
    """El loader traslada el JSON tal cual, sin perder ni transformar bloques.

    Se compara contra el ARCHIVO, no contra números escritos en el test. Con
    valores congelados acá, este test dejaba de medir al loader y pasaba a medir
    la calibración: recalibrar un rubro contra su fuente sectorial lo rompía
    aunque el loader siguiera funcionando perfecto.
    """
    import json  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    ruta = (
        Path(__file__).resolve().parents[2]
        / "application"
        / "data"
        / "heuristics"
        / f"{Vertical.KIOSCO_ALMACEN.value}.json"
    )
    data = json.loads(ruta.read_text(encoding="utf-8"))
    config = load_vertical_heuristics(Vertical.KIOSCO_ALMACEN)

    assert config.business_type == Vertical.KIOSCO_ALMACEN
    assert config.cash_health.healthy_days_min == data["cash_health"]["healthy_days_min"]
    assert config.margin.healthy_min == data["margin"]["healthy_min"]
    assert config.margin.healthy_max == data["margin"]["healthy_max"]
    assert config.inventory.rotation_days_max == data["inventory"]["rotation_days_max"]
    assert config.supplier.stockout_sensitivity == data["supplier"]["stockout_sensitivity"]
    assert config.seasonality == data["seasonality"]


def test_vertical_json_loader_raises_instead_of_serving_another_vertical(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Sin fallback: si falta el JSON del rubro, el loader propaga en vez de
    scorear el negocio con los benchmarks de otro.

    NO se prueba con `load_vertical_heuristics(parse_vertical("kiosco"))`: ahí
    la excepción la tira `parse_vertical` al evaluar el argumento y el loader ni
    se ejecuta — el test quedaría verde aunque se borrara su cuerpo entero. El
    rechazo del código corto legado se mide en `test_verticals.py`, donde el SUT
    ES `parse_vertical`.

    El temporal tiene el JSON de kiosco y NO el de limpieza a propósito: con un
    directorio vacío, un loader que volviera a caer a kiosco levantaría igual
    (por el archivo destino, que tampoco estaría) y el test pasaría por la razón
    equivocada.

    El `cache_clear()` de los dos lados es obligatorio: `load_vertical_heuristics`
    tiene `lru_cache`, así que sin limpiar antes devolvería el config ya cargado
    (verde por la razón equivocada) y sin limpiar después envenenaría al resto de
    la suite con el directorio temporal.
    """
    shutil.copy(
        loader._HEURISTICS_DIR / f"{Vertical.KIOSCO_ALMACEN.value}.json",
        tmp_path / f"{Vertical.KIOSCO_ALMACEN.value}.json",
    )
    load_vertical_heuristics.cache_clear()
    monkeypatch.setattr(loader, "_HEURISTICS_DIR", tmp_path)
    try:
        with pytest.raises(FileNotFoundError):
            load_vertical_heuristics(Vertical.LIMPIEZA)
    finally:
        load_vertical_heuristics.cache_clear()


# ── Modo cobertura de caja (sin saldo: cash_source="flujo") ───────────────────


def test_cash_coverage_mode_scores_from_liquid_flow() -> None:
    """Sin saldo pero con movimientos líquidos: el score sale de la cobertura
    in/out, no de días de runway. ratio 2.0 → banda [1.5,3.0] → 90+."""
    state = _make_state(
        cash_source="flujo",
        cash_on_hand_est=Decimal("0"),  # sin saldo
        liquid_inflow_est=Decimal("2000000"),
        liquid_outflow_est=Decimal("1000000"),  # ratio = 2.0
    )
    result = calculate_health_score(state)
    assert result.cash_source == "flujo"
    assert result.score_cash >= 90, f"score_cash={result.score_cash}"
    # No dispara CASH_LOW con caja sana
    assert result.primary_risk_code != "CASH_LOW"


def test_cash_coverage_burning_cash_scores_low() -> None:
    """Egresos líquidos > ingresos → quema caja → score bajo + CASH_LOW."""
    state = _make_state(
        cash_source="flujo",
        cash_on_hand_est=Decimal("0"),
        liquid_inflow_est=Decimal("500"),
        liquid_outflow_est=Decimal("1000"),  # ratio = 0.5
        products=[],
        supplier_count=5,
    )
    result = calculate_health_score(state)
    assert result.score_cash <= 14, f"score_cash={result.score_cash}"


def test_unknown_cash_excluded_from_total_and_risk() -> None:
    """cash_source='desconocido': caja NO cuenta en el total (pesos re-normalizados)
    ni puede ser el riesgo primario. No más CASH_LOW falso."""
    state = _make_state(
        cash_source="desconocido",
        cash_on_hand_est=Decimal("0"),
        liquid_inflow_est=Decimal("0"),
        liquid_outflow_est=Decimal("0"),
    )
    result = calculate_health_score(state)
    assert result.cash_source == "desconocido"
    assert result.primary_risk_code != "CASH_LOW"
    # El total se computa solo con las dimensiones conocidas (sin caja=50 arrastrando).
    # Pesos renormalizados sobre stock/supplier/margin/growth (0.20+0.10+0.20+0.20=0.70).
    expected = round(
        (
            result.score_stock * 0.20
            + result.score_supplier * 0.10
            + result.score_margin * 0.20
            + result.score_growth * 0.20
        )
        / 0.70
    )
    assert result.score_total == expected, f"total={result.score_total} expected={expected}"
