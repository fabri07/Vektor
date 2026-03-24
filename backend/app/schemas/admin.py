"""Pydantic schemas for the SUPERADMIN metrics endpoint."""

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
