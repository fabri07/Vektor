"""Pydantic schemas for the SUPERADMIN metrics and analytics endpoints."""

from pydantic import BaseModel


class JobStats(BaseModel):
    success: int
    failed: int


class AdminMetricsResponse(BaseModel):
    total_tenants: int
    total_onboarding_completed: int
    avg_data_completeness_score: float | None
    avg_health_score: float | None
    jobs_last_24h: JobStats
    tenants_by_vertical: dict[str, int]


# ── Analytics / benchmarks ────────────────────────────────────────────────────

class BenchmarkThresholds(BaseModel):
    critical_below: float
    warning_below: float
    healthy_min: float
    healthy_max: float
    source: str  # "data_driven" | "static"


class VerticalBenchmarkItem(BaseModel):
    vertical_code: str
    sample_count: int
    avg_score: float | None
    avg_margin_ratio: float | None
    p50_margin_ratio: float | None
    avg_data_completeness: float | None
    benchmark_source: str
    benchmark: BenchmarkThresholds


class AnalyticsBenchmarksResponse(BaseModel):
    verticals: list[VerticalBenchmarkItem]
