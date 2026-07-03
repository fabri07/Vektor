"""Pydantic schemas for the SUPERADMIN metrics and analytics endpoints."""

import uuid
from datetime import datetime
from typing import Any

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


# ── Data repair ────────────────────────────────────────────────────────────────


class RepairRequest(BaseModel):
    tenant_id: uuid.UUID | None = None
    source_run_id: uuid.UUID | None = None


class DataRepairItemResponse(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    tenant_id: uuid.UUID
    source_file_id: uuid.UUID | None
    sale_entry_id: uuid.UUID | None
    product_id: uuid.UUID | None
    action: str
    before_json: dict[str, Any] | None
    after_json: dict[str, Any] | None
    confidence: str
    created_at: datetime

    model_config = {"from_attributes": True}


class DataRepairRunResponse(BaseModel):
    id: uuid.UUID
    tenant_id: uuid.UUID | None
    repair_type: str
    status: str
    dry_run: bool
    source_run_id: uuid.UUID | None
    candidates_found: int
    sales_detected: int
    sales_voided: int
    products_detected: int
    products_created: int
    products_updated: int
    products_skipped: int
    details_json: dict[str, Any] | None
    created_at: datetime
    completed_at: datetime | None

    model_config = {"from_attributes": True}


class InventoryIntegrityDivergence(BaseModel):
    product_id: uuid.UUID
    product_name: str
    stock_units: int
    stock_esperado: int
    diff: int
    anchor_qty: int
    purchase_qty: int
    sales_qty: int


class InventoryIntegrityThreshold(BaseModel):
    relative_pct: float
    absolute_floor_units: int


class InventoryIntegrityCheckResponse(BaseModel):
    tenant_id: uuid.UUID
    checked: int
    divergences: list[InventoryIntegrityDivergence]
    skipped_no_anchor: int
    skipped_complex_ledger: int
    threshold: InventoryIntegrityThreshold
