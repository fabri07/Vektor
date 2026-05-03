"""
Ingestion pipeline endpoints.

POST   /ingestion/upload                — upload file, enqueue parsing job
GET    /ingestion/files                 — list files for current tenant
GET    /ingestion/files/{file_id}/preview   — get parsed_summary_json
POST   /ingestion/files/{file_id}/confirm  — confirm import (NEEDS_CONFIRMATION only)
"""

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_tenant, get_current_user
from app.application.services.file_parsing import (
    IMAGE_MIMES as _IMAGE_MIMES,
)
from app.application.services.file_parsing import (
    SPREADSHEET_MIMES as _SPREADSHEET_MIMES,
)
from app.application.services.file_parsing import (
    detect_supported_mime,
    parse_uploaded_content,
    sanitize_filename,
)
from app.application.services.ingestion_import_service import insert_confirmed_data
from app.config.settings import get_settings
from app.integrations.s3 import S3Client
from app.jobs.ingestion_worker import (
    process_image_ocr,
    process_spreadsheet,
    process_text_document,
)
from app.main import limiter
from app.observability.logger import get_logger
from app.persistence.db.session import get_db_session
from app.persistence.models.file import (
    PROCESSING_STATUS_DONE,
    PROCESSING_STATUS_FAILED,
    PROCESSING_STATUS_NEEDS_CONFIRMATION,
    PROCESSING_STATUS_PENDING,
    PROCESSING_STATUS_PROCESSING,
    PROCESSING_STATUS_REJECTED,
    UploadedFile,
)
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.persistence.repositories.file_repository import FileRepository
from app.schemas.ingestion import (
    ConfirmIngestionRequest,
    ConfirmIngestionResponse,
    FilePreviewResponse,
    FileStatusItem,
    UploadResponse,
)

router = APIRouter()

logger = get_logger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

FileHint = Literal["ventas", "gastos", "stock", "general"]


def _pick_job(mime: str) -> object:
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

    repo = FileRepository(session)
    try:
        record.processing_status = PROCESSING_STATUS_PROCESSING
        await repo.save(record)
        await session.flush()

        s3 = S3Client()
        content = await s3.download(record.s3_key)
        summary = parse_uploaded_content(content, record.content_type, record.original_filename)

        gate = ValidationGate()
        gate_result = gate.validate(summary, force=force)

        if not gate_result.passed:
            record.processing_status = PROCESSING_STATUS_REJECTED
            record.rejection_reason = gate_result.rejection_reason
            record.parsed_summary_json = summary
            await repo.save(record)
            logger.info(
                "ingestion.sync_fallback.rejected",
                file_id=str(record.id),
                reason=gate_result.rejection_reason,
            )
            return

        final_summary = gate_result.corrected_summary if gate_result.corrected_summary else summary
        record.parsed_summary_json = final_summary
        record.processing_status = PROCESSING_STATUS_NEEDS_CONFIRMATION
        await repo.save(record)

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
            detail="El archivo supera el tamaño máximo de 10 MB.",
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
    )
    repo = FileRepository(session)
    saved = await repo.save(record)

    if get_settings().USE_LOCAL_FALLBACK:
        await _process_file_sync(saved, session, force=force)
        return UploadResponse(file_id=saved.id, status="PROCESSING")

    # Enqueue parsing job — fall back to sync processing if Celery/Redis
    # is unavailable (beta: single Railway service without workers).
    job = _pick_job(detected_mime)
    try:
        job.delay(str(saved.id), str(tenant.tenant_id), force)  # type: ignore[attr-defined]
    except Exception:
        logger.warning(
            "ingestion.celery_unavailable",
            file_id=str(saved.id),
            msg="Celery/Redis no disponible, procesando archivo de forma síncrona.",
        )
        await _process_file_sync(saved, session, force=force)
        return UploadResponse(file_id=saved.id, status="PROCESSING")

    return UploadResponse(file_id=saved.id, status="PROCESSING")


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

    return FilePreviewResponse(
        file_id=record.id,
        processing_status=record.processing_status,
        parsed_summary_json=record.parsed_summary_json,
    )


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

    try:
        s3 = S3Client()
        await s3.delete(record.s3_key)
    except Exception as exc:
        logger.warning("ingestion.delete.s3_failed", file_id=str(file_id), error=str(exc))

    await repo.delete(record)
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
) -> dict:
    repo = FileRepository(session)
    record = await repo.get_by_id(file_id, tenant.tenant_id)
    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Archivo no encontrado.")

    if record.processing_status not in (PROCESSING_STATUS_PENDING, PROCESSING_STATUS_FAILED):
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

    # Insert parsed rows into business tables, then mark done
    updated_summary = dict(record.parsed_summary_json or {})
    updated_summary["confirmed_fields"] = body.confirmed_fields

    counts = await insert_confirmed_data(
        session,
        tenant.tenant_id,
        updated_summary,
        body.confirmed_fields,
    )
    updated_summary["imported_counts"] = counts

    record.parsed_summary_json = updated_summary
    record.processing_status = PROCESSING_STATUS_DONE
    await repo.save(record)

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
