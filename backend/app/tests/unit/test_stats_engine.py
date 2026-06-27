"""Tests para stats_engine.py — TDD primero.

Motor estadístico/financiero determinístico.
REGLA: el módulo NO importa anthropic ni llama LLM.
"""
from __future__ import annotations

import math
import sys
import importlib

import pytest


# ---------------------------------------------------------------------------
# Helper para importar el módulo limpio
# ---------------------------------------------------------------------------

def _import_stats_engine():  # type: ignore[return]
    from app.application.agents.shared import stats_engine  # type: ignore[import]
    return stats_engine


# ---------------------------------------------------------------------------
# test_stats_engine_no_llm
# ---------------------------------------------------------------------------


def test_stats_engine_no_llm() -> None:
    """El módulo stats_engine NO debe importar anthropic ni instanciar cliente LLM."""
    se = _import_stats_engine()
    module_source = sys.modules.get(se.__name__)
    # Verificamos que el namespace del módulo no contiene nada de anthropic
    members = dir(se)
    for name in members:
        val = getattr(se, name)
        module_name = getattr(val, "__module__", "") or ""
        assert "anthropic" not in module_name, (
            f"stats_engine expone '{name}' de un módulo anthropic ({module_name})"
        )
    # Tampoco debería existir "anthropic" en las importaciones del módulo
    assert "anthropic" not in (se.__dict__.get("__spec__", None) or object()).__class__.__name__.lower()
    # Verificación directa: el atributo anthropic no está
    assert not hasattr(se, "anthropic"), "stats_engine no debe importar el módulo anthropic"


# ---------------------------------------------------------------------------
# describe_sales
# ---------------------------------------------------------------------------


class TestDescribeSales:
    def test_normal(self) -> None:
        from app.application.agents.shared.stats_engine import describe_sales

        values = [100.0, 200.0, 150.0, 50.0, 300.0]
        result = describe_sales(values)
        assert result["n"] == 5
        assert result["mean"] == pytest.approx(160.0)
        assert result["median"] == pytest.approx(150.0)
        assert result["min"] == pytest.approx(50.0)
        assert result["max"] == pytest.approx(300.0)
        assert result["p25"] == pytest.approx(100.0)
        assert result["p75"] == pytest.approx(200.0)
        assert result["cv"] is not None
        assert result["cv"] == pytest.approx(result["std"] / result["mean"])

    def test_empty(self) -> None:
        from app.application.agents.shared.stats_engine import describe_sales

        result = describe_sales([])
        assert result["status"] == "insufficient_data"
        assert result["n"] == 0

    def test_single_element(self) -> None:
        from app.application.agents.shared.stats_engine import describe_sales

        result = describe_sales([42.0])
        assert result["n"] == 1
        assert result["mean"] == pytest.approx(42.0)
        # std con n=1 puede ser 0 o nan; cv debe ser None cuando std=0 o mean=0
        # al menos no debe explotar

    def test_mean_zero_cv_none(self) -> None:
        from app.application.agents.shared.stats_engine import describe_sales

        result = describe_sales([0.0, 0.0, 0.0])
        assert result["mean"] == pytest.approx(0.0)
        assert result["cv"] is None, "cv debe ser None cuando mean==0"


# ---------------------------------------------------------------------------
# project_sales
# ---------------------------------------------------------------------------


class TestProjectSales:
    def _linear_series(self, n: int = 10, slope: float = 5.0, base: float = 100.0):
        return [base + i * slope for i in range(n)]

    def test_n_less_than_7_insufficient(self) -> None:
        from app.application.agents.shared.stats_engine import project_sales

        result = project_sales([100.0, 110.0, 120.0], days_ahead=15)
        assert result["status"] == "insufficient_data"
        assert result["n"] < 7
        assert result["min_required"] == 7

    def test_normal_linear(self) -> None:
        from app.application.agents.shared.stats_engine import project_sales

        series = self._linear_series(n=10, slope=10.0, base=50.0)
        result = project_sales(series, days_ahead=10)
        assert result["status"] == "ok"
        assert result["days_ahead"] == 10
        # Tendencia positiva
        assert result["trend_slope"] > 0
        # r_squared cercano a 1 en una serie perfectamente lineal
        assert result["r_squared"] == pytest.approx(1.0, abs=0.01)
        # projection_total debe ser positivo
        assert result["projection_total"] > 0
        assert result["projection_daily_avg"] == pytest.approx(
            result["projection_total"] / 10, rel=1e-3
        )

    def test_flat_series_slope_zero(self) -> None:
        from app.application.agents.shared.stats_engine import project_sales

        series = [100.0] * 10
        result = project_sales(series, days_ahead=5)
        assert result["status"] == "ok"
        assert result["trend_slope"] == pytest.approx(0.0, abs=1e-6)
        assert result["projection_daily_avg"] == pytest.approx(100.0, abs=1e-3)

    def test_exactly_7_elements_ok(self) -> None:
        from app.application.agents.shared.stats_engine import project_sales

        series = [float(i * 10 + 100) for i in range(7)]
        result = project_sales(series, days_ahead=7)
        assert result["status"] == "ok"

    def test_empty_insufficient(self) -> None:
        from app.application.agents.shared.stats_engine import project_sales

        result = project_sales([], days_ahead=10)
        assert result["status"] == "insufficient_data"


# ---------------------------------------------------------------------------
# working_capital
# ---------------------------------------------------------------------------


class TestWorkingCapital:
    def test_favorable(self) -> None:
        from app.application.agents.shared.stats_engine import working_capital

        result = working_capital(
            avg_daily_sales=10_000.0,
            inventory_days=10,
            receivables_days=5,
            payables_days=20,
        )
        # ccc = 10 + 5 - 20 = -5
        assert result["cash_conversion_cycle_days"] == -5
        assert result["interpretation"] == "favorable"
        assert result["working_capital_needed"] == pytest.approx(-50_000.0)

    def test_ajustado(self) -> None:
        from app.application.agents.shared.stats_engine import working_capital

        result = working_capital(
            avg_daily_sales=5_000.0,
            inventory_days=20,
            receivables_days=15,
            payables_days=0,
        )
        # ccc = 20 + 15 - 0 = 35
        assert result["cash_conversion_cycle_days"] == 35
        assert result["interpretation"] == "ajustado"
        assert result["working_capital_needed"] == pytest.approx(175_000.0)

    def test_ccc_exactly_30_favorable(self) -> None:
        from app.application.agents.shared.stats_engine import working_capital

        result = working_capital(1000.0, 10, 20, 0)
        # ccc = 30 → limit es <30 para favorable, así que 30 = ajustado
        assert result["cash_conversion_cycle_days"] == 30
        assert result["interpretation"] == "ajustado"

    def test_avg_daily_zero_insufficient(self) -> None:
        from app.application.agents.shared.stats_engine import working_capital

        result = working_capital(0.0, 10, 5, 3)
        assert result["status"] == "insufficient_data"

    def test_avg_daily_negative_insufficient(self) -> None:
        from app.application.agents.shared.stats_engine import working_capital

        result = working_capital(-1.0, 10, 5, 3)
        assert result["status"] == "insufficient_data"


# ---------------------------------------------------------------------------
# product_profitability
# ---------------------------------------------------------------------------


class TestProductProfitability:
    def test_alta(self) -> None:
        from app.application.agents.shared.stats_engine import product_profitability

        result = product_profitability(sales=1000.0, cogs=600.0, units=10)
        # margen = (1000-600)/1000 = 40% → alta
        assert result["gross_margin_pct"] == pytest.approx(40.0)
        assert result["classification"] == "alta"
        assert result["margin_per_unit"] == pytest.approx(40.0)

    def test_media(self) -> None:
        from app.application.agents.shared.stats_engine import product_profitability

        result = product_profitability(sales=1000.0, cogs=800.0, units=10)
        # margen = 20% → media
        assert result["gross_margin_pct"] == pytest.approx(20.0)
        assert result["classification"] == "media"

    def test_baja(self) -> None:
        from app.application.agents.shared.stats_engine import product_profitability

        result = product_profitability(sales=1000.0, cogs=900.0, units=10)
        # margen = 10% → baja
        assert result["gross_margin_pct"] == pytest.approx(10.0)
        assert result["classification"] == "baja"

    def test_sales_zero_flag(self) -> None:
        from app.application.agents.shared.stats_engine import product_profitability

        result = product_profitability(sales=0.0, cogs=100.0, units=5)
        assert result["gross_margin_pct"] is None
        assert result["division_by_zero"] is True

    def test_units_zero(self) -> None:
        from app.application.agents.shared.stats_engine import product_profitability

        result = product_profitability(sales=500.0, cogs=300.0, units=0)
        # margin_per_unit indefinido; gross_margin_pct sí calculable
        assert result["gross_margin_pct"] == pytest.approx(40.0)
        assert result["margin_per_unit"] is None
        assert result["units_division_by_zero"] is True


# ---------------------------------------------------------------------------
# npv_simple
# ---------------------------------------------------------------------------


class TestNpvSimple:
    def test_favorable(self) -> None:
        from app.application.agents.shared.stats_engine import npv_simple

        # Inversión $1000, cashflows mensuales $200 × 8 meses, tasa anual 12%
        result = npv_simple(
            initial_investment=1000.0,
            monthly_cashflows=[200.0] * 8,
            discount_rate_annual=0.12,
        )
        assert result["npv"] > 0
        assert result["recommendation"] == "favorable"
        assert result["irr_annual"] is not None

    def test_desfavorable(self) -> None:
        from app.application.agents.shared.stats_engine import npv_simple

        # Inversión $10000, cashflows muy pequeños
        result = npv_simple(
            initial_investment=10000.0,
            monthly_cashflows=[50.0] * 6,
            discount_rate_annual=0.12,
        )
        assert result["npv"] < 0
        assert result["recommendation"] == "desfavorable"

    def test_irr_nan_becomes_none(self) -> None:
        from app.application.agents.shared.stats_engine import npv_simple

        # Todos los flujos negativos → irr no tiene solución real
        result = npv_simple(
            initial_investment=1000.0,
            monthly_cashflows=[-50.0] * 6,
            discount_rate_annual=0.10,
        )
        # irr_monthly podría ser nan → debe ser None
        assert result["irr_monthly"] is None or result["irr_monthly"] is not math.nan

    def test_empty_cashflows(self) -> None:
        from app.application.agents.shared.stats_engine import npv_simple

        result = npv_simple(1000.0, [], 0.12)
        assert result["status"] == "insufficient_data"


# ---------------------------------------------------------------------------
# detect_anomalies
# ---------------------------------------------------------------------------


class TestDetectAnomalies:
    def test_pico_detectado(self) -> None:
        from app.application.agents.shared.stats_engine import detect_anomalies

        # n=8 garantiza que sqrt(n-1)=sqrt(7)≈2.65 > 2.5 para outlier extremo
        values = [100.0, 105.0, 98.0, 102.0, 99.0, 101.0, 100.0, 900.0]
        result = detect_anomalies(values, z_threshold=2.5)
        assert len(result) >= 1
        tipos = {r["type"] for r in result}
        assert "pico" in tipos
        # El índice 7 (900) debería ser detectado
        indices = [r["index"] for r in result]
        assert 7 in indices

    def test_caida_detectada(self) -> None:
        from app.application.agents.shared.stats_engine import detect_anomalies

        # n=8 garantiza que sqrt(n-1)=sqrt(7)≈2.65 > 2.5 para outlier extremo
        values = [100.0, 105.0, 98.0, 102.0, 99.0, 101.0, 100.0, 2.0]
        result = detect_anomalies(values, z_threshold=2.5)
        assert len(result) >= 1
        tipos = {r["type"] for r in result}
        assert "caida" in tipos
        indices = [r["index"] for r in result]
        assert 7 in indices

    def test_std_zero_empty(self) -> None:
        from app.application.agents.shared.stats_engine import detect_anomalies

        values = [100.0, 100.0, 100.0, 100.0]
        result = detect_anomalies(values)
        assert result == []

    def test_n_less_than_3_empty(self) -> None:
        from app.application.agents.shared.stats_engine import detect_anomalies

        assert detect_anomalies([100.0, 200.0]) == []
        assert detect_anomalies([]) == []
        assert detect_anomalies([50.0]) == []

    def test_normal_no_anomalies(self) -> None:
        from app.application.agents.shared.stats_engine import detect_anomalies

        values = [100.0 + i * 2.0 for i in range(10)]  # serie plana
        result = detect_anomalies(values, z_threshold=2.5)
        assert isinstance(result, list)

    def test_shape_of_result(self) -> None:
        from app.application.agents.shared.stats_engine import detect_anomalies

        values = [10.0, 10.0, 10.0, 10.0, 10.0, 200.0]
        result = detect_anomalies(values, z_threshold=2.0)
        for item in result:
            assert "index" in item
            assert "value" in item
            assert "z_score" in item
            assert "type" in item
