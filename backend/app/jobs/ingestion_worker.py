"""
Celery workers: file ingestion pipeline.

Three tasks, one per file category:
  - process_spreadsheet  : xlsx / csv
  - process_text_document: txt / docx
  - process_image_ocr    : jpg / png / heic

All tasks follow the same contract:
  1. Load UploadedFile record from DB (fail if not found).
  2. Set processing_status = PROCESSING.
  3. Download content from S3.
  4. Parse / extract data.
  5. Save parsed_summary_json + final processing_status to DB.
  6. Commit.

Confidence levels:  HIGH | MEDIUM | LOW
processing_status after parse: NEEDS_CONFIRMATION (always — human reviews before import).
On unrecoverable error: processing_status = FAILED.
"""

import asyncio
import time
import uuid as _uuid
from typing import Any

from app.application.services import pipeline_event_service
from app.application.services.file_parsing import (
    analyze_headers,
    extract_amounts_from_text,
    parse_uploaded_content,
)
from app.application.services.validation_gate import ValidationGate
from app.jobs.celery_app import celery_app
from app.observability.logger import bind_request_context, get_logger, log_job
from app.observability.metrics import track_job_event
from app.persistence.models.file import (
    PROCESSING_STATUS_FAILED,
    PROCESSING_STATUS_NEEDS_CONFIRMATION,
    PROCESSING_STATUS_PROCESSING,
    PROCESSING_STATUS_REJECTED,
)
from app.persistence.models.pipeline_event import STAGE_VALIDATE

logger = get_logger(__name__)


def _analyze_headers(headers: list[str]) -> dict[str, Any]:
    """Backward-compatible wrapper for legacy imports in tests and workers."""
    return analyze_headers(headers)


def _extract_amounts_from_text(text: str) -> dict[str, Any]:
    """Backward-compatible wrapper for legacy imports in tests and workers."""
    return extract_amounts_from_text(text)


# ── Shared async helpers ──────────────────────────────────────────────────────


async def _load_and_lock(session: Any, file_id: str, tenant_id: str) -> Any:
    """Load UploadedFile and set status=PROCESSING. Returns the ORM object."""
    from sqlalchemy import select  # noqa: PLC0415

    from app.persistence.models.file import UploadedFile  # noqa: PLC0415

    result = await session.execute(
        select(UploadedFile).where(
            UploadedFile.id == _uuid.UUID(file_id),
            UploadedFile.tenant_id == _uuid.UUID(tenant_id),
        )
    )
    record = result.scalar_one_or_none()
    if record is None:
        raise ValueError(f"UploadedFile {file_id} not found for tenant {tenant_id}")
    # FASE 0: bindear trace_id del record (los contextvars no cruzan el boundary de Celery).
    if record.trace_id is not None:
        bind_request_context(trace_id=record.trace_id)
    record.processing_status = PROCESSING_STATUS_PROCESSING
    await session.flush()
    return record


async def _save_result(
    session: Any,
    record: Any,
    summary: dict[str, Any],
    processing_status: str,
    *,
    emit_stage: str | None = None,
) -> None:
    record.parsed_summary_json = summary
    record.processing_status = processing_status
    await session.flush()
    if emit_stage is not None:
        await pipeline_event_service.emit_event(
            session,
            trace_id=record.trace_id or record.id,
            tenant_id=record.tenant_id,
            stage=emit_stage,
            file_id=record.id,
            rows_out=summary.get("row_count") or summary.get("rows_processed"),
            confidence=summary.get("confidence"),
            detail={"passed": True, "file_type": summary.get("file_type")},
        )


def _build_async_session(database_url: str) -> Any:
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: PLC0415
    from sqlalchemy.orm import sessionmaker  # noqa: PLC0415

    from app.config.settings import get_settings  # noqa: PLC0415

    engine = create_async_engine(
        database_url,
        pool_pre_ping=True,
        connect_args=get_settings().pg_connect_args,
    )
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)  # type: ignore[call-overload]
    return engine, factory


async def _apply_validation_gate(
    factory: Any,
    file_id: str,
    tenant_id: str,
    summary: dict[str, Any],
    t0: float,
    task_name: str,
    force: bool = False,
) -> dict[str, Any] | None:
    """
    Runs ValidationGate on the parsed summary.
    Returns the (possibly corrected) summary if it passed, or None if REJECTED.
    Persists REJECTED status + rejection_reason to DB if gate fails.
    """
    gate = ValidationGate()
    result = gate.validate(summary, force=force)

    if not result.passed:
        async with factory() as session:
            record = await _load_and_lock(session, file_id, tenant_id)
            record.processing_status = PROCESSING_STATUS_REJECTED
            record.rejection_reason = result.rejection_reason
            record.parsed_summary_json = summary
            await pipeline_event_service.emit_event(
                session,
                trace_id=record.trace_id or record.id,
                tenant_id=record.tenant_id,
                stage=STAGE_VALIDATE,
                file_id=record.id,
                rows_rejected=summary.get("row_count"),
                detail={"passed": False, "reason": result.rejection_reason},
            )
            await track_job_event(
                session,
                task_name,
                _uuid.UUID(tenant_id),
                success=False,
                duration_ms=int((time.monotonic() - t0) * 1000),
                error=f"REJECTED:{result.rejection_reason}",
            )
            await session.commit()
        logger.info(
            "ingestion.validation_gate.rejected",
            file_id=file_id,
            tenant_id=tenant_id,
            reason=result.rejection_reason,
        )
        return None

    return result.corrected_summary if result.corrected_summary is not None else summary


# ── Celery tasks ──────────────────────────────────────────────────────────────


@celery_app.task(  # type: ignore[misc]
    name="jobs.process_spreadsheet",
    queue="ingestion",
    max_retries=3,
    default_retry_delay=30,
    soft_time_limit=150,
    time_limit=180,
)
def process_spreadsheet(file_id: str, tenant_id: str, force: bool = False) -> None:
    """Parse .xlsx or .csv file and extract ventas/gastos/productos."""
    from app.config.settings import get_settings  # noqa: PLC0415

    s = get_settings()

    async def _run() -> None:
        from app.integrations.s3 import S3Client  # noqa: PLC0415

        engine, factory = _build_async_session(s.DATABASE_URL)
        t0 = time.monotonic()
        try:
            with log_job("jobs.process_spreadsheet", tenant_id=tenant_id, logger=logger):
                async with factory() as session:
                    record = await _load_and_lock(session, file_id, tenant_id)
                    await session.commit()

                # Download from S3
                s3 = S3Client()
                content = await s3.download(record.s3_key)

                # Parse
                summary = parse_uploaded_content(
                    content,
                    record.content_type,
                    record.original_filename,
                )

                # Validation gate — REJECTED if confidence too low or schema invalid
                validated_summary = await _apply_validation_gate(
                    factory,
                    file_id,
                    tenant_id,
                    summary,
                    t0,
                    "jobs.process_spreadsheet",
                    force=force,
                )
                if validated_summary is None:
                    return  # REJECTED — already persisted

                async with factory() as session:
                    result_record = await _load_and_lock(session, file_id, tenant_id)
                    await _save_result(
                        session,
                        result_record,
                        validated_summary,
                        PROCESSING_STATUS_NEEDS_CONFIRMATION,
                        emit_stage=STAGE_VALIDATE,
                    )
                    await track_job_event(
                        session,
                        "jobs.process_spreadsheet",
                        _uuid.UUID(tenant_id),
                        success=True,
                        duration_ms=int((time.monotonic() - t0) * 1000),
                    )
                    await session.commit()

                logger.info(
                    "ingestion.spreadsheet.done",
                    file_id=file_id,
                    tenant_id=tenant_id,
                    confidence=validated_summary.get("confidence"),
                    rows=validated_summary.get("rows_processed"),
                )

        except Exception as exc:
            logger.error(
                "ingestion.spreadsheet.failed",
                file_id=file_id,
                tenant_id=tenant_id,
                error=str(exc),
            )
            async with factory() as session:
                result_record = await _load_and_lock(session, file_id, tenant_id)
                await _save_result(
                    session,
                    result_record,
                    {"error": str(exc), "file_type": "spreadsheet"},
                    PROCESSING_STATUS_FAILED,
                )
                await track_job_event(
                    session,
                    "jobs.process_spreadsheet",
                    _uuid.UUID(tenant_id),
                    success=False,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    error=str(exc),
                )
                await session.commit()
            raise
        finally:
            await engine.dispose()

    asyncio.run(_run())


@celery_app.task(  # type: ignore[misc]
    name="jobs.process_text_document",
    queue="ingestion",
    max_retries=3,
    default_retry_delay=30,
    soft_time_limit=150,
    time_limit=180,
)
def process_text_document(file_id: str, tenant_id: str, force: bool = False) -> None:
    """Parse .txt, .docx, .pdf or .pptx file, extracting reusable context."""
    from app.config.settings import get_settings  # noqa: PLC0415

    s = get_settings()

    async def _run() -> None:
        from app.integrations.s3 import S3Client  # noqa: PLC0415

        engine, factory = _build_async_session(s.DATABASE_URL)
        t0 = time.monotonic()
        try:
            with log_job("jobs.process_text_document", tenant_id=tenant_id, logger=logger):
                async with factory() as session:
                    record = await _load_and_lock(session, file_id, tenant_id)
                    await session.commit()

                s3 = S3Client()
                content = await s3.download(record.s3_key)

                summary = parse_uploaded_content(
                    content,
                    record.content_type,
                    record.original_filename,
                )

                # Validation gate
                validated_summary = await _apply_validation_gate(
                    factory,
                    file_id,
                    tenant_id,
                    summary,
                    t0,
                    "jobs.process_text_document",
                    force=force,
                )
                if validated_summary is None:
                    return

                async with factory() as session:
                    result_record = await _load_and_lock(session, file_id, tenant_id)
                    await _save_result(
                        session,
                        result_record,
                        validated_summary,
                        PROCESSING_STATUS_NEEDS_CONFIRMATION,
                        emit_stage=STAGE_VALIDATE,
                    )
                    await track_job_event(
                        session,
                        "jobs.process_text_document",
                        _uuid.UUID(tenant_id),
                        success=True,
                        duration_ms=int((time.monotonic() - t0) * 1000),
                    )
                    await session.commit()

                logger.info(
                    "ingestion.text_document.done",
                    file_id=file_id,
                    tenant_id=tenant_id,
                    source_format=validated_summary.get("source_format"),
                    row_count=validated_summary.get("row_count"),
                )

        except Exception as exc:
            logger.error(
                "ingestion.text_document.failed",
                file_id=file_id,
                tenant_id=tenant_id,
                error=str(exc),
            )
            async with factory() as session:
                result_record = await _load_and_lock(session, file_id, tenant_id)
                await _save_result(
                    session,
                    result_record,
                    {"error": str(exc), "file_type": "text"},
                    PROCESSING_STATUS_FAILED,
                )
                await track_job_event(
                    session,
                    "jobs.process_text_document",
                    _uuid.UUID(tenant_id),
                    success=False,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    error=str(exc),
                )
                await session.commit()
            raise
        finally:
            await engine.dispose()

    asyncio.run(_run())


@celery_app.task(  # type: ignore[misc]
    name="jobs.process_image_ocr",
    queue="ingestion",
    max_retries=3,
    default_retry_delay=30,
    soft_time_limit=150,
    time_limit=180,
)
def process_image_ocr(file_id: str, tenant_id: str, force: bool = False) -> None:
    """
    Run OCR on an image file (.jpg, .png, .heic).

    Confidence is always LOW — never auto-imports without user confirmation.
    If pytesseract is not available, marks file NEEDS_CONFIRMATION with an
    explicit error message so the user can review manually.
    """
    from app.config.settings import get_settings  # noqa: PLC0415

    s = get_settings()

    async def _run() -> None:
        from app.integrations.s3 import S3Client  # noqa: PLC0415

        engine, factory = _build_async_session(s.DATABASE_URL)
        t0 = time.monotonic()
        try:
            with log_job("jobs.process_image_ocr", tenant_id=tenant_id, logger=logger):
                async with factory() as session:
                    record = await _load_and_lock(session, file_id, tenant_id)
                    await session.commit()

                s3 = S3Client()
                content = await s3.download(record.s3_key)

                summary = parse_uploaded_content(
                    content,
                    record.content_type,
                    record.original_filename,
                )

                # Validation gate — images are always LOW confidence, REJECTED unless force=True
                validated_summary = await _apply_validation_gate(
                    factory, file_id, tenant_id, summary, t0, "jobs.process_image_ocr", force=force
                )
                if validated_summary is None:
                    return

                # ALWAYS NEEDS_CONFIRMATION for images — never auto-import
                async with factory() as session:
                    result_record = await _load_and_lock(session, file_id, tenant_id)
                    await _save_result(
                        session,
                        result_record,
                        validated_summary,
                        PROCESSING_STATUS_NEEDS_CONFIRMATION,
                        emit_stage=STAGE_VALIDATE,
                    )
                    await track_job_event(
                        session,
                        "jobs.process_image_ocr",
                        _uuid.UUID(tenant_id),
                        success=True,
                        duration_ms=int((time.monotonic() - t0) * 1000),
                    )
                    await session.commit()

                logger.info(
                    "ingestion.image_ocr.done",
                    file_id=file_id,
                    tenant_id=tenant_id,
                    has_ocr="error" not in validated_summary,
                )

        except Exception as exc:
            logger.error(
                "ingestion.image_ocr.failed",
                file_id=file_id,
                tenant_id=tenant_id,
                error=str(exc),
            )
            async with factory() as session:
                result_record = await _load_and_lock(session, file_id, tenant_id)
                await _save_result(
                    session,
                    result_record,
                    {"error": str(exc), "file_type": "image", "confidence": "LOW"},
                    PROCESSING_STATUS_FAILED,
                )
                await track_job_event(
                    session,
                    "jobs.process_image_ocr",
                    _uuid.UUID(tenant_id),
                    success=False,
                    duration_ms=int((time.monotonic() - t0) * 1000),
                    error=str(exc),
                )
                await session.commit()
            raise
        finally:
            await engine.dispose()

    asyncio.run(_run())
