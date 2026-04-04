"""Tests del scorer determinístico de AgentHealth.

Fórmula canónica (no modificar pesos):
  health_score = cash×0.35 + stock×0.30 + supplier×0.15 + discipline×0.20
"""

from __future__ import annotations

from app.application.agents.health.scorer import ComponentScores, compute_health_score


def test_health_score_deterministic() -> None:
    """El mismo input siempre produce el mismo score (1000 iteraciones)."""
    components = ComponentScores(
        cash_score=75.0,
        stock_score=80.0,
        supplier_score=90.0,
        discipline_score=60.0,
    )
    scores = [compute_health_score(components) for _ in range(1000)]
    assert len(set(scores)) == 1, "El score no es determinístico"


def test_health_score_formula_cash_only() -> None:
    """Solo cash=100, resto=0 → 100×0.35 = 35.0"""
    c = ComponentScores(cash_score=100, stock_score=0, supplier_score=0, discipline_score=0)
    assert compute_health_score(c) == 35.0


def test_health_score_formula_stock_only() -> None:
    """Solo stock=100, resto=0 → 100×0.30 = 30.0"""
    c = ComponentScores(cash_score=0, stock_score=100, supplier_score=0, discipline_score=0)
    assert compute_health_score(c) == 30.0


def test_health_score_formula_supplier_only() -> None:
    """Solo supplier=100, resto=0 → 100×0.15 = 15.0"""
    c = ComponentScores(cash_score=0, stock_score=0, supplier_score=100, discipline_score=0)
    assert compute_health_score(c) == 15.0


def test_health_score_formula_discipline_only() -> None:
    """Solo discipline=100, resto=0 → 100×0.20 = 20.0"""
    c = ComponentScores(cash_score=0, stock_score=0, supplier_score=0, discipline_score=100)
    assert compute_health_score(c) == 20.0


def test_health_score_formula_all_100() -> None:
    """Todos 100 → score debe ser exactamente 100.0"""
    c = ComponentScores(
        cash_score=100, stock_score=100, supplier_score=100, discipline_score=100
    )
    assert compute_health_score(c) == 100.0


def test_health_score_formula_all_zero() -> None:
    """Todos 0 → score debe ser 0.0"""
    c = ComponentScores(cash_score=0, stock_score=0, supplier_score=0, discipline_score=0)
    assert compute_health_score(c) == 0.0


def test_health_score_weights_sum_to_one() -> None:
    """Verificar que la suma de pesos es 1.0 (100%)."""
    weights_sum = 0.35 + 0.30 + 0.15 + 0.20
    assert abs(weights_sum - 1.0) < 1e-9, f"Pesos no suman 1.0: {weights_sum}"
