"""
Ingestion pipeline endpoints.

POST   /ingestion/upload                — upload file, enqueue parsing job
GET    /ingestion/files                 — list files for current tenant
GET    /ingestion/files/{file_id}/preview   — get parsed_summary_json
POST   /ingestion/files/{file_id}/confirm  — confirm import (NEEDS_CONFIRMATION only)
"""

import hashlib
import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from sqlalchemy import func, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import (
    ensure_tenant_not_under_maintenance,
    get_current_tenant,
    get_current_user,
    require_modify_access,
    require_role,
)
from app.application.services import (
    customer_import_service,
    pipeline_event_service,
    supplier_import_service,
)
from app.application.services import ingestion_import_service as _iis
from app.application.services.column_mapping_service import (
    CANONICAL_FIELDS,
    MASTER_REFERENCE_TARGETS,
    REQUIRED_ALTERNATIVES,
    REQUIRED_FIELDS,
    SINGLE_VALUE_FIELDS,
    ColumnMappingService,
    conditional_requirement,
    missing_required_fields,
    parse_target,
    required_reason,
    validate_required_date_mapping,
)
from app.application.services.column_risk import (
    MappingEntry,
    apply_column_risk_decisions,
    build_contextual_column_risk,
    context_is_included,
    derive_context_mapping_entries,
    validate_column_risk_decisions,
)
from app.application.services.file_deletion_service import (
    build_master_details,
    preview_file_deletion,
    record_import_ledger,
    revert_file_data,
    snapshot_masters_before_import,
)
from app.application.services.file_parsing import (
    IMAGE_MIMES as _IMAGE_MIMES,
)
from app.application.services.file_parsing import (
    MAX_FILE_SIZE_BYTES,
    MAX_FILE_SIZE_LABEL,
    detect_supported_mime,
    parse_uploaded_content,
    sanitize_filename,
)
from app.application.services.file_parsing import (
    SPREADSHEET_MIMES as _SPREADSHEET_MIMES,
)
from app.application.services.ingestion_import_service import (
    EmptyImportError,
    check_nonempty_import,
    insert_confirmed_data,
)
from app.application.services.ingestion_import_service import (
    _capture_column_risk_rows as capture_column_risk_rows,
)
from app.application.services.ingestion_lease_service import (
    ImportLeaseLostError,
    acquire_import_lease,
    finalize_import_lease,
    release_import_lease,
)
from app.application.services.llm_file_type_detector import maybe_detect_file_type
from app.application.services.score_trigger_service import (
    trigger_score_recalculation_after_commit,
)
from app.config.purchase_cost_rollout import purchase_cost_enabled_for
from app.config.settings import get_settings
from app.domain.inventory_effect import (
    EFFECT_LABELS,
    HISTORICAL_REPLAY,
    InvalidInventoryEffectError,
    SheetInventoryProfile,
    default_effect_for,
    options_for,
    resolve_inventory_effects,
)
from app.domain.inventory_replay_gate import (
    MENSAJE_REPLAY_NO_GATEABLE,
    MOTIVO_REPLAY_NO_GATEABLE,
    replay_no_gateable,
)
from app.domain.purchase_cost import CENTAVO
from app.domain.purchase_cost_decision import (
    PurchaseCostDecision as CostDecision,
)
from app.domain.purchase_cost_decision import (
    validate_purchase_cost_decisions,
)
from app.domain.purchase_group import (
    MOTIVO_CIFRAS_DISTINTAS,
    MOTIVO_SIN_ENVIO_COMPARTIDO,
    MOTIVO_SIN_IDENTIDAD,
)
from app.domain.stage_timing import StageTimings
from app.integrations.s3 import S3Client
from app.jobs.ingestion_worker import (
    process_image_ocr,
    process_spreadsheet,
    process_text_document,
)
from app.main import limiter
from app.observability.logger import bind_request_context, get_logger
from app.persistence.db.session import get_db_session
from app.persistence.models.file import (
    PROCESSING_STATUS_DONE,
    PROCESSING_STATUS_FAILED,
    PROCESSING_STATUS_IMPORTING,
    PROCESSING_STATUS_NEEDS_COMPLETION,
    PROCESSING_STATUS_NEEDS_CONFIRMATION,
    PROCESSING_STATUS_PENDING,
    PROCESSING_STATUS_PROCESSING,
    PROCESSING_STATUS_REJECTED,
    UploadedFile,
)
from app.persistence.models.pipeline_event import (
    STAGE_CONFIRM,
    STAGE_PARSE,
    STAGE_REJECT,
    STAGE_UPLOAD,
    STAGE_VALIDATE,
)
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.persistence.repositories.customer_repository import CustomerRepository
from app.persistence.repositories.file_repository import FileRepository
from app.persistence.repositories.supplier_repository import SupplierRepository
from app.schemas.ingestion import (
    ColumnAtRisk,
    ColumnMapping,
    ColumnMappingSuggestion,
    ColumnRiskDecision,
    ColumnRiskRequest,
    ConditionalRequirement,
    ConfirmIngestionRequest,
    ConfirmIngestionResponse,
    ContextualColumnRisk,
    EntityFieldCatalog,
    FieldCatalogEntry,
    FileDeletionPreviewResponse,
    FileDeletionResult,
    FilePreviewResponse,
    FileStatusItem,
    InventoryEffectOption,
    InventoryImpactItem,
    InventoryReplayRequest,
    InventoryReplayResponse,
    MasterPreviewSample,
    MasterPreviewSummary,
    PendingSaleItem,
    PreservedEntity,
    PurchaseGroupItem,
    PurchaseGroupLine,
    PurchaseGroupsRequest,
    PurchaseGroupsResponse,
    RereadApplyStartResponse,
    RereadCounts,
    RereadItem,
    RereadPreviewResponse,
    RereadRunStatusResponse,
    RereadUndoResponse,
    SheetInventoryEffect,
    SheetPurchaseGroups,
    TenantColumnMappingResponse,
    UploadResponse,
)

router = APIRouter()

logger = get_logger(__name__)

#: F-H3.c: cuántos productos del impacto proyectado se devuelven en el confirm.
#: Un catálogo real puede tener más de mil y la respuesta se vuelve impagable;
#: la lista ya viene ordenada con los negativos primero, así que el corte se
#: lleva lo menos interesante. El total completo viaja en
#: `inventory_impact_total` — un corte que no se declara se lee como el total.
_MAX_IMPACTO_LISTADO = 100

# ── Constants ─────────────────────────────────────────────────────────────────
# MAX_FILE_SIZE_BYTES / MAX_FILE_SIZE_LABEL: única fuente de verdad en file_parsing.

FileHint = Literal["ventas", "gastos", "stock", "general"]


def _pick_job(mime: str) -> Any:
    """Return the Celery task to enqueue for a given MIME type."""
    if mime in _IMAGE_MIMES:
        return process_image_ocr
    if mime in _SPREADSHEET_MIMES:
        return process_spreadsheet
    return process_text_document


# ── Sync fallback (beta: Celery/Redis unavailable) ───────────────────────────


async def _process_file_sync(
    record: UploadedFile,
    session: AsyncSession,
    force: bool = False,
) -> None:
    """Process a file synchronously when Celery is unavailable.

    Reuses the parsing helpers from ingestion_worker but runs inside the
    existing request session instead of creating a separate Celery-owned
    engine.  On failure the file is marked FAILED so the user sees a clear
    status instead of being stuck in PENDING forever.
    """
    from app.application.services.validation_gate import ValidationGate  # noqa: PLC0415

    trace_id = record.trace_id or record.id
    bind_request_context(trace_id=trace_id)
    repo = FileRepository(session)
    try:
        record.processing_status = PROCESSING_STATUS_PROCESSING
        await repo.save(record)
        await session.flush()

        s3 = S3Client()
        content = await s3.download(record.s3_key)
        _t0 = time.monotonic()
        summary = parse_uploaded_content(content, record.content_type, record.original_filename)
        await pipeline_event_service.emit_event(
            session,
            trace_id=trace_id,
            tenant_id=record.tenant_id,
            stage=STAGE_PARSE,
            file_id=record.id,
            rows_in=summary.get("row_count"),
            confidence=summary.get("confidence"),
            latency_ms=int((time.monotonic() - _t0) * 1000),
            detail={"file_type": summary.get("file_type"), "warnings": summary.get("warnings")},
        )

        gate = ValidationGate()
        gate_result = gate.validate(summary, force=force)

        if not gate_result.passed:
            record.processing_status = PROCESSING_STATUS_REJECTED
            record.rejection_reason = gate_result.rejection_reason
            record.parsed_summary_json = summary
            await repo.save(record)
            await pipeline_event_service.emit_event(
                session,
                trace_id=trace_id,
                tenant_id=record.tenant_id,
                stage=STAGE_VALIDATE,
                file_id=record.id,
                rows_rejected=summary.get("row_count"),
                detail={"passed": False, "reason": gate_result.rejection_reason},
            )
            logger.info(
                "ingestion.sync_fallback.rejected",
                file_id=str(record.id),
                reason=gate_result.rejection_reason,
            )
            return

        final_summary = gate_result.corrected_summary if gate_result.corrected_summary else summary
        # FASE 2 (A1): desambiguar el tipo por contenido si quedó "general"
        # (fail-silent, solo si ENABLE_LLM_FILE_TYPE_DETECTION está on).
        await maybe_detect_file_type(
            session,
            final_summary,
            trace_id=trace_id,
            tenant_id=record.tenant_id,
            file_id=record.id,
        )
        record.parsed_summary_json = final_summary
        record.processing_status = PROCESSING_STATUS_NEEDS_CONFIRMATION
        await repo.save(record)
        await pipeline_event_service.emit_event(
            session,
            trace_id=trace_id,
            tenant_id=record.tenant_id,
            stage=STAGE_VALIDATE,
            file_id=record.id,
            rows_out=final_summary.get("row_count"),
            confidence=final_summary.get("confidence"),
            detail={"passed": True},
        )

        logger.info(
            "ingestion.sync_fallback.done",
            file_id=str(record.id),
            file_type=final_summary.get("file_type"),
            confidence=final_summary.get("confidence"),
        )

    except Exception as exc:
        logger.error(
            "ingestion.sync_fallback.failed",
            file_id=str(record.id),
            error=str(exc),
        )
        record.parsed_summary_json = {"error": str(exc)}
        record.processing_status = PROCESSING_STATUS_FAILED
        await repo.save(record)


# ── Endpoints ─────────────────────────────────────────────────────────────────


@router.post(
    "/upload",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a file and enqueue ingestion job",
)
@limiter.limit("20/hour")
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    file_hint: FileHint = Query(default="general"),
    force: bool = Query(default=False, description="Forzar ingestión aunque confidence=LOW"),
    allow_duplicate: bool = Query(
        default=False,
        description=(
            "Permitir reimportar un archivo cuyo contenido EXACTO ya fue importado. "
            "Por defecto se bloquea con 409 para no duplicar datos."
        ),
    ),
    tenant: Tenant = Depends(get_current_tenant),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> UploadResponse:
    content = await file.read()

    if len(content) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"El archivo supera el tamaño máximo de {MAX_FILE_SIZE_LABEL}. "
                "Dividilo en archivos más chicos e importalos por separado."
            ),
        )

    filename = sanitize_filename(file.filename or "upload")
    try:
        detected_mime = detect_supported_mime(content, filename)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=str(exc),
        ) from exc

    # A6: dedup de re-upload. Si este contenido EXACTO ya fue importado (DONE),
    # BLOQUEAR (409) — reimportarlo duplica ventas/gastos/stock. El override
    # explícito (allow_duplicate=true) permite reimportar a propósito. Se chequea
    # ANTES de subir a S3 / crear el registro para no dejar objetos ni filas huérfanas.
    content_hash = hashlib.sha256(content).hexdigest()
    repo = FileRepository(session)
    _dup = await repo.find_imported_by_content_hash(tenant.tenant_id, content_hash)
    if _dup is not None and not allow_duplicate:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Este archivo ya fue importado antes ('{_dup.original_filename}'). "
                "Reimportarlo duplicaría los datos. Si querés importarlo igual, "
                "reintentá con allow_duplicate=true."
            ),
        )
    # C: re-subida por NOMBRE (mismo nombre, contenido distinto = versión actualizada).
    # No se bloquea, pero se avisa: reimportar todo duplica las filas repetidas. Solo si
    # no es el caso de dup exacto (que ya se manejó arriba).
    _name_dup = (
        await repo.find_imported_by_filename(
            tenant.tenant_id, filename, exclude_content_hash=content_hash
        )
        if _dup is None
        else None
    )

    # Build S3 key: uploads/{tenant_id}/{uuid}/{filename}
    file_uuid = uuid.uuid4()
    s3_key = f"uploads/{tenant.tenant_id}/{file_uuid}/{filename}"

    s3 = S3Client()
    stored_key = await s3.upload_to_key(content=content, key=s3_key, content_type=detected_mime)

    # FASE 0: trazabilidad (trace_id agrupa el ciclo de vida).
    trace_id = uuid.uuid4()
    bind_request_context(trace_id=trace_id)

    record = UploadedFile(
        tenant_id=tenant.tenant_id,
        uploaded_by=current_user.user_id,
        original_filename=filename,
        s3_key=stored_key,
        content_type=detected_mime,
        size_bytes=len(content),
        purpose=file_hint,
        status="uploaded",
        processing_status=PROCESSING_STATUS_PENDING,
        trace_id=trace_id,
        content_hash=content_hash,
    )
    saved = await repo.save(record)

    await pipeline_event_service.emit_event(
        session,
        trace_id=trace_id,
        tenant_id=tenant.tenant_id,
        stage=STAGE_UPLOAD,
        file_id=saved.id,
        detail={
            "filename": filename,
            "content_type": detected_mime,
            "size_bytes": len(content),
            "file_hint": file_hint,
            "content_hash": content_hash,
        },
    )

    # Llegamos acá con un duplicado solo si allow_duplicate=true (override explícito):
    # dejamos el aviso informativo en la respuesta para que el frontend lo muestre.
    dup_of = _dup.id if _dup else None
    if _dup:
        dup_warning: str | None = (
            f"Este archivo ya fue importado antes ('{_dup.original_filename}'). "
            "Reimportación forzada: podrías estar duplicando datos."
        )
    elif _name_dup:
        dup_warning = (
            f"Ya importaste un archivo con este nombre ('{_name_dup.original_filename}'). "
            "Si es una corrección del mismo archivo, usá 'Releer' sobre el original (lo "
            "reemplaza sin duplicar). Si son datos nuevos, subí un archivo solo con las "
            "filas nuevas — si reimportás todo, se duplican las filas repetidas."
        )
    else:
        dup_warning = None

    if get_settings().USE_LOCAL_FALLBACK:
        await _process_file_sync(saved, session, force=force)
        return UploadResponse(
            file_id=saved.id, status="PROCESSING", duplicate_of=dup_of, warning=dup_warning
        )

    # Enqueue parsing job — fall back to sync processing if Celery/Redis
    # is unavailable (beta: single Railway service without workers).
    job = _pick_job(detected_mime)
    try:
        job.delay(str(saved.id), str(tenant.tenant_id), force)
    except Exception:
        logger.warning(
            "ingestion.celery_unavailable",
            file_id=str(saved.id),
            msg="Celery/Redis no disponible, procesando archivo de forma síncrona.",
        )
        await _process_file_sync(saved, session, force=force)
        return UploadResponse(
            file_id=saved.id, status="PROCESSING", duplicate_of=dup_of, warning=dup_warning
        )

    return UploadResponse(
        file_id=saved.id, status="PROCESSING", duplicate_of=dup_of, warning=dup_warning
    )


@router.get(
    "/files",
    response_model=list[FileStatusItem],
    summary="List ingested files for the current tenant",
)
async def list_files(
    processing_status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[UploadedFile]:
    repo = FileRepository(session)
    return await repo.list_by_tenant_filtered(
        tenant_id=tenant.tenant_id,
        processing_status=processing_status,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/files/{file_id}",
    response_model=FileStatusItem,
    summary="Get a single ingested file by id",
)
async def get_file(
    file_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> UploadedFile:
    """Estado de UN archivo, sin depender de la paginación del listado.

    `GET /files` pagina de a 50 ordenando por fecha descendente, así que quien
    abre un link a un archivo viejo no tiene garantía de encontrarlo ahí. Sin
    esta ruta, el front no podía distinguir "no existe" de "no entró en la
    página" y terminaba avisando que el archivo se había eliminado sobre uno
    que estaba vivo.

    El 404 es lo que le da derecho a esa afirmación: solo se devuelve cuando el
    archivo no existe para este tenant o fue borrado.
    """
    repo = FileRepository(session)
    archivo = await repo.get_by_id(file_id, tenant.tenant_id)
    if archivo is None:
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    return archivo


async def _build_master_previews(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    summary: dict[str, Any],
) -> list[MasterPreviewSummary]:
    """F7d: preview universal de maestros — para cada hoja de clientes/proveedores
    detectada, estima create/update/needs_review/invalid/duplicate contra el
    motor de identidad de F7b (``build_import_preview``) usando un mapeo
    heurístico (las mismas sugerencias que ``GET /column-mappings`` — el usuario
    todavía no eligió el mapeo final, eso pasa recién en el confirm). Solo
    diagnóstico, no persiste nada.

    PII minimizada: el registro completo (con documento/email/teléfono) vive
    solo en memoria durante el request para poder matchear contra los
    existentes — la respuesta serializada solo lleva nombre + estado + un
    diagnóstico corto por fila de muestra (ver ``MasterPreviewSample``).
    """
    mapping_ctxs = list(summary.get("mapping_contexts") or [])
    master_ctxs = [c for c in mapping_ctxs if c.get("entity_type") in ("customer", "supplier")]
    if not master_ctxs:
        # Legacy: archivo de un solo contexto (sin mapping_contexts) ya inferido
        # como "clientes"/"proveedores" por file_parsing.
        inferred = summary.get("inferred_type")
        _headers = summary.get("headers", [])
        if inferred == "clientes":
            master_ctxs = [{"context_id": None, "entity_type": "customer", "headers": _headers}]
        elif inferred == "proveedores":
            master_ctxs = [{"context_id": None, "entity_type": "supplier", "headers": _headers}]
    if not master_ctxs:
        return []

    mapping_svc = ColumnMappingService(session)
    customer_repo = CustomerRepository(session)
    supplier_repo = SupplierRepository(session)
    existing_customers: list[Any] | None = None
    existing_suppliers: list[Any] | None = None
    previews: list[MasterPreviewSummary] = []

    for ctx in master_ctxs:
        entity = ctx["entity_type"]
        ctx_id = ctx.get("context_id")
        bucket_key = "clientes_detectados" if entity == "customer" else "proveedores_detectados"
        rows = _iis._rows_for_context(summary.get(bucket_key) or [], ctx_id or "")
        if not rows:
            continue
        headers = ctx.get("headers") or list(rows[0].keys())
        sample_rows = ctx.get("preview_rows") or rows[:10]
        # Review 7d (Important): este GET puede correr en cada poll/reload de la
        # página — NUNCA debe disparar la 4ª capa LLM (costo + latencia sin cache
        # ni cap), aunque ENABLE_LLM_COLUMN_MAPPING esté prendido. Solo
        # determinístico acá; el LLM sigue disponible en GET /column-mappings,
        # que el usuario dispara explícitamente al armar el mapeo real.
        suggestions = await mapping_svc.suggest_mappings(
            tenant_id, entity, headers, sample_rows, allow_llm=False
        )
        target_to_col = {
            s["target_field"]: s["source_column"] for s in suggestions if s["status"] == "mapped"
        }
        if not target_to_col:
            continue  # sin mapeo estimable: no se adivina el shape (mismo criterio que el confirm)

        preview: customer_import_service.ImportPreview | supplier_import_service.ImportPreview
        if entity == "customer":
            _fields = CANONICAL_FIELDS["customer"]
            records = [
                {f: row.get(target_to_col[f]) for f in _fields if f in target_to_col}
                for row in rows
            ]
            if existing_customers is None:
                existing_customers = await customer_repo.list_for_dedup(tenant_id)
            preview = customer_import_service.build_import_preview(records, existing_customers)
        else:
            _fields = CANONICAL_FIELDS["supplier"]
            records = [
                {f: row.get(target_to_col[f]) for f in _fields if f in target_to_col}
                for row in rows
            ]
            if existing_suppliers is None:
                existing_suppliers = await supplier_repo.list_for_dedup(tenant_id)
            preview = supplier_import_service.build_import_preview(records, existing_suppliers)

        samples = [
            MasterPreviewSample(
                row_index=item.row_index,
                status=item.status,
                display_name=str(item.fields.get("name"))[:80] if item.fields.get("name") else None,
                existing_name=item.existing_name,
                issue=item.issues[0] if item.issues else None,
            )
            for item in preview.items[:5]
        ]
        previews.append(
            MasterPreviewSummary(
                context_id=ctx_id,
                entity_type=entity,
                to_create=preview.to_create,
                to_update=preview.to_update,
                needs_review=preview.needs_review,
                invalid=preview.invalid,
                duplicates=preview.duplicates,
                samples=samples,
            )
        )
    return previews


@router.get(
    "/files/{file_id}/preview",
    response_model=FilePreviewResponse,
    summary="Get parsed summary for user review",
)
async def get_file_preview(
    file_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> FilePreviewResponse:
    repo = FileRepository(session)
    record = await repo.get_by_id(file_id, tenant.tenant_id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Archivo no encontrado.")

    if record.processing_status in (PROCESSING_STATUS_PENDING, "PROCESSING"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"El archivo aún se está procesando (estado: {record.processing_status}).",
        )

    raw_at_risk = (record.parsed_summary_json or {}).get("columns_at_risk", [])
    columns_at_risk = [ColumnAtRisk(**col) for col in raw_at_risk if isinstance(col, dict)]

    summary = record.parsed_summary_json or {}

    # F7d: preview de maestros — best-effort, nunca debe romper el preview del
    # archivo (es un diagnóstico adicional, no el dato principal de la respuesta).
    master_previews: list[MasterPreviewSummary] = []
    try:
        master_previews = await _build_master_previews(session, tenant.tenant_id, summary)
    except Exception:
        logger.warning("ingestion.preview.master_preview_failed", file_id=str(file_id))

    # F8a: riesgo contextual desde las sugerencias de mapeo (informativo, best-effort).
    # Sin inclusión (preview no conoce aún la decisión del usuario → muestra todo).
    contextual_risk: list[ContextualColumnRisk] = []
    try:
        entries, entities = await derive_context_mapping_entries(session, tenant.tenant_id, summary)
        contextual_risk = [
            ContextualColumnRisk(**row)
            for row in build_contextual_column_risk(
                summary, entries, context_entities=entities
            )
        ]
    except Exception:
        logger.warning("ingestion.preview.contextual_risk_failed", file_id=str(file_id))

    return FilePreviewResponse(
        file_id=record.id,
        processing_status=record.processing_status,
        parsed_summary_json=record.parsed_summary_json,
        columns_at_risk=columns_at_risk,
        contextual_column_risk=contextual_risk,
        master_previews=master_previews,
    )


@router.post(
    "/files/{file_id}/column-risk",
    response_model=list[ContextualColumnRisk],
    summary="Recompute contextual column risk for a draft mapping (read-only)",
)
async def compute_column_risk(
    file_id: uuid.UUID,
    body: ColumnRiskRequest,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[ContextualColumnRisk]:
    """F8a: recalcula el riesgo contextual con el mapeo efectivo del usuario
    (incluye ``user_selected`` por columna → ``affected_rows`` exacto y el set
    accionable preciso). READ-ONLY: no escribe datos comerciales ni el summary."""
    repo = FileRepository(session)
    record = await repo.get_by_id(file_id, tenant.tenant_id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Archivo no encontrado.")
    if record.processing_status in (PROCESSING_STATUS_PENDING, "PROCESSING"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"El archivo aún se está procesando (estado: {record.processing_status}).",
        )

    summary = record.parsed_summary_json or {}
    entries, entities = await derive_context_mapping_entries(
        session,
        tenant.tenant_id,
        summary,
        user_mappings=body.column_mappings,
        # Literal[...] a nivel schema (rechaza valores inválidos en el borde de
        # la API); el resto del pipeline maneja entity_type como str genérico.
        context_entity=cast("dict[str, str]", body.context_entity),
    )
    # Con el mapeo efectivo del usuario se aplica la MISMA inclusión que el confirm:
    # los contextos que el usuario decidió NO importar no generan riesgo accionable.
    return [
        ContextualColumnRisk(**row)
        for row in build_contextual_column_risk(
            summary,
            entries,
            context_entities=entities,
            confirmed_fields=body.confirmed_fields,
            context_confirmed=body.context_confirmed,
        )
    ]


@router.post(
    "/files/{file_id}/inventory-effects",
    response_model=list[SheetInventoryEffect],
    summary="Modo de inventario propuesto y opciones por hoja, para un mapeo borrador",
)
async def compute_inventory_effects(
    file_id: uuid.UUID,
    body: ColumnRiskRequest,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[SheetInventoryEffect]:
    """F-H3.e: qué le propone Véktor al inventario de cada hoja, y entre qué puede
    elegir el usuario. READ-ONLY.

    Existe porque el default (`default_effect_for`) y las opciones (`options_for`)
    son reglas de DOMINIO que dependen de la entidad de la hoja y de los campos que
    el mapeo cubre: una hoja de ventas sin columna de cantidad no mueve unidades, y
    ese mismo archivo con `cantidad` mapeada sí. Calcularlo en la UI sería una copia
    de la regla que se desactualiza — el defecto que ya se pagó con el catálogo de
    campos, donde la pantalla mostraba una cosa y mandaba otra.

    Reusa `ColumnRiskRequest` a propósito: la entrada es exactamente la misma —el
    mapeo borrador con su entidad efectiva por hoja— y un schema gemelo sería otra
    copia que puede divergir.
    """
    repo = FileRepository(session)
    record = await repo.get_by_id(file_id, tenant.tenant_id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Archivo no encontrado.")

    summary = record.parsed_summary_json or {}
    entries, entities = await derive_context_mapping_entries(
        session,
        tenant.tenant_id,
        summary,
        user_mappings=body.column_mappings,
        context_entity=cast("dict[str, str]", body.context_entity),
    )
    etiquetas = {
        ctx["context_id"]: str(ctx.get("label") or ctx["context_id"]).strip()
        for ctx in summary.get("mapping_contexts", [])
        if ctx.get("context_id")
    }

    # Los campos salen del mapeo QUE MANDÓ EL CLIENTE, no de `entries`.
    # `derive_context_mapping_entries` completa las columnas sin mapear con las
    # sugerencias (historial del tenant, heurística), y el confirm NO las usa para
    # el perfil: arma `SheetInventoryProfile` con `body.column_mappings`. Leer los
    # derivados acá haría que la pantalla ofrezca "aplicar la historia" en una hoja
    # donde el usuario todavía no mapeó la cantidad, y que el confirm resuelva otro
    # default. La entidad efectiva sí sale de `entities`: ahí `derive` resuelve el
    # override del usuario con la misma prioridad que el confirm.
    campos_por_contexto: dict[str, set[str]] = defaultdict(set)
    for m in body.column_mappings:
        if parse_target(m.target_field).kind == "canonical":
            campos_por_contexto[m.context_id or ""].add(m.target_field)

    resultado: list[SheetInventoryEffect] = []
    for context_id in entries:
        perfil = SheetInventoryProfile(
            context_id=context_id,
            entity=entities.get(context_id),
            # Sólo campos CANÓNICOS, igual que el confirm: un
            # `custom_field:cantidad` guarda el dato pero el importador no lo lee
            # como cantidad, así que no habilita mover inventario.
            mapped_fields=frozenset(campos_por_contexto.get(context_id, set())),
        )
        resultado.append(
            SheetInventoryEffect(
                context_id=context_id,
                label=etiquetas.get(context_id, context_id).strip(),
                default=default_effect_for(perfil),
                options=[
                    InventoryEffectOption(value=v, label=EFFECT_LABELS[v])
                    for v in options_for(perfil)
                ],
            )
        )
    return resultado


#: F-H6.d: cuántos grupos de compra se listan por hoja. Un libro de compras real
#: puede traer cientos de comprobantes y la respuesta se vuelve impagable; el
#: total completo viaja en `grupos_total` — un corte que no se declara se lee como
#: el total (mismo criterio que `inventory_impact`).
_MAX_GRUPOS_LISTADOS = 50

#: Por qué un grupo no admite reparto, en castellano. Las CLAVES son los motivos
#: del dominio (`purchase_group`): esto traduce, no vuelve a decidir. Una segunda
#: tabla de reglas acá podría discrepar con la que aplica el importador.
_MOTIVO_EN_CASTELLANO: dict[str, str] = {
    MOTIVO_SIN_IDENTIDAD: (
        "Las filas no dicen a qué comprobante pertenecen (falta el número de "
        "remito o factura, o el proveedor). Una cifra de envío repetida en diez "
        "filas es indistinguible de diez envíos iguales, así que Véktor no la "
        "reparte por su cuenta. Mapeá el número de comprobante, o declará que "
        "toda la hoja es una sola compra."
    ),
    MOTIVO_CIFRAS_DISTINTAS: (
        "El mismo comprobante trae más de una cifra de envío distinta. Pueden ser "
        "un flete y un seguro, o el total en una fila y el prorrateo en las otras: "
        "sumarlas como si fueran una sola sería elegir por vos."
    ),
    MOTIVO_SIN_ENVIO_COMPARTIDO: (
        "Este comprobante no declara ningún costo de envío para repartir entre sus "
        "líneas."
    ),
}

#: Qué se le dice a un tenant que todavía no tiene el motor de costos de compra.
#: UN solo texto para los DOS puntos de control (el preview y el confirm): son la
#: misma limitación, y dos redacciones se leerían como dos problemas distintos.
#: No nombra la variable de entorno ni la allowlist — eso es de la operación, no
#: del negocio del usuario.
_MOTOR_DE_COSTOS_DESHABILITADO = (
    "El cálculo de costos de compra —repartir el envío del comprobante entre sus "
    "líneas y aplicar descuentos e impuestos al costo— todavía no está habilitado "
    "en esta cuenta. Se está activando de a poco. Mientras tanto el import toma el "
    "monto de cada fila como costo final; si necesitás el reparto, escribinos."
)

#: Cuando la hoja no forma NINGÚN grupo. No es lo mismo que un grupo que no puede
#: repartir: acá no hay nada que agrupar todavía.
_SIN_GRUPOS = (
    "Esta hoja todavía no declara costos de compra que se puedan repartir: no hay "
    "ninguna columna mapeada como envío, descuento, impuestos o flete de línea."
)


def _monto(valor: Decimal) -> str:
    """Un monto listo para mostrar, al centavo y como string decimal.

    String y no float: el dominio ya redondeó con ``ROUND_HALF_UP`` (el redondeo
    de cualquier planilla, no el bancario de Python) y mandarlo como número deja
    que el navegador lo vuelva a redondear — la pantalla mostraría un centavo
    distinto del que se va a guardar.
    """
    return str(valor.quantize(CENTAVO, rounding=ROUND_HALF_UP))


@router.post(
    "/files/{file_id}/purchase-groups",
    response_model=PurchaseGroupsResponse,
    summary="Qué líneas componen cada compra y cómo quedaría repartido su envío",
)
async def compute_purchase_groups(
    file_id: uuid.UUID,
    body: PurchaseGroupsRequest,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> PurchaseGroupsResponse:
    """F-H6.d: el reparto del costo compartido, ANTES de confirmar. READ-ONLY.

    Elegir «repartir el envío por subtotal» sin ver el resultado es aceptar a
    ciegas un cambio en el costo de cada producto —y por lo tanto en su margen—.
    Esta pantalla muestra qué líneas quedaron juntas, cuánto le tocó a cada una y
    cuánto quedó sin repartir.

    **Los números salen del MISMO planificador que corre el import**
    (`_planificar_costos_de_la_hoja`), no de un cálculo propio. Es la garantía que
    reclama por escrito el docstring de `identidad_de_comprobante`: si el preview
    y el importador agruparan distinto, la pantalla ofrecería repartir un costo
    entre líneas que después no se van a agrupar, y el usuario vería un reparto
    que no ocurrió. Hay un test que compara las dos salidas sobre el mismo archivo.

    Hermano de `/column-risk` y `/inventory-effects`: mismos guards (404/409),
    misma entrada (el mapeo borrador) y la misma regla sobre de dónde salen los
    campos — del mapeo QUE MANDÓ EL CLIENTE, no de las sugerencias derivadas.
    """
    # La compuerta va PRIMERO, antes de tocar el archivo: no depende de él, y un
    # tenant sin el motor habilitado no tiene por qué recibir un 404 o un 409
    # sobre una pantalla que no puede usar. Se valida acá y no sólo en el
    # frontend: esconder el control no impide que alguien llame la API.
    if not purchase_cost_enabled_for(tenant.tenant_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=_MOTOR_DE_COSTOS_DESHABILITADO,
        )

    repo = FileRepository(session)
    record = await repo.get_by_id(file_id, tenant.tenant_id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Archivo no encontrado.")
    if record.processing_status in (PROCESSING_STATUS_PENDING, "PROCESSING"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"El archivo aún se está procesando (estado: {record.processing_status}).",
        )

    summary = record.parsed_summary_json or {}
    contextos = [c for c in (summary.get("mapping_contexts") or []) if c.get("context_id")]

    # Entidad EFECTIVA por hoja, con la MISMA prioridad que el confirm y que el
    # importador: override del usuario → entidad original del summary. Sin esto,
    # una hoja general que el usuario reasignó a Gastos no mostraría sus grupos
    # aunque el import sí los vaya a armar.
    override = cast("dict[str, str]", body.context_entity or {})

    # Los campos salen del mapeo QUE MANDÓ EL CLIENTE, por la misma razón que en
    # `/inventory-effects`: `derive_context_mapping_entries` completa las columnas
    # sin mapear con sugerencias, y el confirm NO las usa para esto.
    mapeo_por_contexto: dict[str, dict[str, str]] = defaultdict(dict)
    for m in body.column_mappings:
        mapeo_por_contexto[m.context_id or ""][m.source_column] = m.target_field

    decisiones = {
        d.context_id: CostDecision(
            context_id=d.context_id,
            base=d.base,
            shared_shipping=d.shared_shipping,
            line_shipping=d.line_shipping,
        )
        for d in body.purchase_cost_decisions
    }
    sin_comprobante = {d.context_id: d.action for d in body.shipping_decisions}

    hojas: list[SheetPurchaseGroups] = []
    for ctx in contextos:
        ctx_id = str(ctx["context_id"])
        entidad = override.get(ctx_id) or ctx.get("entity_type")
        # Sólo compras: el reparto del costo compartido es un problema de hojas de
        # gastos. Una hoja de ventas no tiene comprobante de proveedor que repartir.
        if entidad != "expense":
            continue

        # Las filas viven en el bucket del tipo ORIGINAL de la hoja, igual que en
        # `_filas_y_mapeo`: una hoja que el parser mandó a otro bucket y el usuario
        # reasignó a Gastos tiene sus filas donde las dejó el parser.
        bucket = summary.get(
            _iis.ENTITY_BUCKET.get(ctx.get("entity_type") or "", "otros_detectados"), []
        )
        filas = _iis._rows_for_context(bucket, ctx_id)
        mapeo = mapeo_por_contexto.get(ctx_id, {})
        cols, _cf_cols, _cruzados = (
            _iis._resolve_target_cols(mapeo) if mapeo else ({}, {}, {})
        )

        _costos, _ilegibles, plan = _iis._planificar_costos_de_la_hoja(
            ctx_id,
            filas,
            cols,
            decisiones,
            sin_comprobante=sin_comprobante.get(ctx_id),
        )

        nombre_col = cols.get("product_name") or cols.get("name")

        def _celda(row: int, col: str | None, _filas: list[dict[str, Any]] = filas) -> str | None:
            """El valor CRUDO de una celda, como lo escribió el usuario.

            La clave del grupo viene normalizada (minúsculas, sin espacios al
            costado) porque así es como se agrupa; mostrarla tal cual convertiría
            «Distribuidora Sur» en «distribuidora sur» en la pantalla. Se agrupa
            por la clave y se muestra el original.
            """
            if not col or row >= len(_filas):
                return None
            valor = _filas[row].get(col)
            return (str(valor).strip() or None) if valor is not None else None

        grupos: list[PurchaseGroupItem] = []
        for grupo in plan.groups:
            # Del PRIMER renglón del grupo: todos comparten la clave normalizada,
            # así que si el archivo escribió el mismo proveedor con dos grafías
            # cualquiera de las dos nombra la misma compra.
            _primera = grupo.row_indexes[0] if grupo.row_indexes else 0
            lineas = [
                PurchaseGroupLine(
                    row_index=row,
                    producto=(
                        str(filas[row].get(nombre_col)).strip() or None
                        if nombre_col
                        and row < len(filas)
                        and filas[row].get(nombre_col) is not None
                        else None
                    ),
                    subtotal=_monto(costo.base if costo else Decimal("0")),
                    envio_asignado=_monto(
                        costo.shipping_allocated if costo else Decimal("0")
                    ),
                    costo_total=_monto(costo.total if costo else Decimal("0")),
                    costo_unitario_final=(
                        _monto(costo.unit_cost_final)
                        if costo is not None and costo.unit_cost_final is not None
                        else None
                    ),
                )
                for row in grupo.row_indexes
                # `_costos` no tiene entrada para una fila sin monto: no hay base
                # sobre la cual ajustar nada. Igual se lista —es una línea de la
                # compra, y puede ser justo la que trae la cifra de envío— con sus
                # montos en cero en vez de desaparecer del grupo.
                for costo in [_costos.get(row)]
            ]
            repartido = sum(
                (_costos[row].shipping_allocated for row in grupo.row_indexes if row in _costos),
                Decimal("0"),
            )
            grupos.append(
                PurchaseGroupItem(
                    proveedor=(
                        _celda(_primera, cols.get("supplier_name")) if grupo.key else None
                    ),
                    comprobante=(
                        _celda(_primera, cols.get("invoice_number")) if grupo.key else None
                    ),
                    subtotal=_monto(grupo.subtotal),
                    envio_compartido=_monto(grupo.shared_shipping),
                    repartido=_monto(repartido),
                    sin_repartir=_monto(grupo.shared_shipping - repartido),
                    distribuible=grupo.distribuible,
                    motivo_no_distribuible=(
                        _MOTIVO_EN_CASTELLANO.get(grupo.motivo_no_distribuible or "")
                        or None
                    ),
                    lineas=lineas,
                )
            )

        # `puede_distribuir` se DERIVA del plan, no de una segunda lectura del
        # mapeo: preguntarle acá "¿hay columna de comprobante?" sería reimplementar
        # el criterio que ya aplicó `build_purchase_groups`, y las dos respuestas
        # podrían divergir sobre el mismo archivo.
        puede = any(g.distribuible for g in plan.groups)
        motivo: str | None = None
        if not puede:
            if not plan.groups:
                motivo = _SIN_GRUPOS
            else:
                # El motivo dominante: con varios grupos frenados por causas
                # distintas, mostrar sólo el primero escondería la otra mitad.
                _causas = [g.motivo_no_distribuible for g in plan.groups]
                _dominante = max(set(_causas), key=_causas.count) or ""
                motivo = _MOTIVO_EN_CASTELLANO.get(_dominante, _SIN_GRUPOS)

        hojas.append(
            SheetPurchaseGroups(
                context_id=ctx_id,
                label=str(ctx.get("label") or ctx_id).strip(),
                puede_distribuir=puede,
                motivo=motivo,
                grupos_total=len(plan.groups),
                grupos=grupos[:_MAX_GRUPOS_LISTADOS],
                filas_sin_comprobante=sum(
                    len(g.row_indexes) for g in plan.groups if g.key is None
                ),
            )
        )

    return PurchaseGroupsResponse(sheets=hojas)


@router.post(
    "/files/{file_id}/cancel",
    summary=(
        "Cancel confirmation; mark file as NEEDS_COMPLETION so user can re-upload with complete "
        "data"
    ),
)
async def cancel_file_confirmation(
    file_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    repo = FileRepository(session)
    record = await repo.get_by_id(file_id, tenant.tenant_id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Archivo no encontrado.")

    if record.processing_status != PROCESSING_STATUS_NEEDS_CONFIRMATION:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Solo se puede cancelar un archivo en estado NEEDS_CONFIRMATION.",
        )

    record.processing_status = PROCESSING_STATUS_NEEDS_COMPLETION
    await repo.save(record)
    await session.commit()

    logger.info("ingestion.cancel", file_id=str(file_id))
    return {"file_id": str(file_id), "status": PROCESSING_STATUS_NEEDS_COMPLETION}


@router.get(
    "/files/{file_id}/deletion-preview",
    response_model=FileDeletionPreviewResponse,
    summary="Qué datos se borran si se elimina este archivo",
)
async def get_deletion_preview(
    file_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> FileDeletionPreviewResponse:
    """Read-only: alimenta la advertencia previa al borrado.

    El borrado revierte TAMBIÉN lo editado a mano, así que el usuario tiene que
    poder ver qué se lleva puesto antes de aceptar.
    """
    repo = FileRepository(session)
    record = await repo.get_by_id(file_id, tenant.tenant_id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Archivo no encontrado.")

    resumen = await preview_file_deletion(session, file_id, tenant.tenant_id)
    return FileDeletionPreviewResponse(file_id=file_id, **resumen)


@router.delete(
    "/files/{file_id}",
    response_model=FileDeletionResult,
    summary="Elimina un archivo y revierte los datos que importó",
    dependencies=[Depends(require_modify_access)],
)
async def delete_file(
    file_id: uuid.UUID,
    confirm: bool = Query(
        default=False,
        description=(
            "Confirmación explícita de que se van a borrar los datos importados "
            "por el archivo. Sin esto, 409 con el detalle de qué se borraría."
        ),
    ),
    tenant: Tenant = Depends(get_current_tenant),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> FileDeletionResult:
    """Borra el archivo Y revierte lo que importó.

    Antes esto solo hacía ``deleted_at = now()``: el archivo desaparecía de la
    lista y sus ventas/gastos/productos seguían en el dashboard. Peor, volver a
    subirlo corregido duplicaba todo, porque las huellas anti-duplicado incluyen
    el ``uploaded_file_id`` y un archivo nuevo no reconoce lo del anterior.

    Gateado con ``require_modify_access`` (PIN): pasó de ocultar un archivo a
    destruir datos de negocio, el mismo riesgo que el DELETE de ventas y gastos.

    ``confirm=true`` es obligatorio — la reversa alcanza también a los registros
    editados a mano, y esa decisión la toma el usuario, no el endpoint.
    """
    repo = FileRepository(session)
    record = await repo.get_by_id(file_id, tenant.tenant_id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Archivo no encontrado.")

    # Un import en curso rebota por ESO, antes que por falta de confirmación:
    # pedirle al usuario que confirme un borrado que igual va a fallar sería un
    # mensaje equivocado. El CAS de abajo sigue siendo el guard real de la carrera.
    if record.processing_status == PROCESSING_STATUS_IMPORTING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No se puede eliminar un archivo mientras se importa.",
        )

    if not confirm:
        resumen = await preview_file_deletion(session, file_id, tenant.tenant_id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "CONFIRM_REQUIRED",
                "message": (
                    "Borrar este archivo también borra los datos que importó. "
                    "Confirmá para continuar."
                ),
                **resumen,
            },
        )

    # F4: soft-delete ATÓMICO (CAS) — cierra la carrera delete↔confirm. Un check en
    # Python (leer estado → borrar) deja un TOCTOU: el confirm podría tomar el lease
    # entre el check y el UPDATE. El CAS `WHERE deleted_at IS NULL AND status !=
    # IMPORTING` garantiza que NO se borra un archivo con import en curso, y el CAS
    # del lease (que exige `deleted_at IS NULL`) garantiza lo simétrico.
    # FASE 0: el crudo en R2 se preserva (input para ML + respaldo).
    result = await session.execute(
        update(UploadedFile)
        .where(
            UploadedFile.id == file_id,
            UploadedFile.tenant_id == tenant.tenant_id,
            UploadedFile.deleted_at.is_(None),
            UploadedFile.processing_status != PROCESSING_STATUS_IMPORTING,
        )
        .values(deleted_at=func.now())
    )
    if cast("CursorResult[Any]", result).rowcount == 0:
        # rowcount 0 sólo se alcanza por una carrera: el archivo estaba vivo en el
        # `get_by_id` de arriba pero para el CAS ya está IMPORTING (confirm ganó) o
        # borrado (otro delete ganó). Re-consultar para distinguir 409 de 204.
        # (Un re-delete SECUENCIAL de un archivo ya borrado corta antes, en el 404
        # del `get_by_id`, porque filtra `deleted_at IS NULL`.)
        current = await repo.get_by_id(file_id, tenant.tenant_id)
        if current is not None and current.processing_status == PROCESSING_STATUS_IMPORTING:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="No se puede eliminar un archivo mientras se importa.",
            )
        # El CAS perdió: el archivo es de otro request (un confirm en curso o un
        # delete que ya revirtió). No se revierte nada — hacerlo borraría datos
        # que este request no marcó como suyos.
        _revertido: dict[str, Any] = {}
    else:
        # La reversa va SOLO si el CAS ganó.
        _revertido = await revert_file_data(
            session, file_id, tenant.tenant_id, actor_user_id=user.user_id
        )
        # Los scores quedaban calculados sobre datos que este borrado acaba de
        # revertir. Se dispara DESPUÉS del commit: el worker abre su propia
        # sesión, así que encolarlo antes lo haría leer un estado que todavía no
        # existe — o que un rollback va a descartar.
        trigger_score_recalculation_after_commit(
            session, str(tenant.tenant_id), "file_deleted"
        )
    await session.commit()

    # Respuesta explícita, nunca un 204 mudo: la UI necesita distinguir "se
    # eliminó todo" de "se eliminó, pero N cosas quedaron y hay que revisarlas".
    # Estos números salen de la reversa YA ejecutada dentro de la transacción —
    # el preview era una estimación previa, esto es lo que efectivamente pasó.
    _conservados = [
        PreservedEntity(**c) for c in cast("list[Any]", _revertido.get("conservados", []))
    ]
    return FileDeletionResult(
        fully_reverted=not _conservados,
        deleted={
            "sales": int(_revertido.get("ventas", 0)),
            "expenses": int(_revertido.get("gastos", 0)),
            "products": int(_revertido.get("productos", 0)),
            "stock_movements": int(_revertido.get("movimientos_stock", 0)),
            "others": int(_revertido.get("otros", 0)),
            "masters": int(_revertido.get("maestros_desactivados", 0)),
        },
        restored={
            "products": int(_revertido.get("productos_restaurados", 0)),
            "masters": int(_revertido.get("maestros_restaurados", 0)),
        },
        conservados=_conservados,
    )


@router.post(
    "/files/{file_id}/reprocess",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Re-enqueue a PENDING or FAILED file for processing",
)
async def reprocess_file(
    file_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> dict[str, Any]:
    repo = FileRepository(session)
    record = await repo.get_by_id(file_id, tenant.tenant_id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Archivo no encontrado.")

    # PROCESSING "trabado": si el worker murió (SIGKILL por time_limit, sin escribir
    # FAILED), el archivo queda eterno en PROCESSING. Lo consideramos reprocesable
    # cuando lleva más que el hard time_limit de Celery (180s) + margen → sin riesgo
    # de pisar un job realmente en vuelo. Así el usuario re-lee el archivo (ya está
    # en R2) sin re-subirlo.
    stale_after = timedelta(seconds=300)
    updated = record.updated_at
    if updated is not None and updated.tzinfo is None:
        updated = updated.replace(tzinfo=UTC)
    is_stale_processing = (
        record.processing_status == PROCESSING_STATUS_PROCESSING
        and updated is not None
        and updated < datetime.now(UTC) - stale_after
    )
    reprocessable = record.processing_status in (
        PROCESSING_STATUS_PENDING,
        PROCESSING_STATUS_FAILED,
    )
    if not (reprocessable or is_stale_processing):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"El archivo no puede reprocesarse (estado actual: {record.processing_status}).",
        )

    record.processing_status = PROCESSING_STATUS_PENDING
    record.parsed_summary_json = None
    await repo.save(record)

    if get_settings().USE_LOCAL_FALLBACK:
        await _process_file_sync(record, session)
    else:
        job = _pick_job(record.content_type)
        try:
            job.delay(str(record.id), str(tenant.tenant_id))
        except Exception:
            await _process_file_sync(record, session)

    return {"file_id": str(file_id), "status": "requeued"}


@router.get(
    "/field-catalog",
    response_model=dict[str, EntityFieldCatalog],
    summary="Campos canónicos, requeridos y escalares por entidad",
)
async def get_field_catalog(
    tenant: Tenant = Depends(get_current_tenant),
) -> dict[str, EntityFieldCatalog]:
    """Fuente ÚNICA de qué campos existen, cuáles son obligatorios y cuáles
    admiten una sola columna.

    Existe porque el frontend mantenía una copia manual de ``CANONICAL_FIELDS``
    ("mantener en sync") y divergió: a ``expense`` le faltaban ``payment_method``
    e ``is_recurring``. Como el ``<select>`` del panel solo renderiza opciones de
    esa copia, cuando el backend sugería ``payment_method`` ninguna ``<option>``
    matcheaba, el DOM caía a la primera y la pantalla mostraba "Sin mapear"
    mientras el estado enviaba ``payment_method``. La UI mostraba una cosa y
    mandaba otra.

    Derivado en vivo de las mismas estructuras que usan la validación del confirm
    y el importador — no hay una segunda lista que pueda quedar desfasada.

    Estático por deploy (no depende del tenant ni del archivo); el auth se pide
    igual porque el catálogo describe la forma de los datos de negocio.
    """

    def _condicion(entity: str, field: str) -> ConditionalRequirement | None:
        """F-C.c3b: la regla contextual del campo, si tiene una.

        Se sirve pero NO se aplica: `required` sigue igual y el confirm sigue
        validando con `missing_required_fields`. La pantalla puede explicar
        "el producto sólo hace falta si la hoja mueve unidades" sin que el
        importador rechace la planilla de honorarios que no lo trae.
        """
        regla = conditional_requirement(entity, field)
        if regla is None:
            return None
        return ConditionalRequirement(
            condition=regla.condition,
            explanation=regla.explanation,
            # `signals` son frozensets: sin ordenar, el JSON cambia de orden entre
            # requests y la misma regla se lee como si fuera otra.
            signals=[sorted(grupo) for grupo in regla.signals],
        )

    return {
        entity: EntityFieldCatalog(
            required=list(REQUIRED_FIELDS.get(entity, [])),
            required_alternatives={
                campo: sorted(alternativa)
                for campo, alternativa in REQUIRED_ALTERNATIVES.get(entity, {}).items()
            },
            fields=[
                FieldCatalogEntry(
                    value=value,
                    label=label,
                    single_value=value in SINGLE_VALUE_FIELDS.get(entity, frozenset()),
                    required_reason=required_reason(entity, value),
                    required_when=_condicion(entity, value),
                )
                for value, label in fields.items()
            ],
        )
        for entity, fields in CANONICAL_FIELDS.items()
    }


@router.get(
    "/files/{file_id}/column-mappings",
    response_model=list[ColumnMappingSuggestion],
    summary="Get column mapping suggestions for a file",
)
async def get_column_mappings(
    file_id: uuid.UUID,
    entity_type: str | None = Query(
        default=None,
        description="Override explícito de la entidad: sale | expense | product | "
        "customer | supplier. Si se manda, GANA sobre la entidad del contexto — es "
        "la entidad que el usuario eligió en el selector de sección. Si se omite, "
        "se usa la del contexto (o 'sale' en archivos planos).",
    ),
    context_id: str | None = Query(
        default=None,
        description="Contexto (hoja/tabla) en archivos multi-contexto. Si se da, "
        "se usan sus headers/preview y, salvo override, su entity_type.",
    ),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[ColumnMappingSuggestion]:
    """Sugerencias de mapeo para las columnas de un archivo (o de una de sus hojas).

    La entidad efectiva se resuelve con la MISMA prioridad que la inserción real
    (ver ``derive_context_mapping_entries`` y el confirm): **override del usuario →
    entidad original del ``mapping_contexts`` → default ``"sale"``**.

    El override es obligatorio acá porque el frontend renderiza los targets contra
    el catálogo de la entidad que el usuario eligió en el selector de sección.
    Mientras este endpoint invertía la prioridad (la entidad del summary le ganaba
    al param), devolvía sugerencias de la entidad ORIGINAL: el ``<select>`` no tenía
    esas opciones, la pantalla mostraba "(campo desconocido)" y los requeridos de la
    entidad elegida quedaban sin cubrir → 422 al confirmar.
    """
    # F7d: "customer"/"supplier" sumados — sin esto, un archivo flat (legacy, sin
    # mapping_contexts) de clientes/proveedores no podía pedir sugerencias de
    # mapeo (context_id resuelve el entity_type real igual, pero el query param
    # por default "sale" ya rebotaba acá antes de llegar a esa resolución).
    # `None` = "no lo mandó" (se cae a la entidad del contexto), distinto de
    # mandar "sale" explícitamente, que sí es un override.
    if entity_type is not None and entity_type not in (
        "sale",
        "expense",
        "product",
        "inventory",
        "customer",
        "supplier",
    ):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "entity_type debe ser: sale, expense, product, inventory, customer o "
                "supplier."
            ),
        )

    repo = FileRepository(session)
    record = await repo.get_by_id(file_id, tenant.tenant_id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Archivo no encontrado.")

    summary = record.parsed_summary_json or {}

    # Resolver headers/sample_rows/entity_type por contexto si se pidió uno.
    resolved_entity = entity_type or "sale"
    if context_id:
        ctx = next(
            (
                c
                for c in summary.get("mapping_contexts", [])
                if c.get("context_id") == context_id
            ),
            None,
        )
        if ctx is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Contexto '{context_id}' no encontrado en el archivo.",
            )
        # Sin headers (texto/imagen): no hay columnas que mapear.
        if not ctx.get("headers"):
            return []
        headers = ctx["headers"]
        sample_rows = ctx.get("preview_rows") or []
        # Override del usuario primero: es la entidad EFECTIVA de la sección, la
        # misma que el frontend usa para renderizar los targets y la misma que el
        # confirm usa para insertar.
        resolved_entity = entity_type or ctx.get("entity_type") or "sale"
    else:
        headers = summary.get("headers", [])
        sample_rows = (
            summary.get("preview_rows")
            or summary.get("ventas_detectadas")
            or summary.get("gastos_detectados")
            or summary.get("stock_detectado")
            or []
        )

    svc = ColumnMappingService(session)
    raw_suggestions = await svc.suggest_mappings(
        tenant.tenant_id,
        resolved_entity,
        headers,
        sample_rows,
        # FASE 2 (A2): traza la decisión de la 4ª capa LLM en pipeline_events.
        trace_id=record.trace_id or record.id,
        file_id=record.id,
    )
    return [
        ColumnMappingSuggestion(**{**s, "context_id": context_id}) for s in raw_suggestions
    ]


@router.get(
    "/column-mappings",
    response_model=list[TenantColumnMappingResponse],
    summary="List all learned column mappings for this tenant",
)
async def list_column_mappings(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[TenantColumnMappingResponse]:
    svc = ColumnMappingService(session)
    mappings = await svc.get_learned_mappings(tenant.tenant_id)
    return [
        TenantColumnMappingResponse(
            id=m.id,
            entity_type=m.entity_type,
            source_column=m.source_column,
            target_field=m.target_field,
            confirmed_count=m.confirmed_count,
            last_seen_at=m.last_seen_at,
        )
        for m in mappings
    ]


@router.delete(
    "/column-mappings/{mapping_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a learned column mapping",
)
async def delete_column_mapping(
    mapping_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    svc = ColumnMappingService(session)
    found = await svc.delete_mapping(tenant.tenant_id, mapping_id)
    if not found:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Mapeo no encontrado."
        )
    await session.commit()


def _sanitize_error_message(exc: BaseException) -> str:
    """Mensaje de error apto para persistir en la traza: sin valores de fila.

    ``str()`` de un error de SQLAlchemy trae el SQL completo más
    ``[parameters: (...)]``, y el de un ``UniqueViolationError`` de Postgres
    agrega ``DETAIL: Key (tenant_id, sku_normalized)=(<uuid>, ABC-123) already
    exists``. Los dos filtran datos del negocio a una tabla append-only que
    después se lee desde un endpoint admin — exactamente lo que el invariante de
    no-PII prohíbe.

    Lo que se conserva es lo único que sirve para diagnosticar: la PRIMERA línea
    del error del driver, que en Postgres es la violación y el NOMBRE de la
    constraint (``duplicate key value violates unique constraint
    "uq_products_tenant_sku_norm"``). El ``DETAIL`` con los valores se descarta.
    """
    from sqlalchemy.exc import DBAPIError, SQLAlchemyError  # noqa: PLC0415

    if isinstance(exc, DBAPIError) and exc.orig is not None:
        # `exc.orig` es la excepción del driver: su primera línea es el mensaje
        # de Postgres sin el DETAIL. `str(exc)` sí incluiría statement y params.
        return str(exc.orig).split("\n")[0].strip()[:500]
    if isinstance(exc, SQLAlchemyError):
        # Otros errores del ORM: nunca el str completo (puede traer el statement).
        return type(exc).__name__
    return str(exc)[:500]


@router.post(
    "/files/{file_id}/confirm",
    response_model=ConfirmIngestionResponse,
    summary="Confirm ingestion of parsed data",
    dependencies=[Depends(ensure_tenant_not_under_maintenance)],
)
async def confirm_file(
    file_id: uuid.UUID,
    body: ConfirmIngestionRequest,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> ConfirmIngestionResponse:
    # F-T: el reloj arranca acá, no en el import. `latency_ms` medía sólo
    # `insert_confirmed_data`, así que un confirm que tarda 30 s en validar y 1 s
    # en insertar se reportaba como "1 s" — y la persona que esperó los 31
    # tenía razón. Los checkpoints se cierran con `mark()` para no re-indentar
    # las ~800 líneas de validación.
    _timings = StageTimings()
    repo = FileRepository(session)
    record = await repo.get_by_id(file_id, tenant.tenant_id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Archivo no encontrado.")

    # Pre-check barato: sólo NEEDS_CONFIRMATION (confirm normal) e IMPORTING
    # (candidato a takeover si el lease quedó stale) llegan al CAS del lease.
    # DONE/PROCESSING/etc. se rechazan acá con un mensaje claro. El guard real
    # de concurrencia es el CAS atómico de `acquire_import_lease`.
    if record.processing_status not in (
        PROCESSING_STATUS_NEEDS_CONFIRMATION,
        PROCESSING_STATUS_IMPORTING,
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"El archivo no está pendiente de confirmación "
                f"(estado actual: {record.processing_status})."
            ),
        )

    # Derivar entity_type desde el summary para validación y aprendizaje
    _inferred_type = (record.parsed_summary_json or {}).get("inferred_type", "general")
    # F7d: "clientes"/"proveedores" sumados — sin esto, un archivo flat (legacy,
    # sin mapping_contexts) de SOLO maestros validaba contra REQUIRED_FIELDS["sale"]
    # (el default de abajo) en vez de REQUIRED_FIELDS["customer"/"supplier"].
    _entity_map = {
        "ventas": "sale",
        "gastos": "expense",
        "stock": "product",
        "clientes": "customer",
        "proveedores": "supplier",
    }
    _entity_type = _entity_map.get(_inferred_type, "sale")

    # ── Separar mapeos planos (legacy single-context) de los cualificados por contexto ──
    _summary_for_ctx = record.parsed_summary_json or {}
    _context_entity: dict[str, str] = {
        ctx["context_id"]: ctx["entity_type"]
        for ctx in _summary_for_ctx.get("mapping_contexts", [])
        if ctx.get("context_id") and ctx.get("entity_type")
    }
    _flat_mappings = [m for m in body.column_mappings if m.context_id is None]
    _ctx_mappings = [m for m in body.column_mappings if m.context_id is not None]

    # ¿El importador va a tomar el camino de UNA sola tabla?
    #
    # Es la negación EXACTA del despacho de `insert_confirmed_data`
    # (`if inferred_type == "mixed" or summary.get("multi_sheet")` →
    # `_insert_multisheet_data`), y por eso vale como respuesta a la pregunta que
    # importa acá: el cobro del envío (`_cobrar_envios_de_la_hoja`) es un closure
    # anidado dentro del camino multi-hoja, así que **cualquier otro camino no
    # cobra envío**. Estaba calculado adentro del gate de replay; subió de scope
    # porque ahora lo consultan dos guards y una segunda copia podría divergir.
    _plano = _inferred_type != "mixed" and not _summary_for_ctx.get("multi_sheet")

    # Etiqueta legible de una hoja para los mensajes de error. `context_id` es un
    # identificador interno ("sheet:precios y stock ") — mostrárselo al usuario,
    # con su espacio final incluido, no lo ayuda a encontrar la hoja.
    _ctx_label: dict[str, str] = {
        ctx["context_id"]: str(ctx.get("label") or ctx["context_id"]).strip()
        for ctx in _summary_for_ctx.get("mapping_contexts", [])
        if ctx.get("context_id")
    }

    def _hoja(context_id: str) -> str:
        return _ctx_label.get(context_id, context_id).strip()

    # Mapeos agrupados por hoja — los usan la validación por contexto y el
    # snapshot que se traza al confirmar.
    _mappings_por_contexto: dict[str, list[ColumnMapping]] = defaultdict(list)
    for _m in _ctx_mappings:
        _mappings_por_contexto[_m.context_id or ""].append(_m)

    def _campo(entity_type: str, field: str) -> str:
        """Etiqueta en castellano del campo, no su nombre técnico."""
        return CANONICAL_FIELDS.get(entity_type, {}).get(field, field)

    def _motivos(entity_type: str, faltantes: list[str]) -> dict[str, str]:
        """Por qué el importador necesita cada faltante, para la traza.

        Va a ``pipeline_events`` junto al 422 para que el operador que diagnostica
        después lea EXACTAMENTE lo que leyó la persona. Con sólo los nombres
        técnicos, reconstruir qué decía la pantalla exigía saber de memoria qué
        texto servía el deploy de ese día.
        """
        return {
            campo: motivo
            for campo in faltantes
            if (motivo := required_reason(entity_type, campo))
        }

    def _detalle_faltantes(entity_type: str, faltantes: list[str]) -> str:
        """«Monto de venta. Véktor necesita saber cuánta plata entró…».

        La etiqueta sola dice QUÉ falta y nada más; quien no mapeó el monto no
        sabe si su planilla no entra, si entra incompleta o si entra distinta — y
        son tres destinos distintos según el campo (a «Otros» rescatable, o
        descartada sin rastro). El motivo es lo único que le permite decidir si
        arregla la planilla o sigue.

        Mismo texto que sirve el catálogo (`REQUIRED_REASONS`): el banner de la
        pantalla y el rechazo del backend no pueden explicar cosas distintas
        sobre el mismo campo.
        """
        partes = []
        for campo in faltantes:
            etiqueta = _campo(entity_type, campo)
            motivo = required_reason(entity_type, campo)
            partes.append(f"{etiqueta}. {motivo}" if motivo else f"{etiqueta}.")
        return " ".join(partes)

    # El trace_id se resuelve ACÁ, antes del primer guard: los rechazos de
    # validación ocurren ANTES del lease (a propósito — una request que va a
    # rebotar nunca lo toma) y hasta ahora no dejaban NI UNA fila en
    # pipeline_events. Diagnosticar los tres 422 de ASTERIA exigió reconstruir el
    # caso a mano porque no había traza de ninguno.
    _trace_id = record.trace_id or record.id
    bind_request_context(trace_id=_trace_id)

    async def _emit_validation_reject(motivo: str, detalle: dict[str, Any]) -> None:
        """Traza un rechazo PREVIO al lease.

        Es seguro emitir acá y no contradice la nota de ``_emit_confirm_failure``:
        esa advertencia ("primero compensar el lease, después trazar") existe
        porque ``emit_event`` abre un ``begin_nested()`` que flushea
        incondicionalmente, y sobre una sesión que viene de un import reventado
        ese flush la deja abortada. En este camino la sesión está limpia y no hay
        lease que compensar.

        Best-effort de punta a punta: la traza nunca puede tapar el 422.
        Sin PII: motivo, campos y nombres de columna — nunca valores de fila.
        """
        try:
            await pipeline_event_service.emit_event(
                session,
                trace_id=_trace_id,
                tenant_id=tenant.tenant_id,
                stage=STAGE_REJECT,
                file_id=file_id,
                detail={
                    "stage_failed": "confirm",
                    "phase": "validation",
                    "http_status": status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "motivo": motivo,
                    "confirmed_fields": body.confirmed_fields,
                    # F-T: cuánto costó llegar al rechazo. Un 422 también hace
                    # esperar, y con nueve hojas la validación no es gratis: sin
                    # esto, "rebotó" y "rebotó después de veinte segundos" se leen
                    # igual en la traza.
                    "timings_ms": _timings.as_detail(),
                    **detalle,
                },
            )
            # Commit propio: el 422 sigue viaje y haría rollback del request,
            # llevándose el evento.
            await session.commit()
        except Exception:  # noqa: BLE001 — la traza nunca tapa el rechazo
            logger.warning(
                "ingestion.confirm.validation_trace_failed",
                file_id=str(file_id),
                motivo=motivo,
            )

    # Override del usuario para reasignar la entidad de un contexto completo
    # (ej. una hoja "general"/producto pasada a venta/gasto). Fuente única con
    # el gate F6-A1 (más abajo) y con `_insert_multisheet_data`, que también lo
    # consulta — sin esto, `_entity_for` quedaba desalineada: validaba
    # requeridos/decisiones de riesgo/aprendizaje bajo la entidad ORIGINAL del
    # summary mientras el import real ya usaba la reasignada (bug de review).
    _override = body.context_entity or {}

    def _entity_for(mapping: ColumnMapping) -> str:
        # Con context_id, el entity_type se deriva del contexto (autoritativo,
        # igual que la inserción en _insert_multisheet_data): override del
        # usuario primero, después el original del summary, después el del
        # payload como último fallback.
        if mapping.context_id:
            return (
                _override.get(mapping.context_id)
                or _context_entity.get(mapping.context_id)
                or mapping.entity_type
                or _entity_type
            )
        return mapping.entity_type or _entity_type

    # `entity_type` opcional: una hoja que el parser no pudo clasificar llega sin
    # entidad y el guard de más abajo igual necesita saber si el usuario la
    # incluyó. Misma firma que `context_is_included`, que ya la acepta nullable.
    def _context_included(context_id: str, entity_type: str | None) -> bool:
        # Fuente única compartida con F8 (`/column-risk`) para no divergir.
        return context_is_included(
            context_id, entity_type, body.confirmed_fields, body.context_confirmed
        )

    def _missing_required(entity_type: str, mappings: list[ColumnMapping]) -> set[str]:
        mapped = {
            m.target_field
            for m in mappings
            if parse_target(m.target_field).kind == "canonical"
        }
        # F-H4: un requerido puede estar cubierto por una alternativa completa
        # (`amount` por `unit_price` + `quantity`). La regla vive en
        # `column_mapping_service` y la sirve el catálogo, para que la pantalla
        # no pueda decir que falta algo que el confirm acepta, ni al revés.
        return missing_required_fields(entity_type, mapped)

    # Columnas que las decisiones de riesgo (F8) van a ELIMINAR del mapeo
    # efectivo. La colisión se evalúa sobre lo que va a quedar, no sobre lo que
    # se mandó: dos columnas al mismo target donde una se dropea NO es una
    # colisión (caso legítimo `fecha` + `fecha_alt`). Misma convención de clave
    # que `_dropped_pairs`, que se computa más abajo dentro del import.
    _dropped_by_risk: set[tuple[str, str]] = {
        (d.context_id, d.source_column)
        for d in (body.column_risk_decisions or [])
        if d.action == "drop_column"
    }

    def _colliding_scalars(
        entity_type: str, mappings: list[ColumnMapping]
    ) -> dict[str, list[str]]:
        """Campos de valor único con MÁS DE UNA columna apuntándoles.

        Sin este chequeo, ``_resolve_target_cols`` del importador se quedaba con
        la primera columna del orden del archivo y descartaba el resto en
        silencio: el valor que terminaba guardado dependía de cómo estaban
        ordenadas las columnas del Excel. Elegir un dato de negocio por un
        detalle de implementación es inventarlo (incidente ASTERIA: "Precio de
        compra", "Precio de lista" y "Precio de venta final" caían las tres en
        ``sale_price_ars``).

        Solo aplica a los campos donde una colisión corrompe plata
        (``SINGLE_VALUE_FIELDS``); los demás admiten varias columnas.
        """
        scalars = SINGLE_VALUE_FIELDS.get(entity_type, frozenset())
        by_target: dict[str, list[str]] = defaultdict(list)
        for m in mappings:
            if m.target_field not in scalars:
                continue
            if (m.context_id or "table", m.source_column) in _dropped_by_risk:
                continue  # el usuario ya decidió sacarla: no compite por el campo
            by_target[m.target_field].append(m.source_column)
        return {t: cols for t, cols in by_target.items() if len(cols) > 1}

    def _colliding_custom_fields(mappings: list[ColumnMapping]) -> dict[str, list[str]]:
        """Campos PROPIOS con más de una columna apuntándoles.

        Hermano de ``_colliding_scalars``, para la otra rama del mapeo. Un campo
        propio guarda un valor por fila, así que dos columnas al mismo destino
        tienen exactamente el mismo problema que dos columnas a un escalar
        canónico: sólo una sobrevive. El importador ahora se queda con la
        primera (first-wins, igual que la rama canónica), pero elegir por orden
        del archivo sigue siendo elegir un dato por un detalle de
        implementación — así que se le pregunta al usuario.

        No depende de la entidad: un campo propio es del tenant, no del catálogo
        canónico de la sección.
        """
        by_key: dict[str, list[str]] = defaultdict(list)
        for m in mappings:
            parsed = parse_target(m.target_field)
            if parsed.kind != "custom":
                continue
            if (m.context_id or "table", m.source_column) in _dropped_by_risk:
                continue  # el usuario ya decidió sacarla: no compite por el campo
            by_key[parsed.field].append(m.source_column)
        return {k: cols for k, cols in by_key.items() if len(cols) > 1}

    def _custom_collision_detail(colisiones: dict[str, list[str]]) -> str:
        partes = [
            f"«{key}» ← {', '.join(cols)}" for key, cols in sorted(colisiones.items())
        ]
        return (
            "Hay más de una columna guardándose con el mismo nombre de campo "
            f"propio, y solo se puede guardar una: {'; '.join(partes)}. Cambiale "
            "el nombre a una, o mandala a «Ignorar»."
        )

    def _collision_detail(entity_type: str, colisiones: dict[str, list[str]]) -> str:
        etiquetas = CANONICAL_FIELDS.get(entity_type, {})
        partes = [
            f"«{etiquetas.get(target, target)}» ← {', '.join(cols)}"
            for target, cols in sorted(colisiones.items())
        ]
        return (
            "Hay más de una columna apuntando al mismo campo, y solo se puede "
            f"guardar una: {'; '.join(partes)}. Elegí cuál corresponde y mandá "
            "las demás a otro campo o a «Ignorar»."
        )

    # ── Ninguna hoja se importa sin que alguien haya dicho QUÉ es ───────────────
    # El parser deja `entity_type: null` cuando no pudo clasificar una hoja.
    #
    # ALCANCE REAL de este guard (no sobreestimarlo): el importador ya rutea a
    # "Otros" un contexto cuya entidad no resuelve (`_insert_multisheet_data`,
    # `if entity not in entity_bucket`), así que el default "sale" de
    # `_entity_for` NO era lo que convertía esas filas en ventas — gobierna la
    # validación de requeridos y el aprendizaje de mapeos. Lo que las convertía
    # en ventas era el FRONTEND, que mandaba `context_entity` con "sale" por su
    # propio default. Ese es el fix principal y vive en el panel.
    #
    # Esto es defensa en profundidad para la otra forma del problema: un cliente
    # que incluye una hoja SIN declarar su sección. Corta con 422 en vez de
    # dejarla caer silenciosamente a "Otros". NO protege contra un cliente que
    # manda una sección explícita equivocada.
    #
    # Va antes del lease: una request que va a rebotar nunca lo toma.
    if _mapping_contexts_raw := (_summary_for_ctx.get("mapping_contexts") or []):
        _sin_entidad: list[str] = []
        for _ctx in _mapping_contexts_raw:
            _cid = _ctx.get("context_id")
            if not _cid:
                continue
            # MISMA prioridad que `_entity_for`: override del usuario primero,
            # después la entidad original del summary. Si ninguna resuelve, la
            # hoja no tiene sección y no puede importarse.
            _ent_efectiva = _override.get(_cid) or _context_entity.get(_cid)
            if _ent_efectiva:
                continue
            if _context_included(_cid, _ent_efectiva):
                _sin_entidad.append(str(_ctx.get("label") or _cid))
        if _sin_entidad:
            await _emit_validation_reject(
                "hoja_sin_seccion", {"hojas": _sin_entidad}
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "Estas hojas no tienen sección asignada y no se pueden "
                    f"importar: {', '.join(_sin_entidad)}. Elegí a qué sección va "
                    "cada una (ventas, gastos o productos) o destildala para "
                    "dejarla afuera."
                ),
            )

    # Validación de requeridos — plano (legacy)
    if _flat_mappings:
        confirmed_entity = (
            (body.confirmed_fields.get("ventas") and _entity_type == "sale")
            or (body.confirmed_fields.get("gastos") and _entity_type == "expense")
            or (body.confirmed_fields.get("productos") and _entity_type == "product")
            or (body.confirmed_fields.get("clientes") and _entity_type == "customer")
            or (body.confirmed_fields.get("proveedores") and _entity_type == "supplier")
        )
        if confirmed_entity:
            missing = _missing_required(_entity_type, _flat_mappings)
            if missing:
                _faltantes = sorted(missing)
                await _emit_validation_reject(
                    "requeridos_sin_mapear",
                    {
                        "entity_type": _entity_type,
                        "faltantes": _faltantes,
                        "motivos": _motivos(_entity_type, _faltantes),
                    },
                )
                _encabezado = (
                    "Falta un dato obligatorio"
                    if len(_faltantes) == 1
                    else "Faltan datos obligatorios"
                )
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"{_encabezado}: "
                        f"{_detalle_faltantes(_entity_type, _faltantes)} "
                        "Elegí ese campo en la columna que lo contiene. Un campo "
                        "personalizado guarda el dato pero no reemplaza al "
                        "obligatorio."
                    ),
                )
            if _colisiones := _colliding_scalars(_entity_type, _flat_mappings):
                await _emit_validation_reject(
                    "colision_campo_escalar",
                    {"entity_type": _entity_type, "colisiones": _colisiones},
                )
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=_collision_detail(_entity_type, _colisiones),
                )
            if _cf_colisiones := _colliding_custom_fields(_flat_mappings):
                await _emit_validation_reject(
                    "colision_campo_propio",
                    {"entity_type": _entity_type, "colisiones": _cf_colisiones},
                )
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=_custom_collision_detail(_cf_colisiones),
                )

    # Validación de requeridos — por contexto (multi-hoja), solo contextos incluidos
    if _ctx_mappings:
        for _cid, _ms in _mappings_por_contexto.items():
            _ent = _entity_for(_ms[0])
            if _context_included(_cid, _ent):
                missing = _missing_required(_ent, _ms)
                if missing:
                    _faltantes = sorted(missing)
                    await _emit_validation_reject(
                        "requeridos_sin_mapear",
                        {
                            "context_id": _cid,
                            "entity_type": _ent,
                            "faltantes": _faltantes,
                            "motivos": _motivos(_ent, _faltantes),
                        },
                    )
                    _encabezado = (
                        "falta un dato obligatorio"
                        if len(_faltantes) == 1
                        else "faltan datos obligatorios"
                    )
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=(
                            f"En la hoja «{_hoja(_cid)}» {_encabezado}: "
                            f"{_detalle_faltantes(_ent, _faltantes)} "
                            "Elegí ese campo en la columna que lo contiene. Un campo "
                            "personalizado guarda el dato pero no reemplaza al "
                            "obligatorio."
                        ),
                    )
                if _colisiones := _colliding_scalars(_ent, _ms):
                    await _emit_validation_reject(
                        "colision_campo_escalar",
                        {
                            "context_id": _cid,
                            "entity_type": _ent,
                            "colisiones": _colisiones,
                        },
                    )
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=(
                            f"En la hoja «{_hoja(_cid)}»: "
                            f"{_collision_detail(_ent, _colisiones)}"
                        ),
                    )
                if _cf_colisiones := _colliding_custom_fields(_ms):
                    await _emit_validation_reject(
                        "colision_campo_propio",
                        {
                            "context_id": _cid,
                            "entity_type": _ent,
                            "colisiones": _cf_colisiones,
                        },
                    )
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=(
                            f"En la hoja «{_hoja(_cid)}»: "
                            f"{_custom_collision_detail(_cf_colisiones)}"
                        ),
                    )

    # ── F-H3.a: efecto de inventario por hoja ───────────────────────────────────
    # Se resuelve ANTES del lease, con el mapeo ya validado, por la misma razón que
    # el resto de las validaciones de esta zona: un rechazo acá no deja nada a medio
    # importar. El default NUNCA es `historical_replay` — ver domain/inventory_effect.
    _inventory_effects: dict[str, str] = {}
    if _ctx_mappings:
        _perfiles = [
            SheetInventoryProfile(
                context_id=_cid,
                entity=_entity_for(_ms[0]),
                mapped_fields=frozenset(
                    m.target_field
                    for m in _ms
                    if parse_target(m.target_field).kind == "canonical"
                ),
            )
            for _cid, _ms in _mappings_por_contexto.items()
        ]
        try:
            _inventory_effects = resolve_inventory_effects(_perfiles, body.inventory_effect)
        except InvalidInventoryEffectError as exc:
            await _emit_validation_reject(
                "efecto_de_inventario_invalido",
                {"inventory_effect": body.inventory_effect, "motivo": str(exc)},
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
    elif body.inventory_effect:
        # Mapeos planos (sin `context_id`): no hay hojas contra las cuales resolver
        # el efecto, así que el `inventory_effect` que mandó el cliente no se puede
        # honrar. Antes se descartaba en silencio y el import salía con el default:
        # el usuario elegía reconstruir su inventario y no pasaba nada, sin error ni
        # aviso. Misma regla que `resolve_inventory_effects`.
        _detalle_plano = (
            "El efecto de inventario se declara por hoja, y este envío manda las "
            "columnas sin identificar a qué hoja pertenecen. Volvé a mapear las "
            "columnas para que cada una quede asociada a su hoja."
        )
        await _emit_validation_reject(
            "efecto_de_inventario_sin_hoja",
            {"inventory_effect": body.inventory_effect},
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_detalle_plano,
        )

    # ── Un archivo de UNA sola tabla no puede traer costos de compra ────────────
    # El camino plano del importador NO cobra el envío ni aplica las decisiones de
    # costo, y no lo hace de tres maneras a la vez:
    #   1. `_cobrar_envios_de_la_hoja` es un closure anidado dentro del camino
    #      multi-hoja: desde el plano es estructuralmente inalcanzable;
    #   2. el plano llama al planificador con `ctx_id=None` —que busca la decisión
    #      bajo la clave `""`— mientras la API la manda con el `context_id` real,
    #      así que la decisión se valida, el usuario la ve aceptada y el import la
    #      ignora;
    #   3. los avisos de costo nunca llegan a `counts`, así que tampoco hay rastro.
    #
    # Arreglar el camino plano de verdad es otra fase. Lo que NO se puede hacer
    # mientras tanto es aceptar el archivo: importar una compra sin cobrarle el
    # envío que el usuario mapeó deja un costo más bajo que el real, y con él un
    # margen inflado que nadie va a salir a buscar. Se rechaza y se dice la salida.
    #
    # **No está gateado por tenant**: no cobrar un envío mapeado es incorrecto con
    # el motor de costos prendido o apagado. La compuerta gobierna el reparto, no
    # el silencio.
    if _plano:
        _targets_planos = {m.target_field for m in _flat_mappings} | {
            m.target_field for m in _ctx_mappings
        }
        _columnas_de_costo = sorted(
            _targets_planos & {"shipping_cost", "shipping_cost_line"}
        )
        if _columnas_de_costo or body.purchase_cost_decisions:
            _que_pasa = (
                "tiene columnas de envío mapeadas"
                if _columnas_de_costo
                else "trae decisiones sobre el costo de compra"
            )
            await _emit_validation_reject(
                "costos_de_compra_en_archivo_plano",
                {
                    "columnas": _columnas_de_costo,
                    "decisiones": bool(body.purchase_cost_decisions),
                },
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"«{record.original_filename}» es un archivo de una sola tabla "
                    f"y {_que_pasa}. Véktor todavía no sabe repartir ni cobrar el "
                    "envío en este formato: si lo importara, la compra quedaría con "
                    "un costo más bajo que el real y el margen inflado. Subilo como "
                    "libro con hojas separadas (una por sección), o sacá las columnas "
                    "de envío del mapeo y cargá ese costo como un gasto aparte."
                ),
            )

    # ── F-H6.b: la decisión sobre envíos sin comprobante apunta a una hoja real ──
    # Mismo criterio que el efecto de inventario: una decisión que no se puede
    # honrar no se ignora en silencio, porque significa que el usuario cree haber
    # resuelto algo sobre sus costos que no va a pasar. Va antes del lease.
    if body.shipping_decisions:
        _hojas_con_envio = {
            _cid
            for _cid, _ms in _mappings_por_contexto.items()
            if any(m.target_field == "shipping_cost" for m in _ms)
        }
        for _dec in body.shipping_decisions:
            if _dec.context_id not in _hojas_con_envio:
                await _emit_validation_reject(
                    "decision_de_envio_sin_columna",
                    {"context_id": _dec.context_id, "action": _dec.action},
                )
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"La decisión sobre el envío apunta a la hoja "
                        f"«{_hoja(_dec.context_id)}», que no tiene ninguna columna "
                        "mapeada como envío."
                    ),
                )

    # ── F-H6.c: la decisión sobre el costo se puede honrar ──────────────────────
    # Mismo criterio que la de envíos: una decisión que no se puede cumplir no se
    # ignora en silencio, porque el usuario cree haber resuelto algo sobre sus
    # costos que no va a pasar. Va antes del lease — un archivo que va a rebotar
    # no debería haberlo tomado. La validación es pura y vive en el dominio, así
    # que el confirm no reimplementa qué combinación es imposible.
    if body.purchase_cost_decisions and not purchase_cost_enabled_for(tenant.tenant_id):
        # Segundo punto de control de la compuerta, y el que de verdad protege los
        # números: sin esto, un cliente que arma el body a mano —o una pantalla
        # vieja cacheada— movería el costo de los productos de un tenant que no
        # tiene el motor habilitado. Ocultar el control en el frontend no alcanza.
        #
        # 422 y no un descarte silencioso: el usuario cree haber resuelto algo
        # sobre sus costos, y dejarlo confirmar como si nada le daría un import
        # que no hizo lo que pidió (mismo criterio que la decisión de envío que
        # no se puede honrar). Va pre-lease, con traza.
        await _emit_validation_reject(
            "motor_de_costos_no_habilitado",
            {"contextos": sorted({d.context_id for d in body.purchase_cost_decisions})},
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_MOTOR_DE_COSTOS_DESHABILITADO,
        )

    if body.purchase_cost_decisions:
        _errores_de_costo = validate_purchase_cost_decisions(
            [
                CostDecision(
                    context_id=_d.context_id,
                    base=_d.base,
                    shared_shipping=_d.shared_shipping,
                    line_shipping=_d.line_shipping,
                )
                for _d in body.purchase_cost_decisions
            ],
            {
                _cid: {m.source_column: m.target_field for m in _ms}
                for _cid, _ms in _mappings_por_contexto.items()
            },
            {_cid: _hoja(_cid) for _cid in _mappings_por_contexto},
        )
        if _errores_de_costo:
            _motivo, _texto = _errores_de_costo[0]
            await _emit_validation_reject(
                _motivo,
                {"errores": [m for m, _ in _errores_de_costo]},
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=" ".join(texto for _m, texto in _errores_de_costo),
            )

    # ── F6-A1: bloqueo por fecha faltante, ANTES del lease ──────────────────────
    # Una venta/gasto sin columna de fecha resoluble caía al fallback "hoy" (dato
    # inventado — invariante 2d). Se rechaza upfront, no por fila: sin este gate un
    # archivo de miles de filas sin columna de fecha genera miles de registros en
    # /otros que el bulk import no puede resolver. Solo aplica a SPREADSHEETS: los
    # documentos de texto/imagen no tienen columnas y se rutean a /otros durante el
    # import (A4). Fuente única de "hay fecha o no": resolve_transaction_date_column
    # (la misma del importador), nunca `_FECHA_COLS` privado.
    if _summary_for_ctx.get("file_type", "spreadsheet") == "spreadsheet":
        _date_check: list[tuple[str, list[str] | None, dict[str, str]]] = []
        _mapping_ctxs = _summary_for_ctx.get("mapping_contexts") or []
        if _mapping_ctxs:
            _ctx_map_by_cid: dict[str, dict[str, str]] = defaultdict(dict)
            for _m in _ctx_mappings:
                if _m.target_field != "ignore" and _m.context_id:
                    _ctx_map_by_cid[_m.context_id][_m.source_column] = _m.target_field
            for _ctx in _mapping_ctxs:
                _cid = _ctx.get("context_id")
                if not _cid:
                    continue
                # Entidad EFECTIVA: el usuario puede reasignar una hoja general/
                # producto a venta/gasto (context_entity) y el importador la procesa
                # como tal (misma resolución que _insert_multisheet_data:3634). Sin
                # mirar la entidad efectiva, esa hoja sin fecha escaparía el gate,
                # tomaría el lease y volcaría todo a /otros — se rompería el contrato
                # "422 antes del lease".
                _ent = _override.get(_cid) or _ctx.get("entity_type")
                if _ent not in ("sale", "expense"):
                    continue
                if _context_included(_cid, _ent):
                    _label = str(_ctx.get("label") or _cid)
                    _date_check.append(
                        (_label, _ctx.get("headers"), _ctx_map_by_cid.get(_cid, {}))
                    )
        else:
            # Legacy: summary sin mapping_contexts (single-context por keyword). El
            # importador deriva los headers de `rows[0].keys()` (no de un `headers`
            # top-level, que este path ni siquiera setea), así que el gate hace lo
            # mismo para no divergir del importador (el objetivo de C1).
            _flat_confirmed = bool(
                (body.confirmed_fields.get("ventas") and _entity_type == "sale")
                or (body.confirmed_fields.get("gastos") and _entity_type == "expense")
            )
            if _flat_confirmed:
                _flat_map = {
                    m.source_column: m.target_field
                    for m in _flat_mappings
                    if m.target_field != "ignore"
                }
                _legacy_rows = (
                    _summary_for_ctx.get("ventas_detectadas")
                    or _summary_for_ctx.get("gastos_detectados")
                    or _summary_for_ctx.get("otros_detectados")
                    or []
                )
                _legacy_headers = list(_legacy_rows[0].keys()) if _legacy_rows else None
                _date_check.append(("", _legacy_headers, _flat_map))
        _missing_dates = validate_required_date_mapping(_date_check)
        if _missing_dates:
            _labels = ", ".join(lbl or "hoja principal" for lbl in _missing_dates)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"No se detectó columna de fecha en: {_labels}. Mapeá la columna "
                    "de fecha o completá los datos antes de importar (sin fecha no se "
                    "puede registrar la operación)."
                ),
            )

    # ── F8b: validar decisiones de riesgo de columnas, ANTES del lease ─────────
    # Mismo espíritu que el gate de fecha (F6-A1): una decisión inválida (dropear
    # el único mapeo de un requerido, o rutear un opcional no seleccionado) se
    # rechaza upfront — nunca a mitad del import con el lease ya tomado.
    # Vista del mapeo efectivo por contexto (columna→campo, entidad efectiva) que
    # comparten la validación pre-lease (Task 2) y la aplicación dentro del
    # savepoint (Task 4). Se inicializa siempre (aunque no haya decisiones) para
    # que su referencia posterior sea segura.
    _risk_context_mappings: dict[str, list[MappingEntry]] = defaultdict(list)
    _risk_context_entities: dict[str, str] = {}
    # Decisiones que efectivamente se aplican: solo las de contextos INCLUIDOS en
    # el import (misma inclusión que el importador). Una decisión sobre un contexto
    # EXCLUIDO es no-op — sus filas ni se procesan, así que jamás debe rutear filas
    # a "Otros" ni dropear nada.
    _effective_risk_decisions: list[ColumnRiskDecision] = []
    if body.column_risk_decisions:
        for _m in body.column_mappings:
            if parse_target(_m.target_field).kind in ("ignore", "none"):
                continue
            _cid = _m.context_id or "table"
            _risk_context_entities[_cid] = _entity_for(_m)
            _risk_context_mappings[_cid].append(
                MappingEntry(
                    source_column=_m.source_column,
                    target_field=_m.target_field,
                    mapping_source="none",
                    user_selected=_m.user_selected,
                )
            )
        _risk_violations = validate_column_risk_decisions(
            body.column_risk_decisions,
            _risk_context_mappings,
            _risk_context_entities,
            confirmed_fields=body.confirmed_fields,
            context_confirmed=body.context_confirmed,
        )
        if _risk_violations:
            _risk_detail = " ".join(v.reason for v in _risk_violations)
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Decisión de columna riesgosa inválida: {_risk_detail}",
            )
        _effective_risk_decisions = [
            d
            for d in body.column_risk_decisions
            if _context_included(
                d.context_id, _risk_context_entities.get(d.context_id, "")
            )
        ]
    # Pares (context_id, source_column) DROPEADOS (solo contextos incluidos). El
    # context_id sintético "table" cubre los mapeos planos (single-sheet). Se usa
    # para saltear la columna en los mapeos que se pasan al importador y para no
    # crearle un custom field.
    _dropped_pairs: set[tuple[str, str]] = {
        (d.context_id, d.source_column)
        for d in _effective_risk_decisions
        if d.action == "drop_column"
    }

    # ── F-H3.d.6: un replay que no se puede validar no se confirma ──────────────
    # En el archivo de UNA sola tabla que además da de alta productos, el gate de
    # `historical_replay` no tiene saldo contra el cual evaluar: lo carga el mismo
    # archivo en la misma pasada. Antes se abstenía y las ventas sin respaldo
    # entraban igual a los libros, o sea justo lo contrario de lo que el modo
    # promete. Se rechaza acá —pre-lease, sin nada a medio importar— en vez de
    # degradar a `informational` en silencio: el usuario eligió que Véktor validara
    # cada venta contra el stock, y cambiarle eso sin decírselo lo deja creyendo que
    # su inventario se reconstruyó. Ver `domain/inventory_replay_gate` para el
    # límite y por qué es transitorio.
    #
    # Va acá, pegado al lease y no junto a la resolución del efecto, porque
    # necesita `_dropped_pairs` — el MISMO set de columnas eliminadas que se le
    # pasa al importador. Filtrar con las decisiones crudas dejaba fuera una
    # columna que el importador sí iba a ver (las de contextos no incluidos no
    # cuentan), y esa divergencia va en la peor dirección: el confirm no bloquea y
    # el respaldo termina degradando con el lease ya tomado.
    if _inventory_effects and len(_inventory_effects) == 1:
        _cid_unico, _efecto_unico = next(iter(_inventory_effects.items()))
        # Sobre el mapeo EFECTIVO, igual que la colisión de escalares: una columna
        # que las decisiones de riesgo (F8) van a dropear no da de alta nada, y
        # bloquear por ella sería bloquear por un mapeo que no va a existir.
        # Misma convención de clave que `context_mappings`, que es lo que
        # efectivamente viaja al importador: `context_id or "table"`.
        _targets_unicos = {
            m.target_field
            for m in _mappings_por_contexto.get(_cid_unico, [])
            if (m.context_id or "table", m.source_column) not in _dropped_pairs
        }
        if replay_no_gateable(
            hoja_unica=_plano,
            pide_replay=_efecto_unico == HISTORICAL_REPLAY,
            # Espejo de `wants_productos` / `wants_ventas` del importador, con la
            # columna leída del mapeo declarado (lo único disponible antes del
            # lease). Cuando la columna viene autodetectada y sin mapeo, esto no la
            # ve y el respaldo del importador es el que actúa.
            da_de_alta_productos=bool(
                body.confirmed_fields.get("productos")
                and (_summary_for_ctx.get("has_producto") or _inferred_type == "stock")
                and _targets_unicos & {"product_name", "name"}
            ),
            trae_ventas=bool(
                _inferred_type != "stock"
                and body.confirmed_fields.get("ventas")
                and (
                    _summary_for_ctx.get("has_venta")
                    or _inferred_type in ("ventas", "general")
                )
                and "amount" in _targets_unicos
            ),
            # Espejo de `wants_gastos`: una compra de mercadería declara stock que
            # todavía no existe cuando el gate mira, igual que un catálogo.
            trae_compras=bool(
                _inferred_type != "stock"
                and body.confirmed_fields.get("gastos")
                and (
                    _summary_for_ctx.get("has_gasto")
                    or _inferred_type in ("gastos", "general")
                )
                and "amount" in _targets_unicos
            ),
        ):
            await _emit_validation_reject(
                MOTIVO_REPLAY_NO_GATEABLE,
                {"context_id": _cid_unico, "inventory_effect": _efecto_unico},
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=MENSAJE_REPLAY_NO_GATEABLE.format(hoja=_hoja(_cid_unico)),
            )

    # ── F4: tomar el lease per-file ANTES de cualquier escritura ────────────────
    # CAS atómico NEEDS_CONFIRMATION→IMPORTING (o takeover si quedó stale),
    # commiteado sobre la sesión del request → el IMPORTING queda visible para un
    # confirm concurrente (que bloquea en el row-lock y luego ve IMPORTING → 409).
    # rowcount==0 → otro intento tiene el lease vivo → 409. Se toma DESPUÉS de las
    # validaciones puras (una request que va a rebotar por 422 nunca lo toma) y
    # ANTES de la creación de custom fields (primera escritura).
    _timings.mark("validaciones_pre_lease")
    _import_token = uuid.uuid4()
    if not await acquire_import_lease(session, tenant.tenant_id, file_id, _import_token):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="El archivo ya se está importando o ya se importó.",
        )
    _timings.mark("lease")

    # `_trace_id` ya quedó resuelto arriba, antes del PRIMER guard de validación:
    # la traza del fallo lo necesita y el import puede reventar mucho antes de la
    # línea donde se usaba. Definirlo tarde dejaba el nombre sin asignar en el
    # `except` → UnboundLocalError tapando el error real.

    async def _emit_confirm_failure(exc: BaseException, phase: str) -> None:
        """Deja traza de un confirm que NO terminó bien.

        Sin esto el confirm solo escribía en ``pipeline_events`` en el camino
        feliz (después de ``finalize_import_lease``), así que un archivo que
        nunca llega a importar no deja UNA sola fila: diagnosticarlo exigía
        acceso a la base y adivinar cuál de los desenlaces fue.

        **Cuándo llamarla: SIEMPRE DESPUÉS de liberar/compensar el lease**, con
        su commit ya hecho. Emitir antes lo rompe: ``emit_event`` abre un
        ``begin_nested()``, que flushea INCONDICIONALMENTE, y sobre una sesión
        que viene de un import reventado ese flush deja la transacción en estado
        abortado → el UPDATE de ``release_import_lease`` (best-effort, se traga
        su error) no llega a correr y el archivo queda clavado en IMPORTING. Lo
        detectó ``test_failure_after_f5_savepoints_still_compensates_lease``:
        primero se compensa, después se traza.

        Best-effort de punta a punta: la traza NUNCA puede tapar la excepción
        original ni romper la compensación.

        Sin PII: tipo de error, mensaje, etapa y qué contextos se intentaron —
        nunca valores de fila.
        """
        # El caller suele envolver la causa real en un HTTPException
        # (`raise HTTPException(...) from exc`); el tipo útil para diagnosticar
        # es el de la causa, no el del envoltorio.
        origen = exc.__cause__ if exc.__cause__ is not None else exc
        detail: dict[str, Any] = {
            "stage_failed": "confirm",
            "phase": phase,
            "error_type": type(origen).__name__,
            "error": _sanitize_error_message(origen),
            "confirmed_fields": body.confirmed_fields,
            "contexts_included": sorted(
                cid for cid, incluido in (body.context_confirmed or {}).items() if incluido
            ),
            # F-T: un confirm que muere es donde más importa saber dónde tardó —
            # si se fue en validar o en insertar cambia por completo qué mirar.
            # Las etapas ya cerradas están registradas: `stage`/`mark` anotan en
            # `finally`, así que el bloque que explotó también deja su tiempo.
            "timings_ms": _timings.as_detail(),
        }
        if isinstance(exc, HTTPException):
            detail["http_status"] = exc.status_code
        try:
            await pipeline_event_service.emit_event(
                session,
                trace_id=_trace_id,
                tenant_id=tenant.tenant_id,
                stage=STAGE_REJECT,
                file_id=file_id,
                detail=detail,
            )
            # Commit propio: la excepción original sigue viaje y haría rollback
            # del request, llevándose el evento.
            await session.commit()
        except Exception:  # noqa: BLE001 — la traza nunca tapa el error original
            logger.warning(
                "ingestion.confirm.failure_trace_failed",
                file_id=str(file_id),
                phase=phase,
            )

    # Savepoint que aísla TODO el import: ante cualquier fallo se revierte solo
    # (async-aware, sin `session.rollback()` manual — que rompería el re-arme del
    # savepoint del request), dejando la sesión viva para el UPDATE de
    # compensación. El commit final del request cierra la transacción completa.
    _import_sp = await session.begin_nested()
    try:
        # Crear definiciones de campos personalizados para mapeos custom_field:{key}
        # — idempotente, sin commit propio; el commit final cierra la transacción completa.
        if body.column_mappings:
            from app.application.services.field_definition_service import (  # noqa: PLC0415
                ensure_custom_field_exists,
            )

            for _mapping in body.column_mappings:
                # Una columna dropeada no crea custom field (no se va a importar).
                if (
                    _mapping.context_id or "table",
                    _mapping.source_column,
                ) in _dropped_pairs:
                    continue
                _parsed_target = parse_target(_mapping.target_field)
                if _parsed_target.kind == "custom":
                    _field_key = _parsed_target.field
                    await ensure_custom_field_exists(
                        session,
                        tenant.tenant_id,
                        _entity_for(_mapping),
                        _field_key,
                        _mapping.source_column,  # nombre de la columna como label inicial
                    )

        # ── F8b (Task 4): aplicar las decisiones de riesgo sobre una COPIA del
        # summary, DENTRO del savepoint. drop_column filtra columnas del summary
        # (y, más abajo, de los mapeos); route recalcula las filas afectadas y las
        # aparta para capturarlas en "Otros". Nada se persiste hasta el commit del
        # savepoint → si el confirm falla, rollback integral (invariante 4).
        _applied = (
            apply_column_risk_decisions(
                record.parsed_summary_json or {},
                _effective_risk_decisions,
                _risk_context_entities,
            )
            if _effective_risk_decisions
            else None
        )

        # Insert parsed rows into business tables, then mark done
        updated_summary = (
            _applied.summary if _applied is not None else dict(record.parsed_summary_json or {})
        )
        updated_summary["confirmed_fields"] = body.confirmed_fields
        # Persistir la elección de tratamiento del stock (apertura vs compra) en el summary
        # para que una relectura posterior conserve la decisión sin volver a preguntar.
        if body.stock_treatment is not None:
            updated_summary["stock_treatment"] = body.stock_treatment

        explicit_mappings: dict[str, str] | None = None
        if _flat_mappings:
            # F8b: una columna dropeada ("table") no se pasa al importador.
            explicit_mappings = {
                m.source_column: m.target_field
                for m in _flat_mappings
                if ("table", m.source_column) not in _dropped_pairs
            }

        context_mappings: dict[str, dict[str, str]] | None = None
        if _ctx_mappings:
            _cm: dict[str, dict[str, str]] = defaultdict(dict)
            for m in _ctx_mappings:
                # F8b: saltear las columnas dropeadas de su contexto.
                if (m.context_id or "table", m.source_column) in _dropped_pairs:
                    continue
                _cm[m.context_id or ""][m.source_column] = m.target_field
            context_mappings = dict(_cm)

        # F7d: subconjunto de mapeos de columnas de hojas de maestro (clientes/
        # proveedores) — solo nombres de columna → campo canónico, sin PII de
        # ninguna fila. Se persiste para que una relectura posterior
        # (reread_service._reread_master_entities) pueda reaplicar el mismo
        # upsert idempotente sin volver a preguntar el mapeo (sin esto, F7c/F7d
        # no adivinan el shape de una hoja de maestro y la saltean siempre).
        _master_context_mappings = {
            cid: mapping
            for cid, mapping in (context_mappings or {}).items()
            # Entidad EFECTIVA (override primero, ver _entity_for): un contexto
            # reasignado a customer/supplier tiene que persistir su mapeo para
            # el reread aunque su entidad ORIGINAL en el summary fuera otra —
            # y, a la inversa, uno reasignado FUERA de customer/supplier no
            # debe seguir tratándose como maestro.
            if (_override.get(cid) or _context_entity.get(cid)) in ("customer", "supplier")
        }
        _master_flat_mapping = (
            explicit_mappings if _entity_type in ("customer", "supplier") else None
        )
        if _master_context_mappings or _master_flat_mapping:
            updated_summary["master_column_mappings"] = {
                "context": _master_context_mappings,
                "flat": _master_flat_mapping,
            }

        # F8b (Task 5): persistir las decisiones de riesgo EFECTIVAS (crudas, ya
        # validadas) en el summary — mismo patrón que ``master_column_mappings``
        # (F7d): sin esto, una relectura re-parsea el crudo y perdería los
        # drop/route que el usuario decidió en este confirm. ``reread_service`` las
        # lee de ``parsed_summary_json`` y las RE-APLICA recomputando afectadas
        # (nunca confía en un conteo guardado — invariante 3). Solo las de
        # contextos INCLUIDOS (``_effective_risk_decisions``). Sin PII (contexto +
        # columna + acción; nunca valores de fila).
        if _effective_risk_decisions:
            updated_summary["column_risk_decisions"] = [
                d.model_dump() for d in _effective_risk_decisions
            ]

        # Estado de los maestros ANTES de importar: es el `before_json` del
        # ledger, y después del import ya no se puede reconstruir. Solo se paga si
        # el archivo TRAE clientes/proveedores — en un import común serían dos
        # SELECT completos para nada.
        _before_customers: dict[uuid.UUID, dict[str, Any]] = {}
        _before_suppliers: dict[uuid.UUID, dict[str, Any]] = {}
        # No alcanza con mirar las hojas DE maestros: el importador también crea
        # clientes y proveedores como efecto lateral de una hoja de ventas o
        # gastos, desde sus columnas de referencia (`supplier_name`,
        # `customer_name`, documento, email…). Sin contemplarlas, un archivo de
        # gastos creaba proveedores que después el borrado no podía revertir — y
        # peor, respondía `fully_reverted: true` igual.
        _trae_maestros = bool(_master_context_mappings or _master_flat_mapping) or any(
            m.target_field in MASTER_REFERENCE_TARGETS for m in body.column_mappings
        )
        if _trae_maestros:
            _before_customers, _before_suppliers = await snapshot_masters_before_import(
                session, tenant.tenant_id
            )
        # Se marca SIEMPRE, traiga maestros o no: un cero acá es el dato de que
        # el archivo no los trae, y saltear el checkpoint mezclaría ese tiempo
        # con el del import.
        _timings.mark("snapshot_maestros")

        _t0 = time.monotonic()
        counts = await insert_confirmed_data(
            session,
            tenant.tenant_id,
            updated_summary,
            body.confirmed_fields,
            column_mappings=explicit_mappings,
            context_mappings=context_mappings,
            context_confirmed=body.context_confirmed or None,
            context_entity=cast("dict[str, str]", body.context_entity) or None,
            source="ingestion",
            uploaded_file_id=file_id,
            # El schema lo tipa con Literals (valida la entrada); el importador
            # acepta el tipo ancho porque también lee el valor guardado en el
            # summary por una relectura anterior, que llega como str/dict plano.
            stock_treatment=cast("str | dict[str, str] | None", body.stock_treatment),
            # F-H3: el efecto RESUELTO (default + override), no el crudo del body:
            # el default no viaja en el payload y el importador no sabe calcularlo.
            inventory_effect=_inventory_effects,
            # F-H6.b: sin decisión para una hoja, sus envíos sin comprobante no
            # se cobran. El dict va tal cual: acá no hay default que resolver.
            shipping_decisions={d.context_id: d.action for d in body.shipping_decisions},
            purchase_cost_decisions={
                d.context_id: CostDecision(
                    context_id=d.context_id,
                    base=d.base,
                    shared_shipping=d.shared_shipping,
                    line_shipping=d.line_shipping,
                )
                for d in body.purchase_cost_decisions
            },
            # Ledger de reversa: `products` no tiene columna de origen, así que
            # sin este detalle no hay forma de saber qué productos creó este
            # archivo — y borrarlo no podría deshacerlos.
            return_details=True,
        )
        _confirm_latency_ms = int((time.monotonic() - _t0) * 1000)
        # `latency_ms` del evento NO cambia de significado: sigue siendo el import
        # y sólo el import. Redefinirlo a "todo el confirm" volvería incomparables
        # las filas ya escritas, que son la única serie histórica que hay. El
        # desglose completo viaja aparte, en `detail.timings_ms`.
        _timings.mark(
            "import",
            rows=(
                counts["ventas"]
                + counts["gastos"]
                + counts["productos"]
                + counts["clientes"]
                + counts["proveedores"]
            ),
        )

        # `product_details` sale de `counts` ANTES de cualquier otra cosa: más
        # abajo `counts` se serializa entero en `compact_summary`, y meter ahí el
        # detalle por producto engordaría el JSONB (justo lo que ese bloque
        # existe para evitar) y lo devolvería en la respuesta del endpoint.
        _product_details = counts.pop("product_details", []) or []

        # Maestros creados/modificados por este import. Sin esto, borrar el
        # archivo dejaba vivos sus clientes y proveedores, sin manera de saber de
        # dónde salieron.
        # Se llama SIEMPRE que el import haya devuelto ids de maestros, no solo
        # cuando se anticipó el snapshot: un CREADO no necesita `before` (no
        # existía), así que registrarlo sin snapshot es correcto y es lo que
        # permite desactivarlo al borrar. Solo los ACTUALIZADOS quedan sin
        # `before` si no se anticipó — y sin `before` no hay nada que restaurar.
        _master_details = await build_master_details(
            session, counts, _before_customers, _before_suppliers
        )

        # Se escribe DENTRO del savepoint del import: si el import se revierte,
        # el ledger se va con él (nunca un ledger de un import que no ocurrió).
        await record_import_ledger(
            session,
            tenant_id=tenant.tenant_id,
            file_id=file_id,
            product_details=_product_details,
            master_details=_master_details,
        )
        _timings.mark("ledger_reversa")

        # ── F8b (Task 4) + F8c (Minor 1): capturar en "Otros" las filas
        # ruteadas por columna riesgosa + counters + auditoría AGREGADA, todo
        # DENTRO del savepoint (si el confirm falla, se revierte junto con el
        # import — invariante 4). La captura usa la primitiva idempotente de
        # Task 3 (huella `risk:` propia). F8c: este bloque corre ANTES del
        # chequeo de import vacío (``check_nonempty_import`` más abajo) — la
        # captura NO depende de ``insert_confirmed_data`` (solo usa
        # ``_applied.routed_rows``/``routed_entity``/``routed_totals``/
        # ``dropped_columns``), y un archivo que rutea TODAS sus filas a
        # "Otros" (``total_inserted == 0``) necesita que ``_risk_a_otros`` ya
        # esté calculado para que el chequeo de vacío no lo confunda con un
        # import realmente vacío (Minor 1 de F8b).
        _risk_a_otros = 0
        if _applied is not None:
            _risk_importadas = 0
            for _cid, _rows_by_idx in _applied.routed_rows.items():
                if _rows_by_idx:
                    _risk_a_otros += await capture_column_risk_rows(
                        session,
                        tenant.tenant_id,
                        file_id,
                        _cid,
                        # Entidad efectiva del contexto (sale/expense/product/...);
                        # cae a "otros" si no se pudo resolver (nunca > 10 chars).
                        _applied.routed_entity.get(_cid) or "otros",
                        _rows_by_idx,
                        source="ingestion",
                    )
                _risk_importadas += _applied.routed_totals.get(_cid, 0) - len(_rows_by_idx)
            _columnas_eliminadas = sum(len(v) for v in _applied.dropped_columns.values())
            counts["filas_riesgo_a_otros"] = _risk_a_otros
            counts["filas_riesgo_importadas"] = _risk_importadas
            counts["columnas_eliminadas"] = _columnas_eliminadas

            # Auditoría AGREGADA (insert-only), sin PII (solo nombres de columna y
            # conteos — nunca valores/documentos/emails/teléfonos; invariantes 7, 9).
            if _applied.dropped_columns or _risk_a_otros or _columnas_eliminadas:
                from app.persistence.models.audit import (  # noqa: PLC0415
                    DecisionAuditLog,
                )

                session.add(
                    DecisionAuditLog(
                        tenant_id=tenant.tenant_id,
                        decision_type="INGESTION_COLUMN_RISK_DECISIONS",
                        decision_data={
                            "file_id": str(file_id),
                            "dropped_columns": _applied.dropped_columns,
                            "routed_to_others": {
                                _cid: len(_rows)
                                for _cid, _rows in _applied.routed_rows.items()
                                if _rows
                            },
                            "filas_riesgo_a_otros": _risk_a_otros,
                            "filas_riesgo_importadas": _risk_importadas,
                            "columnas_eliminadas": _columnas_eliminadas,
                        },
                        triggered_by="ingestion:confirm",
                        actor_user_id=None,
                        context={"source": "ingestion.confirm_file"},
                        created_at=datetime.now(UTC),
                    )
                )

        # Import vacío → 422; el compensador (except) restaura NEEDS_CONFIRMATION
        # y limpia el lease para reintentar con mapeo manual. F8c: las filas
        # capturadas en "Otros" (``routed_to_others``) cuentan como manejadas —
        # un archivo que ruteó TODO a "Otros" no es un import vacío.
        try:
            check_nonempty_import(
                counts,
                updated_summary,
                body.confirmed_fields,
                body.context_confirmed,
                routed_to_others=_risk_a_otros,
            )
        except EmptyImportError as exc:
            logger.warning(
                "ingestion.confirm.zero_inserted",
                file_id=str(file_id),
                row_count=updated_summary.get("row_count"),
            )
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=exc.user_message,
            ) from exc

        # Limpiar arrays de datos del summary antes de escribir de vuelta a la BD.
        # Para archivos grandes (multi-hoja o muchas filas) el JSONB puede pesar 10+ MB;
        # guardar solo metadata compacta evita un UPDATE lento en Neon.
        compact_summary: dict[str, Any] = {
            k: v
            for k, v in updated_summary.items()
            if k
            not in (
                "ventas_detectadas",
                "gastos_detectados",
                "stock_detectado",
                "otros_detectados",
                "preview_rows",
                "mapping_contexts",
                # F7d review (Important): clientes_detectados/proveedores_detectados
                # traen filas crudas con nombre/DNI/CUIT/email/teléfono — sin esto,
                # esa PII quedaba at-rest en el JSONB y se re-servía cruda por
                # GET /files/{id}/preview. El preview de maestros se computa aparte
                # (_build_master_previews, contra mapping_contexts) y NO depende de
                # estos buckets sobrevivir al confirm — mismo criterio que los
                # demás buckets de filas crudas de la lista.
                "clientes_detectados",
                "proveedores_detectados",
            )
        }
        compact_summary["imported_counts"] = counts

        # Guardar aprendizaje de mapeos confirmados — agrupado por entity_type del contexto
        if body.column_mappings:
            mapping_svc = ColumnMappingService(session)
            _learn: dict[str, list[dict[str, str]]] = defaultdict(list)
            for m in body.column_mappings:
                _learn[_entity_for(m)].append(
                    {"source_column": m.source_column, "target_field": m.target_field}
                )
            for _ent, _confirmed in _learn.items():
                await mapping_svc.save_mappings(tenant.tenant_id, _ent, _confirmed)
        _timings.mark("aprendizaje_mapeos")

        # Transición final IMPORTING→DONE, token-checked, en la MISMA transacción
        # que los inserts. Si un takeover nos robó el lease → ImportLeaseLostError
        # → rollback de todo (no queda dato a medias).
        await finalize_import_lease(
            session, tenant.tenant_id, file_id, _import_token, compact_summary
        )
        _timings.mark("finalize_lease")

        # F8c: cuántos contextos (hojas/grupos) tuvieron una decisión de riesgo
        # EFECTIVA — ruteo con filas reales o drop de columna. Sin PII (solo
        # cuenta context_ids, nunca valores de fila); 0 si no hubo decisiones
        # (``_applied is None``).
        _column_risk_contexts: set[str] = set()
        if _applied is not None:
            _column_risk_contexts.update(
                cid for cid, rows in _applied.routed_rows.items() if rows
            )
            _column_risk_contexts.update(
                cid for cid, cols in _applied.dropped_columns.items() if cols
            )

        await pipeline_event_service.emit_event(
            session,
            trace_id=_trace_id,
            tenant_id=tenant.tenant_id,
            stage=STAGE_CONFIRM,
            file_id=file_id,
            rows_out=(
                counts["ventas"]
                + counts["gastos"]
                + counts["productos"]
                + counts["clientes"]
                + counts["proveedores"]
            ),
            latency_ms=_confirm_latency_ms,
            detail={
                "imported_counts": counts,
                "confirmed_fields": body.confirmed_fields,
                # F-T: dónde se fue el tiempo. `latency_ms` (arriba) sigue siendo
                # sólo el import, por compatibilidad con las filas viejas; acá está
                # el confirm entero, etapa por etapa y con las filas que movió cada
                # una. Un tiempo sin su denominador no se puede comparar entre
                # archivos.
                "timings_ms": _timings.as_detail(),
                # Con qué mapeo se importó de verdad. Sin esto, saber si un
                # producto quedó con el costo cargado como precio de venta exigía
                # INFERIRLO de los alias aprendidos del tenant, que pudieron
                # cambiar después o haberse aprendido en otro archivo. De acá en
                # adelante cada import queda auto-explicado.
                # Solo pares columna → campo: nunca valores de fila (sin PII).
                "mappings": {
                    "flat": {m.source_column: m.target_field for m in _flat_mappings},
                    "context": {
                        cid: {m.source_column: m.target_field for m in ms}
                        for cid, ms in _mappings_por_contexto.items()
                    },
                },
                "stock_treatment": body.stock_treatment,
                # F-H3.a: el efecto RESUELTO, no lo que mandó el cliente. Saber con
                # qué mapeo entró un precio ya obligaba a guardar el snapshot (F10);
                # saber por qué el stock quedó como quedó necesita lo mismo, y el
                # default no viaja en el payload.
                "inventory_effect": _inventory_effects,
                "column_risk": {
                    "contextos_afectados": len(_column_risk_contexts),
                    "filas_riesgo_a_otros": counts.get("filas_riesgo_a_otros", 0),
                    "filas_riesgo_importadas": counts.get("filas_riesgo_importadas", 0),
                    "columnas_eliminadas": counts.get("columnas_eliminadas", 0),
                },
            },
        )
        # Import OK: liberar el savepoint (los cambios quedan en la transacción del
        # request, que los commitea al final).
        await _import_sp.commit()
    except ImportLeaseLostError as exc:
        # Un takeover ya tomó el lease con otro token. Descartar nuestro import
        # parcial (rollback del savepoint) y NO compensar (el estado es del nuevo dueño).
        await _import_sp.rollback()
        # Acá sí se traza antes de tocar el lease, y es seguro: este camino NO
        # compensa (el lease es del nuevo dueño), así que no hay UPDATE posterior
        # que un flush fallido pueda dejar sin correr.
        await _emit_confirm_failure(exc, phase="lease_lost")
        logger.warning("ingestion.confirm.lease_lost", file_id=str(file_id))
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="El import fue retomado por otro proceso. Volvé a intentar.",
        ) from None
    except BaseException as exc:
        # `BaseException`, no `Exception`: si el cliente corta la conexión, Starlette
        # cancela la task y `CancelledError` (BaseException desde 3.8) esquivaba este
        # bloque → el lease nunca se compensaba y el archivo quedaba clavado en
        # IMPORTING hasta que venciera el TTL. Cubre también el 422 de import vacío.
        # Orden obligatorio: revertir el savepoint (descarta lo parcial sin tocar la
        # sesión del request) → compensar el lease → RECIÉN trazar. La traza va
        # última porque su flush sobre una sesión reventada abortaría la
        # transacción y dejaría al archivo en IMPORTING (ver `_emit_confirm_failure`).
        await _import_sp.rollback()
        await release_import_lease(session, tenant.tenant_id, file_id, _import_token)
        await _emit_confirm_failure(exc, phase="import")
        raise

    # Enqueue score recalculation — BSL will aggregate newly confirmed data
    from app.application.services.score_trigger_service import (  # noqa: PLC0415
        trigger_score_recalculation,
    )

    try:
        trigger_score_recalculation.delay(str(tenant.tenant_id), str(file_id))
    except Exception:
        logger.warning("ingestion.confirm.score_trigger_failed", file_id=str(file_id))
    # F-T: `.delay()` habla con el broker DENTRO del request. Si el broker está
    # lento o caído, el usuario espera — y es la clase de demora que nadie
    # atribuiría al confirm porque el import ya terminó. Va al log y no al evento
    # porque ocurre después de emitirlo.
    _timings.mark("encolar_score")

    # El log va DESPUÉS del encolado (antes quedaba antes) para que lleve el
    # desglose completo, incluida esa última etapa.
    logger.info(
        "ingestion.confirm.done",
        file_id=str(file_id),
        ventas=counts["ventas"],
        gastos=counts["gastos"],
        productos=counts["productos"],
        timings=_timings.as_detail(),
    )

    parts: list[str] = []
    if counts["ventas"]:
        parts.append(f"{counts['ventas']} venta(s)")
    if counts["gastos"]:
        parts.append(f"{counts['gastos']} gasto(s)")
    if counts["productos"]:
        parts.append(f"{counts['productos']} producto(s)")
    if counts["clientes"]:
        parts.append(f"{counts['clientes']} cliente(s)")
    if counts["proveedores"]:
        parts.append(f"{counts['proveedores']} proveedor(es)")

    message = (
        f"Importados: {', '.join(parts)}. La puntuación será recalculada."
        if parts
        else "Datos confirmados. La puntuación de salud será recalculada."
    )

    # Avisos human-in-the-loop: el import no bloquea, pero le señala al usuario qué
    # quedó incompleto para que lo complete (proveedor, producto) o lo clasifique.
    warnings: list[str] = []
    if counts.get("sin_proveedor"):
        warnings.append(
            f"{counts['sin_proveedor']} compra(s) sin proveedor identificado se agruparon "
            "en «No identificado». Asignales el proveedor real cuando puedas."
        )
    if counts.get("sin_producto"):
        warnings.append(
            f"{counts['sin_producto']} compra(s) sin producto detallado crearon un producto "
            "incompleto. Completá precio de venta y datos en Productos."
        )
    # F7d: taxonomía reconciliada de resolución de referencia. "anonimo" (venta de
    # mostrador / compra sin proveedor informado) NUNCA avisa — es el caso normal.
    # Solo "no_resuelto" (trajo una referencia que no matcheó contra ningún
    # cliente/proveedor existente) amerita revisión.
    if counts.get("ventas_cliente_no_resuelto"):
        warnings.append(
            f"{counts['ventas_cliente_no_resuelto']} venta(s) con referencia de cliente "
            "que no se pudo identificar quedaron asignadas a «Local». Revisá el dato "
            "(documento/email/teléfono) cuando puedas."
        )
    if counts.get("compras_proveedor_no_resuelto"):
        warnings.append(
            f"{counts['compras_proveedor_no_resuelto']} compra(s) con referencia de "
            "proveedor que no se pudo identificar quedaron agrupadas en «No "
            "identificado». Revisá el dato (CUIL/email/teléfono) cuando puedas."
        )
    # F7d: maestros con filas que necesitan revisión o son inválidas — NUNCA se
    # persisten (needs_review/invalid/conflicto se saltean siempre en el import
    # service), así que el aviso es la única señal de que quedaron afuera.
    if counts.get("clientes_needs_review"):
        warnings.append(
            f"{counts['clientes_needs_review']} fila(s) de clientes no tenían un dato "
            "fuerte (DNI, CUIT, email o teléfono) para identificar sin ambigüedad y no "
            "se importaron."
        )
    if counts.get("clientes_invalidos"):
        warnings.append(
            f"{counts['clientes_invalidos']} fila(s) de clientes tenían datos inválidos "
            "o ambiguos y no se importaron."
        )
    if counts.get("proveedores_needs_review"):
        warnings.append(
            f"{counts['proveedores_needs_review']} fila(s) de proveedores no tenían un "
            "dato fuerte (CUIL, email o teléfono) para identificar sin ambigüedad y no "
            "se importaron."
        )
    if counts.get("proveedores_invalidos"):
        warnings.append(
            f"{counts['proveedores_invalidos']} fila(s) de proveedores tenían datos "
            "inválidos o ambiguos y no se importaron."
        )
    # F-H4: el monto salió de una cuenta y no del archivo. Los dos avisos existen
    # porque no significan lo mismo: calcular un total que faltaba es el caso
    # esperado (y hay que decirlo para que nadie crea que el archivo lo traía);
    # que el total del archivo no cuadre con precio × cantidad casi siempre
    # significa que la planilla tiene un descuento, un impuesto o una cantidad que
    # mide otra cosa, y eso lo tiene que mirar el dueño del negocio.
    if counts.get("montos_calculados"):
        warnings.append(
            f"{counts['montos_calculados']} fila(s) no traían el monto: se calculó "
            "como precio unitario × cantidad."
        )
    if counts.get("montos_discrepantes"):
        warnings.append(
            f"{counts['montos_discrepantes']} fila(s) tenían un monto distinto de "
            "precio unitario × cantidad. Se guardó el calculado y el monto original "
            "quedó registrado en cada fila. Revisalas: suele ser un descuento, un "
            "impuesto o una cantidad que mide otra cosa."
        )
    # F-H6.b: el envío de un comprobante se cobra una vez. Los tres avisos dicen
    # cosas distintas y por eso no se resumen en uno.
    if counts.get("envios_repetidos_colapsados"):
        warnings.append(
            f"{counts['envios_repetidos_colapsados']} envío(s) figuraban repetidos "
            "en varias líneas del mismo comprobante: se registró uno solo por "
            "comprobante, no uno por línea."
        )
    if counts.get("envios_sin_comprobante"):
        warnings.append(
            f"{counts['envios_sin_comprobante']} fila(s) traían un costo de envío "
            "pero no el número de comprobante, así que no se registró: sin ese dato "
            "no se puede saber si es un envío repetido en varias líneas o varios "
            "envíos distintos. Mapeá la columna del comprobante y volvé a importar."
        )
    if counts.get("envios_cifras_distintas"):
        warnings.append(
            f"{counts['envios_cifras_distintas']} comprobante(s) traían más de una "
            "cifra de envío. Se registraron todas —pueden ser flete y seguro—, pero "
            "revisalas por si la planilla mezcla el total con el prorrateo."
        )
    if counts.get("envios_sin_fecha"):
        warnings.append(
            f"{counts['envios_sin_fecha']} envío(s) no se registraron porque su fila "
            "no tiene una fecha reconocible."
        )
    if counts.get("filas_sin_monto"):
        warnings.append(
            f"{counts['filas_sin_monto']} fila(s) sin monto quedaron en «Otros»: no "
            "traían el total ni el precio unitario y la cantidad para calcularlo."
        )
    if counts.get("otros"):
        # F1-fix: cubre también los productos con nombre ambiguo (F1) — ya no
        # generan un warning propio, "otros" los cuenta porque la fila ambigua
        # se persiste ahí (evita doble conteo/mensaje solapado).
        warnings.append(
            f"{counts['otros']} fila(s) quedaron en «Otros» para que las revises y clasifiques."
        )
    # F8b: decisiones de columnas riesgosas aplicadas en este confirm.
    if counts.get("columnas_eliminadas"):
        warnings.append(
            f"{counts['columnas_eliminadas']} columna(s) con alto porcentaje de datos "
            "faltantes se eliminaron de la importación por tu decisión."
        )
    if counts.get("filas_riesgo_a_otros"):
        warnings.append(
            f"{counts['filas_riesgo_a_otros']} fila(s) con datos faltantes o inválidos en "
            "columnas riesgosas se enviaron a «Otros» para que las completes."
        )

    # F-H3.d.6: el respaldo se activó. No debería llegar acá —el confirm rechaza
    # antes del lease— pero si el importador vio un alta de productos que la
    # validación no llegó a ver, la hoja se degradó y hay que DECIRLO: un replay
    # que no se aplicó y no se avisa se lee como un replay que se aplicó.
    if counts.get("replay_degradado"):
        warnings.append(
            "Estas ventas no modificaron el inventario: el archivo también da de "
            "alta productos, así que no había stock previo contra el cual validar "
            "cada venta. Se calculó el impacto y quedó a la vista. Para aplicarlo, "
            "usá «Aplicar las ventas al inventario» en el panel de impacto: ahí el "
            "cálculo corre contra el stock ya cargado. Las ventas que no alcancen a "
            "cubrirse quedarán con el descuento pendiente."
        )

    # F-H3.b: el impacto que el archivo TENDRÍA sobre el stock. Nada se aplicó —
    # el default es `informational`—, así que el aviso dice qué pasaría, no qué
    # pasó. Un saldo que se va abajo de cero al reproducir la historia casi
    # siempre significa que faltan compras viejas, no que el stock de hoy esté
    # mal: por eso informa y no bloquea.
    if counts.get("stock_proyectado_negativo"):
        _impacto = counts.get("impacto_inventario") or []
        _neg = [p for p in _impacto if p.get("primer_negativo_en")]
        _muestra = ", ".join(f"«{p['product_name']}»" for p in _neg[:3])
        _resto = len(_neg) - 3
        if _resto > 0:
            _muestra += f" y {_resto} más"
        warnings.append(
            f"Si se aplicara la historia de este archivo, {len(_neg)} producto(s) "
            f"quedarían con stock negativo en algún momento ({_muestra}). No se "
            "modificó el stock: probablemente falten compras anteriores. Revisá el "
            "detalle antes de aplicar el histórico."
        )

    # F-H2: vincular una venta a un producto es resolver su IDENTIDAD, no afirmar
    # que había stock ese día. Estos dos avisos son lo que el archivo puede probar
    # sobre sí mismo, y por eso no dependen del ancla `catalog_initial_stock` que
    # sí necesita el chequeo de divergencia de más abajo (que es el complemento:
    # mira el stock reconstruible del tenant, no lo que declara este archivo, y
    # sobre datos históricos sin ancla no cubre nada).
    if counts.get("historial_insuficiente"):
        _productos = counts.get("historial_insuficiente_productos") or []
        _muestra = ", ".join(f"«{p}»" for p in _productos[:3])
        _resto = len(_productos) - 3
        if _resto > 0:
            _muestra += f" y {_resto} más"
        warnings.append(
            f"{counts['historial_insuficiente']} venta(s) son anteriores a la primera "
            f"compra que este archivo registra de su producto ({_muestra}). Se "
            "importaron y quedaron vinculadas, pero no se puede afirmar que hubiera "
            "stock ese día: puede faltar la compra vieja."
        )
    if counts.get("historial_sin_fecha"):
        warnings.append(
            f"{counts['historial_sin_fecha']} venta(s) corresponden a productos que este "
            "archivo declara sin fecha de adquisición. Se vincularon igual; verificar el "
            "stock a la fecha de cada venta necesitaría esa fecha o la compra original."
        )

    # Aviso temporal (human-in-the-loop): si las ventas recién importadas dejan el stock
    # reconstruible en negativo por FECHAS (compras posteriores a las ventas, o compra sin
    # registrar), advertir. Read-only, no bloquea y falla-silencioso (un aviso nunca debe
    # romper un import ya persistido).
    try:
        from sqlalchemy import select as _select  # noqa: PLC0415

        from app.application.services.inventory_temporal_service import (  # noqa: PLC0415
            check_products_temporal_divergence,
        )
        from app.persistence.models.transaction import SaleEntry  # noqa: PLC0415

        affected_ids = [
            pid
            for pid in (
                (
                    await session.execute(
                        _select(SaleEntry.product_id)
                        .where(
                            SaleEntry.tenant_id == tenant.tenant_id,
                            SaleEntry.source_upload_id == file_id,
                            SaleEntry.product_id.isnot(None),
                        )
                        .distinct()
                    )
                )
                .scalars()
                .all()
            )
            if pid is not None
        ]
        # Cota de latencia: el chequeo corre SINCRÓNICO en el request de confirm (2 queries
        # por producto). Un import masivo podría tocar cientos de productos → acotamos y lo
        # logueamos (nunca un cap silencioso).
        max_temporal_check = 200
        if len(affected_ids) > max_temporal_check:
            logger.info(
                "ingestion.confirm.temporal_check_capped",
                file_id=str(file_id),
                affected=len(affected_ids),
                cap=max_temporal_check,
            )
            affected_ids = affected_ids[:max_temporal_check]
        if affected_ids:
            temporal = await check_products_temporal_divergence(
                session, tenant.tenant_id, product_ids=affected_ids
            )
            for div in temporal.divergences:
                warnings.append(
                    f"«{div.product_name}» registra ventas por fecha que superan el stock "
                    "reconstruible (compras posteriores a las ventas o compra sin registrar). "
                    "Revisá las fechas de compra o registrá la compra faltante."
                )
    except Exception:  # noqa: BLE001
        logger.warning("ingestion.confirm.temporal_check_failed", file_id=str(file_id))

    # F-H3.c: el impacto proyectado, para que la UI lo muestre producto por
    # producto. Ya viene ordenado con los negativos arriba, así que el corte se
    # lleva lo menos interesante — y el total va aparte porque truncar sin
    # decirlo se leería como "estos son todos los productos afectados".
    _impacto_filas = counts.get("impacto_inventario") or []
    return ConfirmIngestionResponse(
        file_id=record.id,
        status=PROCESSING_STATUS_DONE,
        message=message,
        warnings=warnings,
        inventory_impact=[
            InventoryImpactItem(**fila) for fila in _impacto_filas[:_MAX_IMPACTO_LISTADO]
        ],
        inventory_impact_total=len(_impacto_filas),
    )


# ── Relectura de archivos (REREAD_FILE) ────────────────────────────────────────


@router.post(
    "/files/{file_id}/reread/preview",
    response_model=RereadPreviewResponse,
    summary="Preview de relectura (dry_run): re-lee el archivo y proyecta cambios",
    dependencies=[Depends(require_role("OWNER", "ADMIN"))],
)
async def reread_preview(
    file_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> RereadPreviewResponse:
    from app.application.services import reread_service  # noqa: PLC0415

    try:
        preview = await reread_service.preview_reread(session, file_id, tenant.tenant_id)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Archivo no encontrado."
        ) from exc

    return RereadPreviewResponse(
        file_id=file_id,
        counts=RereadCounts(**preview.counts()),
        legacy_fallback=preview.legacy_fallback,
        sample_changes=preview.sample_changes,
    )


@router.post(
    "/files/{file_id}/reread/apply",
    response_model=RereadApplyStartResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Encola el apply de la relectura en background (devuelve run_id para polling)",
    dependencies=[
        Depends(require_modify_access),
        Depends(ensure_tenant_not_under_maintenance),
    ],
)
async def reread_apply(
    file_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> RereadApplyStartResponse:
    """El apply puede insertar miles de filas (minutos). Corre en background: se
    crea el run, se encola la task y se devuelve el ``run_id`` para que el frontend
    haga polling de ``GET /reread/runs/{run_id}``. Guard anti-duplicado: una sola
    relectura RUNNING por tenant."""
    from app.application.services import reread_service  # noqa: PLC0415

    try:
        run = await reread_service.start_background_apply(
            session, file_id, tenant.tenant_id
        )
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Archivo no encontrado."
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc

    # Commit ANTES de encolar para que el worker vea el run.
    await session.commit()

    from app.jobs.reread_worker import reread_apply as reread_apply_task  # noqa: PLC0415

    try:
        reread_apply_task.delay(str(run.id), str(file_id), str(tenant.tenant_id))
    except Exception as exc:  # noqa: BLE001
        logger.error("ingestion.reread.enqueue_failed", run_id=str(run.id), error=str(exc))
        run.status = "FAILED"
        run.completed_at = datetime.now(UTC)
        run.details_json = {**(run.details_json or {}), "phase": "enqueue_failed"}
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No se pudo encolar la relectura. Reintentá en unos segundos.",
        ) from exc

    logger.info("ingestion.reread.enqueued", file_id=str(file_id), run_id=str(run.id))
    return RereadApplyStartResponse(file_id=file_id, run_id=run.id, status="RUNNING")


@router.get(
    "/files/{file_id}/reread/runs/{run_id}",
    response_model=RereadRunStatusResponse,
    summary="Estado del apply en background (polling)",
    dependencies=[Depends(require_role("OWNER", "ADMIN"))],
)
async def reread_run_status(
    file_id: uuid.UUID,
    run_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> RereadRunStatusResponse:
    from app.application.services import reread_service  # noqa: PLC0415

    run = await reread_service.get_reread_run(session, run_id, tenant.tenant_id, file_id)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Relectura no encontrada."
        )
    d = run.details_json or {}
    # status del run → status del apply: RUNNING / APPLIED / FAILED.
    items = [RereadItem(**it) for it in d.get("sample_changes", []) if isinstance(it, dict)]
    return RereadRunStatusResponse(
        run_id=run.id,
        file_id=file_id,
        status=run.status,
        to_update=int(d.get("to_update", 0) or 0),
        preserved=int(d.get("preserved", 0) or 0),
        new=int(d.get("new", 0) or 0),
        voided=int(d.get("voided", 0) or 0),
        inserted=int(d.get("inserted", 0) or 0),
        legacy_fallback=bool(d.get("legacy_fallback", False)),
        items=items,
        error=d.get("error"),
        clientes=int(d.get("clientes", 0) or 0),
        proveedores=int(d.get("proveedores", 0) or 0),
    )


@router.post(
    "/files/{file_id}/reread/undo",
    response_model=RereadUndoResponse,
    summary="Revierte la última relectura aplicada de este archivo",
    dependencies=[Depends(require_role("OWNER", "ADMIN"))],
)
async def reread_undo(
    file_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> RereadUndoResponse:
    from app.application.services import reread_service  # noqa: PLC0415

    run = await reread_service.latest_applied_run_for_file(
        session, file_id, tenant.tenant_id
    )
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No hay una relectura aplicada para revertir en este archivo.",
        )

    try:
        result = await reread_service.undo_reread(session, run.id, tenant.tenant_id)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Relectura no encontrada."
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc

    return RereadUndoResponse(
        run_id=run.id,
        restored=result["restored"],
        removed=result["removed"],
        status=result["status"],
        not_reverted_entities=result.get("not_reverted_entities", []),
    )


@router.post(
    "/files/{file_id}/inventory-replay",
    response_model=InventoryReplayResponse,
    summary="Aplica al inventario la historia de ventas de un archivo (por hoja)",
    dependencies=[
        Depends(require_modify_access),
        Depends(ensure_tenant_not_under_maintenance),
    ],
)
async def inventory_replay(
    file_id: uuid.UUID,
    body: InventoryReplayRequest,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> InventoryReplayResponse:
    """F-H3.d.4 — el segundo paso de "confirmar → revisar → aplicar".

    Confirmar no toca stock (`inventory_effect` default: `informational`); acá el
    usuario aplica la historia de las hojas que eligió.

    Gateado con ``require_modify_access`` (PIN) porque mueve inventario en masa:
    es la misma clase de operación que la relectura, no un alta de datos.

    ``dry_run`` corre EXACTAMENTE el mismo cálculo sin escribir. El número que
    devuelve el apply es el autoritativo: entre un preview y la escritura el stock
    pudo cambiar, y por eso el cálculo se rehace adentro de la transacción que
    escribe (misma regla que el borrado por procedencia).
    """
    from app.application.services import inventory_replay_service  # noqa: PLC0415

    repo = FileRepository(session)
    record = await repo.get_by_id(file_id, tenant.tenant_id)
    if not record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Archivo no encontrado."
        )

    # El eje de inventario se declara POR HOJA al confirmar, así que ESCRIBIR sin
    # decir sobre cuáles contradice esa declaración: un libro con una hoja de
    # ventas de servicios (`no_inventory`) y otra de mercadería descontaría las
    # dos. El preview sí puede correr sobre todo el archivo — es read-only y es la
    # forma en que la pantalla descubre qué hojas hay para ofrecerlas.
    if not body.dry_run and not body.context_ids:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "Elegí qué hojas aplicar al inventario. Cada hoja declaró su "
                "propio efecto al importar, así que aplicar el archivo entero "
                "movería stock por filas que dijiste que no lo mueven."
            ),
        )

    outcome = await inventory_replay_service.run_inventory_replay(
        session,
        tenant.tenant_id,
        file_id,
        context_ids=body.context_ids,
        apply=not body.dry_run,
    )
    if not body.dry_run:
        # Un replay mueve el stock de muchos productos de una: los componentes de
        # liquidez y de inventario del score quedaban calculados sobre el estado
        # anterior hasta que otra escritura cualquiera disparara el recálculo. Va
        # DESPUÉS del commit por la misma razón que en el borrado: el worker abre
        # su propia sesión y encolarlo antes lo haría leer un estado inexistente.
        trigger_score_recalculation_after_commit(
            session, str(tenant.tenant_id), "inventory_replay"
        )
        await session.commit()

    warnings: list[str] = []
    if not outcome.alcance_por_hoja:
        warnings.append(
            "Algunas ventas de este archivo no tienen registrada la hoja de origen "
            "(se importaron antes de que se guardara ese dato): el alcance fue el "
            "archivo completo, no las hojas elegidas."
        )
    if outcome.sin_stock:
        warnings.append(
            f"{len(outcome.sin_stock)} venta(s) quedaron sin aplicar por falta de "
            "stock. No se anularon: cargá el inventario que falta y volvé a aplicar."
        )

    return InventoryReplayResponse(
        file_id=file_id,
        dry_run=body.dry_run,
        aplicadas=outcome.aplicadas,
        ya_aplicadas=outcome.ya_aplicadas,
        sin_stock=[
            PendingSaleItem(
                sale_id=str(p.sale_id),
                product_id=str(p.product_id),
                product_name=p.product_name,
                quantity=p.quantity,
                disponible=p.disponible,
            )
            for p in outcome.sin_stock
        ],
        impacto=[InventoryImpactItem(**p.as_dict()) for p in outcome.impacto.productos],
        hojas=outcome.hojas,
        alcance_por_hoja=outcome.alcance_por_hoja,
        warnings=warnings,
    )
