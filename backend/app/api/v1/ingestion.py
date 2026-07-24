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
    REQUIRED_FIELDS,
    ColumnMappingService,
    validate_required_date_mapping,
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
from app.application.services.ingestion_lease_service import (
    ImportLeaseLostError,
    acquire_import_lease,
    finalize_import_lease,
    release_import_lease,
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
    ConfirmIngestionRequest,
    ConfirmIngestionResponse,
    DropColumnsRequest,
    FilePreviewResponse,
    FileStatusItem,
    MasterPreviewSample,
    MasterPreviewSummary,
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

    # F7d: preview de maestros — best-effort, nunca debe romper el preview del
    # archivo (es un diagnóstico adicional, no el dato principal de la respuesta).
    master_previews: list[MasterPreviewSummary] = []
    try:
        master_previews = await _build_master_previews(
            session, tenant.tenant_id, record.parsed_summary_json or {}
        )
    except Exception:
        logger.warning("ingestion.preview.master_preview_failed", file_id=str(file_id))

    return FilePreviewResponse(
        file_id=record.id,
        processing_status=record.processing_status,
        parsed_summary_json=record.parsed_summary_json,
        columns_at_risk=columns_at_risk,
        master_previews=master_previews,
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
        description="Tipo de entidad: sale | expense | product | customer | supplier",
    ),
    context_id: str | None = Query(
        default=None,
        description="Contexto (hoja/tabla) en archivos multi-contexto. Si se da, "
        "se usan sus headers/preview y su entity_type (se ignora el param entity_type).",
    ),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session),
) -> list[ColumnMappingSuggestion]:
    # F7d: "customer"/"supplier" sumados — sin esto, un archivo flat (legacy, sin
    # mapping_contexts) de clientes/proveedores no podía pedir sugerencias de
    # mapeo (context_id resuelve el entity_type real igual, pero el query param
    # por default "sale" ya rebotaba acá antes de llegar a esa resolución).
    if entity_type not in ("sale", "expense", "product", "inventory", "customer", "supplier"):
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
    dependencies=[Depends(ensure_tenant_not_under_maintenance)],
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
            # F7d: clientes/proveedores se incluyen/excluyen como los demás buckets.
            or (entity_type == "customer" and body.confirmed_fields.get("clientes"))
            or (entity_type == "supplier" and body.confirmed_fields.get("proveedores"))
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
            or (body.confirmed_fields.get("clientes") and _entity_type == "customer")
            or (body.confirmed_fields.get("proveedores") and _entity_type == "supplier")
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
            _override = body.context_entity or {}
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

    # ── F4: tomar el lease per-file ANTES de cualquier escritura ────────────────
    # CAS atómico NEEDS_CONFIRMATION→IMPORTING (o takeover si quedó stale),
    # commiteado sobre la sesión del request → el IMPORTING queda visible para un
    # confirm concurrente (que bloquea en el row-lock y luego ve IMPORTING → 409).
    # rowcount==0 → otro intento tiene el lease vivo → 409. Se toma DESPUÉS de las
    # validaciones puras (una request que va a rebotar por 422 nunca lo toma) y
    # ANTES de la creación de custom fields (primera escritura).
    _import_token = uuid.uuid4()
    if not await acquire_import_lease(session, tenant.tenant_id, file_id, _import_token):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="El archivo ya se está importando o ya se importó.",
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
        # Persistir la elección de tratamiento del stock (apertura vs compra) en el summary
        # para que una relectura posterior conserve la decisión sin volver a preguntar.
        if body.stock_treatment is not None:
            updated_summary["stock_treatment"] = body.stock_treatment

        explicit_mappings: dict[str, str] | None = None
        if _flat_mappings:
            explicit_mappings = {m.source_column: m.target_field for m in _flat_mappings}

        context_mappings: dict[str, dict[str, str]] | None = None
        if _ctx_mappings:
            _cm: dict[str, dict[str, str]] = defaultdict(dict)
            for m in _ctx_mappings:
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
            if _context_entity.get(cid) in ("customer", "supplier")
        }
        _master_flat_mapping = (
            explicit_mappings if _entity_type in ("customer", "supplier") else None
        )
        if _master_context_mappings or _master_flat_mapping:
            updated_summary["master_column_mappings"] = {
                "context": _master_context_mappings,
                "flat": _master_flat_mapping,
            }

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
            stock_treatment=body.stock_treatment,
        )
        _confirm_latency_ms = int((time.monotonic() - _t0) * 1000)

        # Import vacío → 422; el compensador (except) restaura NEEDS_CONFIRMATION
        # y limpia el lease para reintentar con mapeo manual.
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

        # Transición final IMPORTING→DONE, token-checked, en la MISMA transacción
        # que los inserts. Si un takeover nos robó el lease → ImportLeaseLostError
        # → rollback de todo (no queda dato a medias).
        await finalize_import_lease(
            session, tenant.tenant_id, file_id, _import_token, compact_summary
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
            detail={"imported_counts": counts, "confirmed_fields": body.confirmed_fields},
        )
        # Import OK: liberar el savepoint (los cambios quedan en la transacción del
        # request, que los commitea al final).
        await _import_sp.commit()
    except ImportLeaseLostError:
        # Un takeover ya tomó el lease con otro token. Descartar nuestro import
        # parcial (rollback del savepoint) y NO compensar (el estado es del nuevo dueño).
        await _import_sp.rollback()
        logger.warning("ingestion.confirm.lease_lost", file_id=str(file_id))
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="El import fue retomado por otro proceso. Volvé a intentar.",
        ) from None
    except Exception:
        # Cualquier fallo post-lease (incl. el 422 de import vacío): revertir el
        # savepoint del import PRIMERO (descarta lo parcial sin tocar la sesión del
        # request) y RECIÉN compensar el lease → NEEDS_CONFIRMATION.
        await _import_sp.rollback()
        await release_import_lease(session, tenant.tenant_id, file_id, _import_token)
        raise

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
    if counts.get("otros"):
        # F1-fix: cubre también los productos con nombre ambiguo (F1) — ya no
        # generan un warning propio, "otros" los cuenta porque la fila ambigua
        # se persiste ahí (evita doble conteo/mensaje solapado).
        warnings.append(
            f"{counts['otros']} fila(s) quedaron en «Otros» para que las revises y clasifiques."
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

    return ConfirmIngestionResponse(
        file_id=record.id,
        status=PROCESSING_STATUS_DONE,
        message=message,
        warnings=warnings,
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
    )
