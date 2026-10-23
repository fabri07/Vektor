"""Pydantic schemas for health score endpoints."""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel


class DimensionScoreResponse(BaseModel):
    dimension: str
    #: ``None`` = dimensión ausente (M·2b-3: margen sin costo suficiente).
    value: float | None
    weight: float
    weighted_value: float
    explanation: str


class HealthScoreResponse(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    tenant_id: UUID
    total_score: float
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
    avg_score: float
    min_score: float
    max_score: float
    level: str


class ScoreSummaryResponse(BaseModel):
    """Lightweight summary for dashboard cards."""

    current_score: float
    level: str
    previous_score: float | None
    delta: float | None
    snapshot_date: datetime
    needs_attention: bool


class HealthScoreV2Response(BaseModel):
    """Full health score response with explicit subscores (schema F1-01)."""

    model_config = {"from_attributes": True}

    id: UUID
    tenant_id: UUID
    score_total: int
    score_cash: int
    #: ``None`` = margen ausente (M·2b-3): sin costo suficiente no hay subscore.
    score_margin: int | None
    score_stock: int
    score_supplier: int
    score_growth: int | None = None  # None → snapshot v1 (pre-Stage-5a)
    primary_risk_code: str
    confidence_level: str
    data_completeness_score: float
    level: str
    created_at: datetime
    is_demo_data: bool = False
    # Fuente de la caja: "arqueo" | "onboarding" | "flujo" | "desconocido".
    # "flujo" → el frontend muestra el recordatorio de arqueo. None en snapshots viejos.
    cash_source: str | None = None
    #: Estado del margen: confirmado | estimado | ausente. ``None`` en snapshots
    #: anteriores a M·2b-3.
    margin_status: str | None = None
    #: Motivo de un margen ausente o estimado («cobertura de costo 9,0%»).
    margin_reason: str | None = None
    cost_coverage_total_pct: float | None = None


class CalculatingResponse(BaseModel):
    """Returned when no health score has been computed yet."""

    status: str = "CALCULATING"


class NoDataResponse(BaseModel):
    """Returned when the tenant has no real data uploaded yet."""

    status: str = "NO_DATA"
    score: None = None
    score_level: str = "NO_DATA"
    is_demo_data: bool = False
    mensaje: str = "Cargá tus datos para ver tu análisis"
