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
import re
import uuid as _uuid
from typing import Any

from app.jobs.celery_app import celery_app
from app.observability.logger import get_logger
from app.persistence.models.file import (
    PROCESSING_STATUS_FAILED,
    PROCESSING_STATUS_NEEDS_CONFIRMATION,
    PROCESSING_STATUS_PROCESSING,
)

logger = get_logger(__name__)

# ── Keyword sets for column/context detection ─────────────────────────────────
_VENTA_COLS = {"precio", "precio_venta", "venta", "ventas", "ingreso", "monto", "importe", "total"}
_GASTO_COLS = {"costo", "gasto", "gastos", "egreso", "compra", "deuda", "pago", "proveedor"}
_PRODUCTO_COLS = {"producto", "descripcion", "nombre", "sku", "codigo", "stock", "inventario", "articulo", "item"}
_FECHA_COLS = {"fecha", "date", "dia", "mes", "periodo"}

_VENTA_CTX = {"venta", "ingreso", "cobro", "ticket", "recibo", "pago recibido", "cobrado"}
_GASTO_CTX = {"gasto", "compra", "pago", "factura", "proveedor", "egreso", "gaste"}
_STOCK_CTX = {"stock", "inventario", "unidades", "cantidad", "mercaderia", "mercadería"}

_AMOUNT_RE = re.compile(r"\$\s*[\d.,]+")


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


# ── Spreadsheet columns analysis ─────────────────────────────────────────────

def _analyze_headers(headers: list[str]) -> dict[str, Any]:
    """Classify headers and determine confidence."""
    normalized = [h.lower().strip().replace(" ", "_") for h in headers]

    has_fecha = any(any(k in col for k in _FECHA_COLS) for col in normalized)
    has_venta = any(any(k in col for k in _VENTA_COLS) for col in normalized)
    has_gasto = any(any(k in col for k in _GASTO_COLS) for col in normalized)
    has_producto = any(any(k in col for k in _PRODUCTO_COLS) for col in normalized)

    recognized = sum([has_fecha, has_venta, has_gasto, has_producto])
    confidence = "HIGH" if (has_fecha and has_venta) else ("MEDIUM" if recognized >= 2 else "MEDIUM")

    return {
        "has_fecha": has_fecha,
        "has_venta": has_venta,
        "has_gasto": has_gasto,
        "has_producto": has_producto,
        "confidence": confidence,
    }


def _rows_to_dicts(headers: list[str], rows: list[list[Any]]) -> list[dict[str, Any]]:
    return [
        {h: (str(v) if v is not None else None) for h, v in zip(headers, row)}
        for row in rows
    ]


# ── Text amount extraction ────────────────────────────────────────────────────

def _classify_line(line: str) -> str:
    low = line.lower()
    if any(k in low for k in _VENTA_CTX):
        return "venta"
    if any(k in low for k in _GASTO_CTX):
        return "gasto"
    if any(k in low for k in _STOCK_CTX):
        return "stock"
    return "desconocido"


def _extract_amounts_from_text(text: str) -> dict[str, Any]:
    lines = text.splitlines()
    ventas: list[dict[str, str]] = []
    gastos: list[dict[str, str]] = []
    stock: list[dict[str, str]] = []

    for line in lines:
        matches = _AMOUNT_RE.findall(line)
        if not matches:
            continue
        category = _classify_line(line)
        entry = {"linea": line.strip(), "montos": matches}
        if category == "venta":
            ventas.append(entry)
        elif category == "gasto":
            gastos.append(entry)
        elif category == "stock":
            stock.append(entry)
        else:
            ventas.append(entry)  # default: assume sale context

    return {
        "ventas_detectadas": ventas,
        "gastos_detectados": gastos,
        "stock_detectado": stock,
    }


# ── Celery tasks ──────────────────────────────────────────────────────────────

@celery_app.task(  # type: ignore[misc]
    name="jobs.process_spreadsheet",
    queue="ingestion",
    max_retries=3,
    default_retry_delay=30,
)
def process_spreadsheet(file_id: str, tenant_id: str) -> None:
    """Parse .xlsx or .csv file and extract ventas/gastos/productos."""
    from app.config.settings import get_settings  # noqa: PLC0415

    s = get_settings()

    async def _run() -> None:
        import csv  # noqa: PLC0415
        import io  # noqa: PLC0415

        import openpyxl  # noqa: PLC0415

        from app.integrations.s3 import S3Client  # noqa: PLC0415

        engine, factory = _build_async_session(s.DATABASE_URL)
        try:
            async with factory() as session:
                record = await _load_and_lock(session, file_id, tenant_id)
                await session.commit()

            # Download from S3
            s3 = S3Client()
            content = await s3.download(record.s3_key)

            # Parse
            summary: dict[str, Any] = {"file_type": "spreadsheet", "warnings": []}
            is_csv = record.content_type == "text/csv" or record.original_filename.endswith(".csv")

            if is_csv:
                text = content.decode("utf-8", errors="replace")
                reader = csv.reader(io.StringIO(text))
                rows = list(reader)
                if not rows:
                    summary.update({"confidence": "MEDIUM", "ventas_detectadas": [], "rows_processed": 0})
                else:
                    headers = rows[0]
                    data_rows = rows[1:]
                    analysis = _analyze_headers(headers)
                    summary.update(analysis)
                    summary["headers"] = headers
                    summary["rows_processed"] = len(data_rows)
                    summary["ventas_detectadas"] = _rows_to_dicts(headers, data_rows[:50])
            else:
                # xlsx
                wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
                ws = wb.active
                all_rows = list(ws.iter_rows(values_only=True))  # type: ignore[union-attr]
                if not all_rows:
                    summary.update({"confidence": "MEDIUM", "ventas_detectadas": [], "rows_processed": 0})
                else:
                    headers = [str(c) if c is not None else f"col_{i}" for i, c in enumerate(all_rows[0])]
                    data_rows_raw = [list(r) for r in all_rows[1:]]
                    analysis = _analyze_headers(headers)
                    summary.update(analysis)
                    summary["headers"] = headers
                    summary["rows_processed"] = len(data_rows_raw)
                    summary["ventas_detectadas"] = _rows_to_dicts(headers, data_rows_raw[:50])
                wb.close()

            async with factory() as session:
                result_record = await _load_and_lock(session, file_id, tenant_id)
                await _save_result(session, result_record, summary, PROCESSING_STATUS_NEEDS_CONFIRMATION)
                await session.commit()

            logger.info(
                "ingestion.spreadsheet.done",
                file_id=file_id,
                confidence=summary.get("confidence"),
                rows=summary.get("rows_processed"),
            )

        except Exception as exc:
            logger.error("ingestion.spreadsheet.failed", file_id=file_id, error=str(exc))
            async with factory() as session:
                result_record = await _load_and_lock(session, file_id, tenant_id)
                await _save_result(
                    session,
                    result_record,
                    {"error": str(exc), "file_type": "spreadsheet"},
                    PROCESSING_STATUS_FAILED,
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
)
def process_text_document(file_id: str, tenant_id: str) -> None:
    """Parse .txt or .docx file, extract amounts with regex."""
    from app.config.settings import get_settings  # noqa: PLC0415

    s = get_settings()

    async def _run() -> None:
        import io  # noqa: PLC0415

        from app.integrations.s3 import S3Client  # noqa: PLC0415

        engine, factory = _build_async_session(s.DATABASE_URL)
        try:
            async with factory() as session:
                record = await _load_and_lock(session, file_id, tenant_id)
                await session.commit()

            s3 = S3Client()
            content = await s3.download(record.s3_key)

            # Extract text
            is_docx = (
                record.content_type
                == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                or record.original_filename.endswith(".docx")
            )
            if is_docx:
                import docx  # noqa: PLC0415

                doc = docx.Document(io.BytesIO(content))
                raw_text = "\n".join(p.text for p in doc.paragraphs)
            else:
                raw_text = content.decode("utf-8", errors="replace")

            amounts = _extract_amounts_from_text(raw_text)
            summary: dict[str, Any] = {
                "file_type": "text",
                "confidence": "MEDIUM",
                "raw_text_preview": raw_text[:500],
                **amounts,
            }

            async with factory() as session:
                result_record = await _load_and_lock(session, file_id, tenant_id)
                await _save_result(session, result_record, summary, PROCESSING_STATUS_NEEDS_CONFIRMATION)
                await session.commit()

            logger.info(
                "ingestion.text_document.done",
                file_id=file_id,
                ventas=len(amounts["ventas_detectadas"]),
                gastos=len(amounts["gastos_detectados"]),
            )

        except Exception as exc:
            logger.error("ingestion.text_document.failed", file_id=file_id, error=str(exc))
            async with factory() as session:
                result_record = await _load_and_lock(session, file_id, tenant_id)
                await _save_result(
                    session,
                    result_record,
                    {"error": str(exc), "file_type": "text"},
                    PROCESSING_STATUS_FAILED,
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
        import io  # noqa: PLC0415

        from app.integrations.s3 import S3Client  # noqa: PLC0415

        engine, factory = _build_async_session(s.DATABASE_URL)
        try:
            async with factory() as session:
                record = await _load_and_lock(session, file_id, tenant_id)
                await session.commit()

            s3 = S3Client()
            content = await s3.download(record.s3_key)

            summary: dict[str, Any] = {
                "file_type": "image",
                "confidence": "LOW",
            }

            # Attempt OCR — graceful fallback if pytesseract / tesseract unavailable
            try:
                import pytesseract  # noqa: PLC0415
                from PIL import Image, UnidentifiedImageError  # noqa: PLC0415

                try:
                    img = Image.open(io.BytesIO(content))
                    raw_text = pytesseract.image_to_string(img, lang="spa+eng")
                except UnidentifiedImageError:
                    raw_text = ""
                    summary["warnings"] = ["No se pudo abrir la imagen (formato no soportado por Pillow)."]

                amounts = _extract_amounts_from_text(raw_text)
                summary.update(
                    {
                        "raw_text_preview": raw_text[:500],
                        **amounts,
                    }
                )

            except ImportError:
                summary["error"] = "OCR no disponible en este entorno"
                summary["ventas_detectadas"] = []
                summary["gastos_detectados"] = []
                summary["stock_detectado"] = []

            # ALWAYS NEEDS_CONFIRMATION for images — never auto-import
            async with factory() as session:
                result_record = await _load_and_lock(session, file_id, tenant_id)
                await _save_result(session, result_record, summary, PROCESSING_STATUS_NEEDS_CONFIRMATION)
                await session.commit()

            logger.info("ingestion.image_ocr.done", file_id=file_id, has_ocr="error" not in summary)

        except Exception as exc:
            logger.error("ingestion.image_ocr.failed", file_id=file_id, error=str(exc))
            async with factory() as session:
                result_record = await _load_and_lock(session, file_id, tenant_id)
                await _save_result(
                    session,
                    result_record,
                    {"error": str(exc), "file_type": "image", "confidence": "LOW"},
                    PROCESSING_STATUS_FAILED,
                )
                await session.commit()
            raise
        finally:
            await engine.dispose()

    asyncio.run(_run())
