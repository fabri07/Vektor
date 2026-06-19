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
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant, get_current_user, require_role
from app.application.services import pipeline_event_service
from app.application.services.column_mapping_service import REQUIRED_FIELDS, ColumnMappingService
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
from app.application.services.llm_file_type_detector import maybe_detect_file_type
from app.config.settings import get_settings
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
    STAGE_UPLOAD,
    STAGE_VALIDATE,
)
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.persistence.repositories.file_repository import FileRepository
from app.schemas.ingestion import (
    ColumnAtRisk,
    ColumnMapping,
    ColumnMappingSuggestion,
    ConfirmIngestionRequest,
    ConfirmIngestionResponse,
    DropColumnsRequest,
    FilePreviewResponse,
    FileStatusItem,
    RereadApplyStartResponse,
    RereadCounts,
    RereadItem,
    RereadPreviewResponse,
    RereadRunStatusResponse,
    RereadUndoResponse,
    TenantColumnMappingResponse,
    UploadResponse,
)

router = APIRouter()

logger = get_logger(__name__)

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

    # Build S3 key: uploads/{tenant_id}/{uuid}/{filename}
    file_uuid = uuid.uuid4()
    s3_key = f"uploads/{tenant.tenant_id}/{file_uuid}/{filename}"

    s3 = S3Client()
    stored_key = await s3.upload_to_key(content=content, key=s3_key, content_type=detected_mime)

    # FASE 0: trazabilidad (trace_id agrupa el ciclo de vida) + hash para dedup.
    trace_id = uuid.uuid4()
    content_hash = hashlib.sha256(content).hexdigest()
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
    repo = FileRepository(session)
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

    # Dedup de re-upload: avisar (no bloquear) si este contenido ya fue importado.
    _dup = await repo.find_imported_by_content_hash(
        tenant.tenant_id, content_hash, exclude_id=saved.id
    )
    dup_of = _dup.id if _dup else None
    dup_warning = (
        f"Este archivo ya fue importado antes ('{_dup.original_filename}'). "
        "Si confirmás, podrías duplicar los datos."
        if _dup
        else None
    )

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

    return FilePreviewResponse(
        file_id=record.id,
        processing_status=record.processing_status,
        parsed_summary_json=record.parsed_summary_json,
        columns_at_risk=columns_at_risk,
    )


@router.post(
    "/files/{file_id}/drop-columns",
    summary="Drop risky columns and keep file in NEEDS_CONFIRMATION",
)
async def drop_columns(
    file_id: uuid.UUID,
    body: DropColumnsRequest,
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
            detail="Solo se pueden eliminar columnas en archivos pendientes de confirmación.",
        )

    summary = dict(record.parsed_summary_json or {})
    columns_to_drop = set(body.columns)

    # Eliminar columnas del summary (headers, preview_rows, columns_at_risk)
    summary["headers"] = [h for h in summary.get("headers", []) if h not in columns_to_drop]
    summary["columns"] = summary["headers"]
    summary["columns_at_risk"] = [
        c for c in summary.get("columns_at_risk", []) if c.get("column") not in columns_to_drop
    ]
    summary["preview_rows"] = [
        {k: v for k, v in row.items() if k not in columns_to_drop}
        for row in summary.get("preview_rows", [])
    ]
    for data_key in (
        "ventas_detectadas",
        "gastos_detectados",
        "productos_detectados",
        "stock_detectado",
    ):
        if isinstance(summary.get(data_key), list):
            summary[data_key] = [
                {k: v for k, v in row.items() if k not in columns_to_drop}
                for row in summary[data_key]
            ]
    summary.setdefault("warnings", []).append(
        f"Columnas eliminadas por el usuario: {', '.join(sorted(columns_to_drop))}."
    )

    record.parsed_summary_json = summary
    await repo.save(record)
    await session.commit()

    logger.info(
        "ingestion.drop_columns",
        file_id=str(file_id),
        dropped=list(columns_to_drop),
    )
    return {"file_id": str(file_id), "dropped_columns": list(columns_to_drop)}


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


@router.delete(
    "/files/{file_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an uploaded file",
)
async def delete_file(
    file_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> None:
    repo = FileRepository(session)
    record = await repo.get_by_id(file_id, tenant.tenant_id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Archivo no encontrado.")

    # FASE 0: soft delete — el crudo en R2 se preserva (input para ML + respaldo).
    # El archivo deja de aparecer en la UI pero el registro y el objeto persisten.
    record.deleted_at = datetime.now(UTC)
    await repo.save(record)
    await session.commit()


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
    "/files/{file_id}/column-mappings",
    response_model=list[ColumnMappingSuggestion],
    summary="Get column mapping suggestions for a file",
)
async def get_column_mappings(
    file_id: uuid.UUID,
    entity_type: str = Query(
        default="sale",
        description="Tipo de entidad: sale | expense | product",
    ),
    context_id: str | None = Query(
        default=None,
        description="Contexto (hoja/tabla) en archivos multi-contexto. Si se da, "
        "se usan sus headers/preview y su entity_type (se ignora el param entity_type).",
    ),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[ColumnMappingSuggestion]:
    if entity_type not in ("sale", "expense", "product", "inventory"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="entity_type debe ser: sale, expense, product o inventory.",
        )

    repo = FileRepository(session)
    record = await repo.get_by_id(file_id, tenant.tenant_id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Archivo no encontrado.")

    summary = record.parsed_summary_json or {}

    # Resolver headers/sample_rows/entity_type por contexto si se pidió uno.
    resolved_entity = entity_type
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
        resolved_entity = ctx.get("entity_type") or entity_type
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


@router.post(
    "/files/{file_id}/confirm",
    response_model=ConfirmIngestionResponse,
    summary="Confirm ingestion of parsed data",
)
async def confirm_file(
    file_id: uuid.UUID,
    body: ConfirmIngestionRequest,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> ConfirmIngestionResponse:
    repo = FileRepository(session)
    record = await repo.get_by_id(file_id, tenant.tenant_id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Archivo no encontrado.")

    if record.processing_status != PROCESSING_STATUS_NEEDS_CONFIRMATION:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"El archivo no está pendiente de confirmación "
                f"(estado actual: {record.processing_status})."
            ),
        )

    # Derivar entity_type desde el summary para validación y aprendizaje
    _inferred_type = (record.parsed_summary_json or {}).get("inferred_type", "general")
    _entity_map = {"ventas": "sale", "gastos": "expense", "stock": "product"}
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

    def _entity_for(mapping: ColumnMapping) -> str:
        # Con context_id, el entity_type se deriva del contexto del summary
        # (autoritativo, igual que la inserción en _insert_multisheet_data). El del
        # payload es solo fallback para que validación/aprendizaje/custom fields
        # queden bajo la misma entidad que realmente se importa.
        if mapping.context_id:
            return (
                _context_entity.get(mapping.context_id)
                or mapping.entity_type
                or _entity_type
            )
        return mapping.entity_type or _entity_type

    def _context_included(context_id: str, entity_type: str) -> bool:
        if body.context_confirmed:
            return bool(body.context_confirmed.get(context_id, False))
        # Legacy: sin context_confirmed, gating por tipo vía confirmed_fields
        return bool(
            (entity_type == "sale" and body.confirmed_fields.get("ventas"))
            or (entity_type == "expense" and body.confirmed_fields.get("gastos"))
            or (entity_type == "product" and body.confirmed_fields.get("productos"))
        )

    def _missing_required(entity_type: str, mappings: list[ColumnMapping]) -> set[str]:
        mapped = {
            m.target_field
            for m in mappings
            if m.target_field != "ignore" and not m.target_field.startswith("custom_field:")
        }
        return set(REQUIRED_FIELDS.get(entity_type, [])) - mapped

    # Validación de requeridos — plano (legacy)
    if _flat_mappings:
        confirmed_entity = (
            (body.confirmed_fields.get("ventas") and _entity_type == "sale")
            or (body.confirmed_fields.get("gastos") and _entity_type == "expense")
            or (body.confirmed_fields.get("productos") and _entity_type == "product")
        )
        if confirmed_entity:
            missing = _missing_required(_entity_type, _flat_mappings)
            if missing:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Campos requeridos sin mapear: {', '.join(sorted(missing))}",
                )

    # Validación de requeridos — por contexto (multi-hoja), solo contextos incluidos
    if _ctx_mappings:
        _ctx_groups: dict[str, list[ColumnMapping]] = defaultdict(list)
        for m in _ctx_mappings:
            _ctx_groups[m.context_id or ""].append(m)
        for _cid, _ms in _ctx_groups.items():
            _ent = _entity_for(_ms[0])
            if _context_included(_cid, _ent):
                missing = _missing_required(_ent, _ms)
                if missing:
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=(
                            f"[{_cid}] Campos requeridos sin mapear: "
                            f"{', '.join(sorted(missing))}"
                        ),
                    )

    # Crear definiciones de campos personalizados para mapeos custom_field:{key}
    # — idempotente, sin commit propio; el commit final cierra la transacción completa.
    if body.column_mappings:
        from app.application.services.field_definition_service import (  # noqa: PLC0415
            ensure_custom_field_exists,
        )

        for _mapping in body.column_mappings:
            if _mapping.target_field.startswith("custom_field:"):
                _field_key = _mapping.target_field[len("custom_field:"):]
                await ensure_custom_field_exists(
                    session,
                    tenant.tenant_id,
                    _entity_for(_mapping),
                    _field_key,
                    _mapping.source_column,  # nombre de la columna como label inicial
                )

    # Insert parsed rows into business tables, then mark done
    updated_summary = dict(record.parsed_summary_json or {})
    updated_summary["confirmed_fields"] = body.confirmed_fields

    explicit_mappings: dict[str, str] | None = None
    if _flat_mappings:
        explicit_mappings = {m.source_column: m.target_field for m in _flat_mappings}

    context_mappings: dict[str, dict[str, str]] | None = None
    if _ctx_mappings:
        _cm: dict[str, dict[str, str]] = defaultdict(dict)
        for m in _ctx_mappings:
            _cm[m.context_id or ""][m.source_column] = m.target_field
        context_mappings = dict(_cm)

    _trace_id = record.trace_id or record.id
    bind_request_context(trace_id=_trace_id)
    _t0 = time.monotonic()
    counts = await insert_confirmed_data(
        session,
        tenant.tenant_id,
        updated_summary,
        body.confirmed_fields,
        column_mappings=explicit_mappings,
        context_mappings=context_mappings,
        context_confirmed=body.context_confirmed or None,
        context_entity=body.context_entity or None,
        source="ingestion",
        uploaded_file_id=file_id,
    )
    _confirm_latency_ms = int((time.monotonic() - _t0) * 1000)

    # El rollback de get_db_session deja el archivo en NEEDS_CONFIRMATION para
    # reintentar con mapeo manual.
    try:
        check_nonempty_import(
            counts, updated_summary, body.confirmed_fields, body.context_confirmed
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

    record.parsed_summary_json = compact_summary
    record.processing_status = PROCESSING_STATUS_DONE
    await repo.save(record)

    await pipeline_event_service.emit_event(
        session,
        trace_id=_trace_id,
        tenant_id=tenant.tenant_id,
        stage=STAGE_CONFIRM,
        file_id=file_id,
        rows_out=counts["ventas"] + counts["gastos"] + counts["productos"],
        latency_ms=_confirm_latency_ms,
        detail={"imported_counts": counts, "confirmed_fields": body.confirmed_fields},
    )

    logger.info(
        "ingestion.confirm.done",
        file_id=str(file_id),
        ventas=counts["ventas"],
        gastos=counts["gastos"],
        productos=counts["productos"],
    )

    # Enqueue score recalculation — BSL will aggregate newly confirmed data
    from app.application.services.score_trigger_service import (  # noqa: PLC0415
        trigger_score_recalculation,
    )

    try:
        trigger_score_recalculation.delay(str(tenant.tenant_id), str(file_id))
    except Exception:
        logger.warning("ingestion.confirm.score_trigger_failed", file_id=str(file_id))

    parts: list[str] = []
    if counts["ventas"]:
        parts.append(f"{counts['ventas']} venta(s)")
    if counts["gastos"]:
        parts.append(f"{counts['gastos']} gasto(s)")
    if counts["productos"]:
        parts.append(f"{counts['productos']} producto(s)")

    message = (
        f"Importados: {', '.join(parts)}. La puntuación será recalculada."
        if parts
        else "Datos confirmados. La puntuación de salud será recalculada."
    )

    return ConfirmIngestionResponse(
        file_id=record.id,
        status=PROCESSING_STATUS_DONE,
        message=message,
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
    dependencies=[Depends(require_role("OWNER", "ADMIN"))],
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

    run = await reread_service.get_reread_run(session, run_id, tenant.tenant_id)
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
    )
