"""Pydantic schemas for health score endpoints."""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel


class DimensionScoreResponse(BaseModel):
    dimension: str
    value: Decimal
    weight: Decimal
    weighted_value: Decimal
    explanation: str


class HealthScoreResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    tenant_id: UUID
    total_score: Decimal
    level: str
    dimensions: list[DimensionScoreResponse]
    triggered_by: str
    snapshot_date: datetime
    created_at: datetime


class WeeklyScoreHistoryResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    week_start: date
    week_end: date
    avg_score: Decimal
    min_score: Decimal
    max_score: Decimal
    level: str


class ScoreSummaryResponse(BaseModel):
    """Lightweight summary for dashboard cards."""

    current_score: Decimal
    level: str
    previous_score: Decimal | None
    delta: Decimal | None
    snapshot_date: datetime
    needs_attention: bool


class HealthScoreV2Response(BaseModel):
    """Full health score response with explicit subscores (schema F1-01)."""

    model_config = {"from_attributes": True}

    id: UUID
    tenant_id: UUID
    score_total: int
    score_cash: int
    score_margin: int
    score_stock: int
    score_supplier: int
    primary_risk_code: str
    confidence_level: str
    data_completeness_score: float
    level: str
    created_at: datetime


class CalculatingResponse(BaseModel):
    """Returned when no health score has been computed yet."""

    status: str = "CALCULATING"
