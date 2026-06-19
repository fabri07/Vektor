"""Pydantic schemas for the ingestion pipeline endpoints."""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class UploadResponse(BaseModel):
    file_id: UUID
    status: str  # always "PROCESSING" immediately after upload
    # Dedup de re-upload: id de un archivo YA importado con el mismo contenido (si existe).
    duplicate_of: UUID | None = None
    warning: str | None = None


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
    # Contexto al que pertenece la sugerencia (hoja/tabla). None = archivo de un solo contexto.
    context_id: str | None = None


class ColumnMapping(BaseModel):
    source_column: str
    target_field: str  # campo canónico, "ignore", o "custom_field:{key}"
    # Mapeo cualificado por contexto (multi-hoja / multi-grupo). None = mapeo plano legacy.
    context_id: str | None = None
    entity_type: str | None = None  # entity_type del contexto (sale|expense|product)


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
            "Si se omite, el sistema usa heurísticas automáticas. En archivos multi-contexto "
            "(multi-hoja), cada ColumnMapping lleva su context_id + entity_type."
        ),
    )
    context_confirmed: dict[str, bool] = Field(
        default_factory=dict,
        description=(
            "Inclusión por contexto (sheet/grupo) en archivos multi-contexto: "
            "{context_id: incluir}. Vacío = se usa confirmed_fields por tipo (legacy)."
        ),
    )
    context_entity: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Override de entity_type por contexto en documentos de texto/imagen: "
            "{context_id: sale|expense}. Permite reasignar un grupo detectado."
        ),
    )


class ConfirmIngestionResponse(BaseModel):
    file_id: UUID
    status: str
    message: str


# ── Relectura de archivos (REREAD_FILE) ────────────────────────────────────────


class RereadCounts(BaseModel):
    to_update: int
    preserved: int
    new: int
    to_void: int
    # Filas ya importadas (huella presente) que el reimport saltea — ni nuevas ni
    # duplicadas. Default 0 por compatibilidad.
    unchanged: int = 0
    # Impacto estimado en el catálogo de productos.
    products_new: int = 0
    products_restock: int = 0


class RereadPreviewResponse(BaseModel):
    file_id: UUID
    counts: RereadCounts
    legacy_fallback: bool = False
    sample_changes: list[dict[str, Any]] = Field(default_factory=list)


class RereadItem(BaseModel):
    action: str
    before: dict[str, Any] | None = None
    after: dict[str, Any] | None = None


class RereadApplyResponse(BaseModel):
    file_id: UUID
    run_id: UUID
    to_update: int
    preserved: int
    new: int
    voided: int
    inserted: int
    legacy_fallback: bool = False
    items: list[RereadItem] = Field(default_factory=list)


class RereadUndoResponse(BaseModel):
    run_id: UUID
    restored: int
    removed: int
    status: str
