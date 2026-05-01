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

from app.application.services.file_parsing import (
    analyze_headers,
    extract_amounts_from_text,
    parse_uploaded_content,
)
from app.jobs.celery_app import celery_app
from app.observability.logger import get_logger, log_job
from app.observability.metrics import track_job_event
from app.persistence.models.file import (
    PROCESSING_STATUS_FAILED,
    PROCESSING_STATUS_NEEDS_CONFIRMATION,
    PROCESSING_STATUS_PROCESSING,
)

logger = get_logger(__name__)


def _analyze_headers(headers: list[str]) -> dict[str, Any]:
    """Backward-compatible wrapper for legacy imports in tests and workers."""
    return analyze_headers(headers)


def _extract_amounts_from_text(text: str) -> dict[str, Any]:
    """Backward-compatible wrapper for legacy imports in tests and workers."""
    return extract_amounts_from_text(text)


# ── Shared async helpers ──────────────────────────────────────────────────────

async def _load_and_lock(
    session: Any, file_id: str, tenant_id: str
) -> Any:
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
    record.processing_status = PROCESSING_STATUS_PROCESSING
    await session.flush()
    return record


async def _save_result(
    session: Any,
    record: Any,
    summary: dict[str, Any],
    processing_status: str,
) -> None:
    record.parsed_summary_json = summary
    record.processing_status = processing_status
    await session.flush()


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


# ── Celery tasks ──────────────────────────────────────────────────────────────

@celery_app.task(  # type: ignore[misc]
    name="jobs.process_spreadsheet",
    queue="ingestion",
    max_retries=3,
    default_retry_delay=30,
    soft_time_limit=150,
    time_limit=180,
)
def process_spreadsheet(file_id: str, tenant_id: str) -> None:
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

                async with factory() as session:
                    result_record = await _load_and_lock(session, file_id, tenant_id)
                    await _save_result(
                        session,
                        result_record,
                        summary,
                        PROCESSING_STATUS_NEEDS_CONFIRMATION,
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
                    confidence=summary.get("confidence"),
                    rows=summary.get("rows_processed"),
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
def process_text_document(file_id: str, tenant_id: str) -> None:
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

                async with factory() as session:
                    result_record = await _load_and_lock(session, file_id, tenant_id)
                    await _save_result(
                        session,
                        result_record,
                        summary,
                        PROCESSING_STATUS_NEEDS_CONFIRMATION,
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
                    source_format=summary.get("source_format"),
                    row_count=summary.get("row_count"),
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
def process_image_ocr(file_id: str, tenant_id: str) -> None:
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

                # ALWAYS NEEDS_CONFIRMATION for images — never auto-import
                async with factory() as session:
                    result_record = await _load_and_lock(session, file_id, tenant_id)
                    await _save_result(
                        session,
                        result_record,
                        summary,
                        PROCESSING_STATUS_NEEDS_CONFIRMATION,
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
                    has_ocr="error" not in summary,
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
