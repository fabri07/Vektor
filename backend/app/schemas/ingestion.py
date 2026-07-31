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


class ContextualColumnRisk(BaseModel):
    """F8a: riesgo de una columna mapeada, POR CONTEXTO y según el mapeo efectivo.

    Reemplaza al diagnóstico global ``ColumnAtRisk`` (basado solo en el nombre de
    columna). Solo son accionables los targets requeridos y los opcionales que el
    usuario seleccionó explícitamente (``user_selected=True``). ``affected_rows`` es
    exacto (vacíos + inválidos según el parser canónico del target).
    """

    context_id: str
    entity_type: str
    source_column: str
    target_field: str
    null_ratio: float  # 0.0–1.0; la UI lo muestra ×100
    affected_rows: int  # vacíos + inválidos, exacto
    null_rows: int
    invalid_rows: int
    field_requirement: Literal["required", "explicitly_selected", "optional"]
    mapping_source: Literal["tenant_history", "heuristic", "fuzzy", "llm", "none"]
    user_selected: bool
    allowed_actions: list[str] = Field(default_factory=list)
    recommendation: str


class MasterPreviewSample(BaseModel):
    """Fila de muestra del preview de un maestro (cliente/proveedor). PII
    minimizada a propósito: solo nombre + estado + primer diagnóstico — nunca
    DNI/CUIT/email/teléfono crudos (esos solo viven en memoria durante el
    request, no se serializan)."""

    row_index: int
    status: str  # "create" | "update" | "invalid" | "duplicate_in_file" | "needs_review"
    display_name: str | None = None
    existing_name: str | None = None
    issue: str | None = None


class MasterPreviewSummary(BaseModel):
    """Preview de una hoja de maestro (F7d) — cuántas filas son create/update/
    needs_review/invalid/duplicate ANTES de confirmar. No persiste nada."""

    context_id: str | None = None
    entity_type: str  # "customer" | "supplier"
    to_create: int
    to_update: int
    needs_review: int
    invalid: int
    duplicates: int
    samples: list[MasterPreviewSample] = Field(default_factory=list)


class FileDeletionPreviewResponse(BaseModel):
    """Qué datos se lleva puestos el borrado de un archivo.

    Alimenta la advertencia que el usuario acepta o cancela. El borrado revierte
    también lo editado a mano, así que ``has_user_edits`` no bloquea: informa.
    """

    file_id: UUID
    ventas: int
    gastos: int
    productos: int
    movimientos_stock: int
    otros: int
    # Filas de "Otros" que el usuario YA clasificó: NO se borran. El registro que
    # generaron (venta/gasto/producto) no lleva `source_upload_id`, así que la
    # reversa no lo alcanza y borrar la fila destruiría su único rastro.
    otros_ya_clasificados: int
    # Hay registros de este archivo que alguien editó a mano después de importar.
    has_user_edits: bool
    # El archivo se importó antes del ledger de reversa: no se puede saber qué
    # productos creó, así que quedan vivos y hay que revisarlos a mano.
    productos_no_rastreables: bool


class FilePreviewResponse(BaseModel):
    model_config = {"from_attributes": True}

    file_id: UUID
    processing_status: str
    parsed_summary_json: dict[str, Any] | None
    # Deprecado (diagnóstico global por nombre de columna); ver contextual_column_risk.
    columns_at_risk: list[ColumnAtRisk] = []
    # F8a: riesgo contextual por columna mapeada (informativo en el preview, desde
    # las sugerencias de mapeo). Vacío si el archivo no tiene contextos transaccionales.
    contextual_column_risk: list[ContextualColumnRisk] = Field(default_factory=list)
    # F7d: preview universal de maestros (clientes/proveedores) — vacío si el
    # archivo no tiene hojas de maestro o si no se pudo estimar el mapeo.
    master_previews: list[MasterPreviewSummary] = Field(default_factory=list)


# ── Column mapping schemas ────────────────────────────────────────────────────


class ColumnMappingSuggestion(BaseModel):
    source_column: str
    normalized_column: str
    sample_values: list[str]
    target_field: str | None
    confidence: float
    source: Literal["tenant_history", "heuristic", "fuzzy", "llm", "none"]
    status: Literal["mapped", "unmapped", "required_missing"]
    # Contexto al que pertenece la sugerencia (hoja/tabla). None = archivo de un solo contexto.
    context_id: str | None = None


class FieldCatalogEntry(BaseModel):
    """Un campo canónico al que se puede mapear una columna."""

    value: str
    label: str
    # True = solo UNA columna puede apuntarle. Dos columnas a un campo escalar no
    # se pueden desempatar sin inventar, así que el confirm las rechaza y la UI
    # bloquea. Ver SINGLE_VALUE_FIELDS en column_mapping_service.
    single_value: bool = False


class EntityFieldCatalog(BaseModel):
    """Campos disponibles y requeridos para una entidad."""

    # Un requerido se cubre SOLO con un campo canónico: un `custom_field:` guarda
    # el dato pero no satisface el requerido (misma regla que `_missing_required`).
    required: list[str]
    fields: list[FieldCatalogEntry]


class ColumnMapping(BaseModel):
    source_column: str
    target_field: str  # campo canónico, "ignore", o "custom_field:{key}"
    # Mapeo cualificado por contexto (multi-hoja / multi-grupo). None = mapeo plano legacy.
    context_id: str | None = None
    # entity_type del contexto (sale|expense|product|customer|supplier)
    entity_type: str | None = None
    # F8a: el usuario tocó/confirmó/creó este mapping (vs. aceptar pasivamente una
    # sugerencia). Solo True vuelve accionable un target OPCIONAL en el protocolo de
    # riesgo. El backend nunca lo infiere de la mera presencia del mapping.
    user_selected: bool = False


class ColumnRiskRequest(BaseModel):
    """Body del endpoint read-only ``POST /files/{id}/column-risk``: el mapeo
    provisional (draft) que el usuario armó, para recalcular el riesgo con el mapeo
    efectivo (incluye ``user_selected`` por columna). No persiste nada.

    ``confirmed_fields``/``context_confirmed`` espejan el body del confirm: los
    contextos que el usuario decidió NO importar no generan riesgo accionable
    (misma decisión de inclusión que ``POST /confirm``)."""

    column_mappings: list[ColumnMapping] = Field(default_factory=list)
    context_entity: dict[str, Literal["sale", "expense", "product", "customer", "supplier"]] = (
        Field(default_factory=dict)
    )
    confirmed_fields: dict[str, bool] = Field(default_factory=dict)
    context_confirmed: dict[str, bool] = Field(default_factory=dict)


class TenantColumnMappingResponse(BaseModel):
    id: UUID
    entity_type: str
    source_column: str
    target_field: str
    confirmed_count: int
    last_seen_at: datetime


# ── Ingestion confirm ─────────────────────────────────────────────────────────


class ColumnRiskDecision(BaseModel):
    """F8b: decisión del usuario sobre UNA columna riesgosa (F8a) al confirmar.

    ``action`` es un set cerrado de solo dos valores: ``drop_column`` (eliminar
    la columna del mapeo antes de importar) o ``route_affected_rows_to_others``
    (las filas afectadas — vacías/inválidas en esa columna — van a "Otros" en
    vez de importarse con el dato faltante). ``cancel_and_complete`` NO es una
    decisión por columna: es una acción global manejada por ``POST /cancel``."""

    context_id: str
    source_column: str
    target_field: str
    action: Literal["drop_column", "route_affected_rows_to_others"]


class ConfirmIngestionRequest(BaseModel):
    confirmed_fields: dict[str, Any] = Field(
        description=(
            "Which data categories to import from the parsed file. "
            "Keys: 'ventas', 'gastos', 'productos', 'clientes', 'proveedores' (F7 — "
            "clientes/proveedores como entidad de primera clase, ya integrado). "
            "Values: bool."
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
    context_entity: dict[str, Literal["sale", "expense", "product", "customer", "supplier"]] = (
        Field(
            default_factory=dict,
            description=(
                "Override de entity_type por contexto en documentos de texto/imagen: "
                "{context_id: sale|expense|product|customer|supplier}. Permite reasignar un "
                "grupo detectado. Un valor inválido/vacío se rechaza acá (422) — nunca cae "
                "silenciosamente a la entidad original vía `or`."
            ),
        )
    )
    stock_treatment: (
        Literal["opening_balance", "purchase"]
        | dict[str, Literal["opening_balance", "purchase"]]
        | None
    ) = Field(
        default=None,
        description=(
            "Cómo tratar el stock de una hoja de catálogo/lista: 'opening_balance' "
            "(saldo de apertura — mercadería que ya tenías, entra al inventario sin "
            "gasto ni salida de caja) o 'purchase' (compra — genera gasto de mercadería "
            "COGS + baja de caja). Si se omite, se asume saldo de apertura.\n\n"
            "Acepta un dict {context_id: tratamiento} para decidir POR HOJA. Un "
            "archivo puede traer un catálogo que el negocio ya tenía y otra hoja de "
            "compras del mes: un único valor global obliga a mentir en una de las dos "
            "y, si se elige 'purchase', genera COGS por productos que ya figuran como "
            "egresos en el libro diario (doble conteo). Un string plano sigue "
            "significando 'para todas las hojas de producto' (compatibilidad)."
        ),
    )
    column_risk_decisions: list[ColumnRiskDecision] = Field(
        default_factory=list,
        description=(
            "F8b: decisiones del usuario sobre columnas riesgosas (F8a) detectadas "
            "en el preview/column-risk. Opcional — vacío por default para mantener "
            "compatibilidad con confirms previos (F7) que no conocen este campo."
        ),
    )


class ConfirmIngestionResponse(BaseModel):
    file_id: UUID
    status: str
    message: str
    # Avisos human-in-the-loop tras confirmar: compras sin proveedor (→ sentinela "No
    # identificado"), compras sin producto detallado (stock incompleto), filas a "Otros".
    # No bloquean; el frontend los muestra en un banner para que el usuario revise.
    warnings: list[str] = Field(default_factory=list)


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
    # F7d: maestros (clientes/proveedores) reaplicados — creados + actualizados.
    clientes: int = 0
    proveedores: int = 0


class RereadApplyStartResponse(BaseModel):
    """El apply corre en background; se devuelve el run para hacer polling."""

    file_id: UUID
    run_id: UUID
    status: str  # "RUNNING"


class RereadRunStatusResponse(BaseModel):
    """Estado del apply en background (polling). ``status``: RUNNING|APPLIED|FAILED."""

    run_id: UUID
    file_id: UUID
    status: str
    to_update: int = 0
    preserved: int = 0
    new: int = 0
    voided: int = 0
    inserted: int = 0
    legacy_fallback: bool = False
    items: list[RereadItem] = Field(default_factory=list)
    error: str | None = None
    # F7d: maestros (clientes/proveedores) reaplicados — creados + actualizados.
    clientes: int = 0
    proveedores: int = 0


class RereadUndoResponse(BaseModel):
    run_id: UUID
    restored: int
    removed: int
    status: str
    # F9b (Task 7): clientes/proveedores/productos que la relectura tocó pero el
    # undo NO restauró porque alguien los editó después de la relectura (política
    # touched-since — nunca pisar una edición manual en silencio).
    not_reverted_entities: list[dict[str, str]] = Field(default_factory=list)
