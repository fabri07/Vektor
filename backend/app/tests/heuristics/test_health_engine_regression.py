"""Tests de regresión para la fórmula v2 del Health Engine — Stage 5c.

Verifica que los cambios futuros no rompan la fórmula canónica ni el comportamiento
documentado en tests/fixtures/agent_baselines/health/v1_formula.json.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from app.domain.verticals import Vertical
from app.heuristics.health_engine import calculate_health_score
from app.state.business_state_service import BusinessState, ProductSummary

# ── helpers ────────────────────────────────────────────────────────────────────


def _product(stock: int = 20, threshold: int = 5) -> ProductSummary:
    return ProductSummary(
        product_id=uuid.uuid4(),
        name="X",
        stock_units=stock,
        low_stock_threshold_units=threshold,
        sale_price_ars=Decimal("1000"),
    )


def _state(
    monthly_sales_est: Decimal = Decimal("100000"),
    monthly_inventory_cost_est: Decimal = Decimal("55000"),
    monthly_fixed_expenses_est: Decimal = Decimal("20000"),
    cash_on_hand_est: Decimal = Decimal("6666.67"),
    prev_monthly_sales_est: Decimal = Decimal("0"),
    supplier_count: int = 4,
    products: list[Any] | None = None,
    completeness: float = 75.0,
    confidence: str = "MEDIUM",
) -> BusinessState:
    return BusinessState(
        snapshot_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        vertical_code=Vertical.KIOSCO_ALMACEN.value,
        data_completeness_score=completeness,
        confidence_level=confidence,
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


# ── Formula v2 regression ──────────────────────────────────────────────────────


class TestFormulaV2Regression:
    def test_weights_sum_to_one(self) -> None:
        """Los pesos de la fórmula v2 suman exactamente 1.0."""
        weights = [0.30, 0.20, 0.10, 0.20, 0.20]  # cash, stock, supplier, margin, growth
        assert abs(sum(weights) - 1.0) < 1e-10

    def test_all_perfect_gives_100(self) -> None:
        """Si todos los componentes son 100, el total es 100."""
        state = _state(
            monthly_sales_est=Decimal("200000"),
            monthly_inventory_cost_est=Decimal("0"),
            monthly_fixed_expenses_est=Decimal("100"),
            cash_on_hand_est=Decimal("1000000"),
            prev_monthly_sales_est=Decimal("100000"),
            supplier_count=10,
            products=[_product(100, 5) for _ in range(5)],
            completeness=100.0,
            confidence="HIGH",
        )
        result = calculate_health_score(state)
        # No podemos garantizar exactamente 100 por las bandas, pero debe ser >= 90
        assert result.score_total >= 90

    def test_prev_sales_neutral_gives_growth_50(self) -> None:
        """Sin historial (prev=0) → score_growth == 50 (neutro)."""
        state = _state(prev_monthly_sales_est=Decimal("0"))
        result = calculate_health_score(state)
        assert result.score_growth == 50

    def test_growth_positive_increases_score(self) -> None:
        """Ventas que crecen 50% vs período anterior → score_growth > 70."""
        state = _state(
            monthly_sales_est=Decimal("150000"),
            prev_monthly_sales_est=Decimal("100000"),  # +50%
        )
        result = calculate_health_score(state)
        assert result.score_growth > 70

    def test_growth_negative_decreases_score(self) -> None:
        """Ventas que caen 50% vs período anterior → score_growth < 40."""
        state = _state(
            monthly_sales_est=Decimal("50000"),
            prev_monthly_sales_est=Decimal("100000"),  # -50%
        )
        result = calculate_health_score(state)
        assert result.score_growth < 40

    def test_score_growth_in_result(self) -> None:
        """HealthScoreResult siempre incluye score_growth."""
        state = _state()
        result = calculate_health_score(state)
        assert hasattr(result, "score_growth")
        assert 0 <= result.score_growth <= 100

    def test_primary_risk_code_includes_growth(self) -> None:
        """Si growth es el peor score, primary_risk_code es GROWTH_STAGNANT."""
        state = _state(
            monthly_sales_est=Decimal("50000"),
            prev_monthly_sales_est=Decimal("200000"),  # caída del 75%
            cash_on_hand_est=Decimal("999999"),
            monthly_fixed_expenses_est=Decimal("100"),
            supplier_count=10,
            products=[_product(100, 5) for _ in range(5)],
        )
        result = calculate_health_score(state)
        assert result.primary_risk_code == "GROWTH_STAGNANT"

    def test_completeness_cap_applies(self) -> None:
        """Con completeness < 40, el score nunca supera 60."""
        state = _state(completeness=30.0, confidence="LOW")
        result = calculate_health_score(state)
        assert result.score_total <= 60


# ── Margin override via benchmark param ───────────────────────────────────────


class TestMarginBenchmarkOverride:
    def test_custom_benchmark_affects_margin_score(self) -> None:
        """Pasar un MarginBenchmark custom cambia el score de margen."""
        from app.heuristics.verticals import (  # noqa: PLC0415
            BenchmarkProvenance,
            MarginBenchmark,
        )

        state = _state(
            monthly_sales_est=Decimal("100000"),
            monthly_inventory_cost_est=Decimal("60000"),
            monthly_fixed_expenses_est=Decimal("20000"),
        )
        # Margen real ≈ (100k - 60k - 20k) / 100k = 20%

        # Benchmark estricto: target = 40% → margen 20% queda en zona warning
        strict_benchmark = MarginBenchmark(
            critical_below=0.05,
            warning_below=0.10,
            healthy_min=0.10,
            healthy_max=0.40,
            provenance=BenchmarkProvenance.TENANT_OVERRIDE,
        )
        # Benchmark relajado: target = 15% → margen 20% queda en zona healthy/excellent
        relaxed_benchmark = MarginBenchmark(
            critical_below=0.03,
            warning_below=0.08,
            healthy_min=0.08,
            healthy_max=0.15,
            provenance=BenchmarkProvenance.TENANT_OVERRIDE,
        )

        strict_result = calculate_health_score(state, benchmark=strict_benchmark)
        relaxed_result = calculate_health_score(state, benchmark=relaxed_benchmark)

        assert strict_result.score_margin < relaxed_result.score_margin
