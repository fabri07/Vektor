"""Pydantic schemas for the ingestion pipeline endpoints."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class UploadResponse(BaseModel):
    file_id: UUID
    status: str  # always "PROCESSING" immediately after upload


class FileStatusItem(BaseModel):
    model_config = {"from_attributes": True}

    id: UUID
    original_filename: str
    content_type: str
    size_bytes: int
    purpose: str
    processing_status: str
    created_at: datetime


class ColumnAtRisk(BaseModel):
    column: str
    null_pct: float
    recommendation: str = "drop"


class FilePreviewResponse(BaseModel):
    model_config = {"from_attributes": True}

    file_id: UUID
    processing_status: str
    parsed_summary_json: dict[str, Any] | None
    columns_at_risk: list[ColumnAtRisk] = []


class DropColumnsRequest(BaseModel):
    columns: list[str] = Field(description="Columnas a eliminar antes de confirmar la importación.")


# ── Column mapping schemas ────────────────────────────────────────────────────


class ColumnMappingSuggestion(BaseModel):
    source_column: str
    normalized_column: str
    sample_values: list[str]
    target_field: str | None
    confidence: float
    source: Literal["tenant_history", "heuristic", "fuzzy", "none"]
    status: Literal["mapped", "unmapped", "required_missing"]


class ColumnMapping(BaseModel):
    source_column: str
    target_field: str  # campo canónico, "ignore", o "custom_field:{key}"


class TenantColumnMappingResponse(BaseModel):
    id: UUID
    entity_type: str
    source_column: str
    target_field: str
    confirmed_count: int
    last_seen_at: datetime


# ── Ingestion confirm ─────────────────────────────────────────────────────────


class ConfirmIngestionRequest(BaseModel):
    confirmed_fields: dict[str, Any] = Field(
        description=(
            "Which data categories to import from the parsed file. "
            "Keys: 'ventas', 'gastos', 'productos'. Values: bool."
        )
    )
    column_mappings: list[ColumnMapping] = Field(
        default_factory=list,
        description=(
            "Mapeo explícito de columnas del archivo a campos canónicos del dominio. "
            "Si se omite, el sistema usa heurísticas automáticas."
        ),
    )


class ConfirmIngestionResponse(BaseModel):
    file_id: UUID
    status: str
    message: str
