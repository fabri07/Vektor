"""AgentHealth — scorer determinístico.

REGLA CRÍTICA: El score se calcula en Python, NUNCA con LLM.
El LLM solo genera narrativa a partir de los números ya calculados.

FÓRMULA CANÓNICA (no modificar pesos):
  health_score = cash×0.35 + stock×0.30 + supplier×0.15 + discipline×0.20
"""

from pydantic import BaseModel

from app.application.agents.shared.heuristic_engine import HeuristicConfig


class ComponentScores(BaseModel):
    cash_score: float
    stock_score: float
    supplier_score: float
    discipline_score: float


class HealthScore(BaseModel):
    business_id: str
    health_score: float
    components: ComponentScores
    alerts: list
    period: str


def compute_cash_score(coverage_days: float, config: HeuristicConfig) -> float:
    """Determinístico. Sin LLM."""
    h = config.cash_health
    if coverage_days >= h.healthy_days_min * 2:
        return 100.0
    elif coverage_days >= h.healthy_days_min:
        ratio = (coverage_days - h.healthy_days_min) / h.healthy_days_min
        return 70.0 + (ratio * 29.0)
    elif coverage_days >= h.warning_days_min:
        ratio = (coverage_days - h.warning_days_min) / (h.healthy_days_min - h.warning_days_min)
        return 30.0 + (ratio * 39.0)
    else:
        ratio = max(0.0, coverage_days / h.warning_days_min) if h.warning_days_min > 0 else 0.0
        return ratio * 29.0


def compute_stock_score(stockout_count: int, slow_moving_count: int, total_products: int) -> float:
    """Determinístico. Sin LLM."""
    if total_products == 0:
        return 50.0  # Sin datos, score neutro
    score = 100.0
    score -= stockout_count * 10
    score -= slow_moving_count * 5
    return max(0.0, min(100.0, score))


def compute_supplier_score(active_suppliers: int, overdue_orders: int) -> float:
    """Determinístico. Sin LLM."""
    if active_suppliers == 0:
        return 50.0
    score = 100.0
    score -= overdue_orders * 15
    return max(0.0, min(100.0, score))


def compute_discipline_score(days_with_data: int, total_days: int) -> float:
    """Determinístico. Sin LLM."""
    if total_days == 0:
        return 0.0
    return min(100.0, (days_with_data / total_days) * 100)


def compute_health_score(components: ComponentScores) -> float:
    """
    FÓRMULA CANÓNICA — no modificar los pesos.
    health_score = cash×0.35 + stock×0.30 + supplier×0.15 + discipline×0.20
    """
    return (
        components.cash_score * 0.35
        + components.stock_score * 0.30
        + components.supplier_score * 0.15
        + components.discipline_score * 0.20
    )
