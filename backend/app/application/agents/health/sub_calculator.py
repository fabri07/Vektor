"""AgentHealth — sub_calculator.

Ejecuta la fórmula v2 sobre un BusinessState.
Delega en calculate_health_score (misma función que health_score_service) para evitar
duplicar lógica de cálculo entre el pipeline Celery y el agente.

Fórmula v2: cash×0.30 + stock×0.20 + supplier×0.10 + margin×0.20 + growth×0.20
"""

from __future__ import annotations

from pydantic import BaseModel

from app.heuristics.health_engine import HealthScoreResult, calculate_health_score
from app.heuristics.verticals import MarginBenchmark
from app.heuristics.verticals.loader import load_vertical_heuristics
from app.state.business_state_service import BusinessState


class ComponentScoresV2(BaseModel):
    """Score de cada dimensión (0–100, entero)."""

    cash_score: int
    stock_score: int
    supplier_score: int
    margin_score: int
    growth_score: int
    total_score: int
    primary_risk_code: str
    confidence_level: str
    data_completeness_score: float
    cash_source: str = "desconocido"


def compute_scores(
    state: BusinessState,
    benchmark: MarginBenchmark | None = None,
) -> ComponentScoresV2:
    """Calcula los 5 subscores y el total v2 a partir del BusinessState."""
    config = load_vertical_heuristics(state.vertical_code)
    result: HealthScoreResult = calculate_health_score(state, benchmark=benchmark, config=config)
    return ComponentScoresV2(
        cash_score=result.score_cash,
        stock_score=result.score_stock,
        supplier_score=result.score_supplier,
        margin_score=result.score_margin,
        growth_score=result.score_growth,
        total_score=result.score_total,
        primary_risk_code=result.primary_risk_code,
        confidence_level=result.confidence_level,
        data_completeness_score=result.data_completeness_score,
        cash_source=result.cash_source,
    )
