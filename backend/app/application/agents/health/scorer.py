"""AgentHealth — scorer.py (compat shim, Stage 5a).

DEPRECADO — no usar para código nuevo.
Usar sub_calculator.py + sub_collector.py en su lugar.

Este módulo queda activo durante un sprint para no romper tests existentes que
importan ComponentScores / compute_health_score directamente.
Será eliminado en Stage 5d (junto con cleanup de otros aliases).

Fórmula documentada (v1, pre-Stage-5a):
  health_score = cash×0.35 + stock×0.30 + supplier×0.15 + discipline×0.20

NOTA: 'discipline_score' era lo que agent.py leía del campo score_margin de la DB.
El campo score_margin SIEMPRE almacenó el score de margen (health_engine.py).
El mislabeling era un bug en agent.py corregido en Stage 5a.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.application.agents.shared.heuristic_engine import HeuristicConfig


class ComponentScores(BaseModel):
    """Modelo v1. Usar ComponentScoresV2 de sub_calculator.py para código nuevo."""

    cash_score: float
    stock_score: float
    supplier_score: float
    discipline_score: float  # era score_margin en DB; ver nota en docstring


class HealthScore(BaseModel):
    """Modelo v1."""

    business_id: str
    health_score: float
    components: ComponentScores
    alerts: list[Any]
    period: str
    confidence_level: str = "LOW"
    data_completeness_score: float = 0.0


# ── Funciones v1 (preservadas para tests existentes) ──────────────────────────


def compute_cash_score(coverage_days: float, config: HeuristicConfig) -> float:
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
    if total_products == 0:
        return 50.0
    score = 100.0
    score -= stockout_count * 10
    score -= slow_moving_count * 5
    return max(0.0, min(100.0, score))


def compute_supplier_score(active_suppliers: int, overdue_orders: int) -> float:
    if active_suppliers == 0:
        return 50.0
    score = 100.0
    score -= overdue_orders * 15
    return max(0.0, min(100.0, score))


def compute_discipline_score(days_with_data: int, total_days: int) -> float:
    if total_days == 0:
        return 0.0
    return min(100.0, (days_with_data / total_days) * 100)


def compute_health_score(components: ComponentScores) -> float:
    """Fórmula v1: cash×0.35 + stock×0.30 + supplier×0.15 + discipline×0.20"""
    return (
        components.cash_score * 0.35
        + components.stock_score * 0.30
        + components.supplier_score * 0.15
        + components.discipline_score * 0.20
    )
