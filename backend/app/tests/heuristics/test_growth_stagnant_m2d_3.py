"""M·2d-3: GROWTH_STAGNANT ya no rompe el insight ni inventa una caída.

Antes: `render_insight` no tenía template → KeyError en `generate_insight`; el
chat lo salteaba y el banner no lo pintaba. Y sin período anterior el
crecimiento vale 50 neutro, así que salía como riesgo principal («las ventas
muestran caída») sin nada con qué comparar.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from app.application.agents.shared.alert_explainer import (
    _ALERT_MEANING,
    _RISK_CODE_TO_METRICS,
)
from app.heuristics.health_engine import _DIMENSION_RISK_CODE, calculate_health_score
from app.heuristics.insight_templates import TEMPLATES, render_insight
from app.tests.heuristics.test_health_engine_regression import _product, _state


def test_cada_codigo_de_riesgo_tiene_template_significado_y_metricas() -> None:
    for code in _DIMENSION_RISK_CODE.values():
        assert code in TEMPLATES, code
        assert code in _ALERT_MEANING, code
        assert code in _RISK_CODE_TO_METRICS, code


def _todo_sano(**over: object):
    base: dict[str, object] = {
        "cash_on_hand_est": Decimal("999999"),
        "monthly_fixed_expenses_est": Decimal("100"),
        "supplier_count": 10,
        "products": [_product(100, 5) for _ in range(5)],
    }
    base.update(over)
    return _state(**base)  # type: ignore[arg-type]


def test_sin_periodo_anterior_crecimiento_no_es_el_riesgo() -> None:
    state = _todo_sano(prev_monthly_sales_est=Decimal("0"))
    result = calculate_health_score(state)
    assert result.score_growth == 50
    assert result.primary_risk_code != "GROWTH_STAGNANT"


def test_sin_periodo_anterior_el_total_no_cambia() -> None:
    """La exclusión es sólo del riesgo: el 50 neutro sigue pesando en el total."""
    r = calculate_health_score(_todo_sano(prev_monthly_sales_est=Decimal("0")))
    assert r.score_growth == 50
    pesos = {"cash": 0.30, "stock": 0.20, "supplier": 0.10, "margin": 0.20, "growth": 0.20}
    scores = {
        "cash": r.score_cash, "stock": r.score_stock, "supplier": r.score_supplier,
        "margin": r.score_margin, "growth": r.score_growth,
    }
    if r.cash_source == "desconocido":  # caja excluida del total (no-invention)
        scores["cash"] = None
    presentes = {k: v for k, v in scores.items() if v is not None}
    esperado = round(
        sum(v * pesos[k] for k, v in presentes.items()) / sum(pesos[k] for k in presentes)
    )
    assert r.score_total == esperado


def test_con_caida_real_sigue_siendo_el_riesgo_y_el_insight_dice_el_porcentaje() -> None:
    state = _todo_sano(
        monthly_sales_est=Decimal("50000"), prev_monthly_sales_est=Decimal("200000")
    )
    result = calculate_health_score(state)
    assert result.primary_risk_code == "GROWTH_STAGNANT"

    title, description, action = render_insight("GROWTH_STAGNANT", state, result)
    assert title == "Tus ventas cayeron 75,0% contra los 30 días anteriores"
    assert "$50.000" in description and "$200.000" in description
    assert "-75,0%" in description and "estimamos" in description
    assert action


def test_estancadas_no_dice_caida() -> None:
    state = SimpleNamespace(
        monthly_sales_est=Decimal("101000"), prev_monthly_sales_est=Decimal("100000")
    )
    title, _, _ = render_insight("GROWTH_STAGNANT", state, None)
    assert title == "Tus ventas están estancadas contra los 30 días anteriores"


def test_crecimiento_no_dice_caida() -> None:
    state = SimpleNamespace(
        monthly_sales_est=Decimal("120000"), prev_monthly_sales_est=Decimal("100000")
    )
    title, _, _ = render_insight("GROWTH_STAGNANT", state, None)
    assert "cayeron" not in title and "crecieron 20,0%" in title


def test_sin_periodo_anterior_el_insight_no_inventa_variacion() -> None:
    state = SimpleNamespace(monthly_sales_est=Decimal("80000"), prev_monthly_sales_est=Decimal("0"))
    title, description, _ = render_insight("GROWTH_STAGNANT", state, None)
    assert "%" not in title and "%" not in description
    assert "Todavía no hay 30 días anteriores" in description
