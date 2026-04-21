"""Shared file parsing helpers for chat uploads and ingestion.

This module centralizes:
  - filename sanitization
  - MIME detection with secure extension fallback
  - canonical parsed_summary_json generation
  - text extraction for supported document formats
"""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path
from typing import Any

import filetype

from app.observability.logger import get_logger

logger = get_logger(__name__)

MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB

SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9.\-_]")

SPREADSHEET_MIMES = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/csv",
}
TEXT_MIMES = {
    "text/plain",
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
IMAGE_MIMES = {
    "image/jpeg",
    "image/png",
    "image/heif",
    "image/heic",
}
ALLOWED_MIMES = SPREADSHEET_MIMES | TEXT_MIMES | IMAGE_MIMES

EXTENSION_TO_MIME = {
    ".csv": "text/csv",
    ".txt": "text/plain",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".heic": "image/heic",
}

SUPPORTED_TYPES_LABEL = "xlsx, csv, txt, pdf, docx, pptx, jpg, png, heic"

VENTA_COLS = {
    "precio",
    "precio_venta",
    "venta",
    "ventas",
    "ingreso",
    "monto",
    "importe",
    "total",
}
GASTO_COLS = {"costo", "gasto", "gastos", "egreso", "compra", "deuda", "pago", "proveedor"}
PRODUCTO_COLS = {
    "producto",
    "descripcion",
    "nombre",
    "sku",
    "codigo",
    "stock",
    "inventario",
    "articulo",
    "item",
}
FECHA_COLS = {"fecha", "date", "dia", "mes", "periodo"}

VENTA_CTX = {"venta", "ingreso", "cobro", "ticket", "recibo", "pago recibido", "cobrado"}
GASTO_CTX = {"gasto", "compra", "pago", "factura", "proveedor", "egreso", "gaste"}
STOCK_CTX = {"stock", "inventario", "unidades", "cantidad", "mercaderia", "mercadería"}

AMOUNT_RE = re.compile(r"\$\s*[\d.,]+")


def sanitize_filename(filename: str) -> str:
    """Remove path traversal and unsafe characters from a filename."""
    filename = filename.replace("\\", "/").split("/")[-1]
    filename = SAFE_FILENAME_RE.sub("_", filename)
    return filename or "upload"


def detect_supported_mime(content: bytes, filename: str) -> str:
    """Detect a supported MIME type from content and filename."""
    kind = filetype.guess(content[:2048])
    ext = Path(filename).suffix.lower()

    detected = kind.mime if kind is not None else ""
    if detected == "application/zip" and ext in EXTENSION_TO_MIME:
        detected = EXTENSION_TO_MIME[ext]

    if not detected and ext in EXTENSION_TO_MIME:
        detected = EXTENSION_TO_MIME[ext]

    if detected == "image/jpg":
        detected = "image/jpeg"

    if detected not in ALLOWED_MIMES:
        raise ValueError(
            f"Tipo de archivo no soportado: {detected or 'desconocido'}. "
            f"Tipos aceptados: {SUPPORTED_TYPES_LABEL}."
        )
    return detected


def infer_source_format(filename: str, mime: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext:
        return ext.lstrip(".")
    if mime == "text/plain":
        return "txt"
    if mime == "text/csv":
        return "csv"
    return mime.rsplit("/", 1)[-1]


def analyze_headers(headers: list[str]) -> dict[str, Any]:
    """Classify headers and determine confidence."""
    normalized = [h.lower().strip().replace(" ", "_") for h in headers]

    has_fecha = any(any(k in col for k in FECHA_COLS) for col in normalized)
    has_venta = any(any(k in col for k in VENTA_COLS) for col in normalized)
    has_gasto = any(any(k in col for k in GASTO_COLS) for col in normalized)
    has_producto = any(any(k in col for k in PRODUCTO_COLS) for col in normalized)

    recognized = sum([has_fecha, has_venta, has_gasto, has_producto])
    confidence = (
        "HIGH"
        if (has_fecha and has_venta)
        else ("MEDIUM" if recognized >= 2 else "MEDIUM")
    )

    return {
        "has_fecha": has_fecha,
        "has_venta": has_venta,
        "has_gasto": has_gasto,
        "has_producto": has_producto,
        "confidence": confidence,
    }


def rows_to_dicts(headers: list[str], rows: list[list[Any]]) -> list[dict[str, Any]]:
    return [
        {h: (str(v) if v is not None else None) for h, v in zip(headers, row, strict=False)}
        for row in rows
    ]


def classify_line(line: str) -> str:
    low = line.lower()
    if any(k in low for k in VENTA_CTX):
        return "venta"
    if any(k in low for k in GASTO_CTX):
        return "gasto"
    if any(k in low for k in STOCK_CTX):
        return "stock"
    return "desconocido"


def extract_amounts_from_text(text: str) -> dict[str, Any]:
    lines = text.splitlines()
    ventas: list[dict[str, str]] = []
    gastos: list[dict[str, str]] = []
    stock: list[dict[str, str]] = []

    for line in lines:
        matches = AMOUNT_RE.findall(line)
        if not matches:
            continue
        category = classify_line(line)
        entry = {"linea": line.strip(), "montos": matches}
        if category == "venta":
            ventas.append(entry)
        elif category == "gasto":
            gastos.append(entry)
        elif category == "stock":
            stock.append(entry)
        else:
            ventas.append(entry)

    return {
        "ventas_detectadas": ventas,
        "gastos_detectados": gastos,
        "stock_detectado": stock,
    }


def parse_uploaded_content(content: bytes, mime: str, filename: str) -> dict[str, Any]:
    """Parse uploaded file bytes into a summary compatible with chat and ingestion."""
    if mime in SPREADSHEET_MIMES:
        return _parse_spreadsheet(content, mime, filename)
    if mime in IMAGE_MIMES:
        return _parse_image(content, mime, filename)
    return _parse_document(content, mime, filename)


def _parse_spreadsheet(content: bytes, mime: str, filename: str) -> dict[str, Any]:
    source_format = infer_source_format(filename, mime)
    summary: dict[str, Any] = {
        "file_type": "spreadsheet",
        "source_format": source_format,
        "warnings": [],
    }

    if mime == "text/csv":
        text = content.decode("utf-8", errors="replace")
        reader = csv.reader(io.StringIO(text))
        rows = list(reader)
        if not rows:
            summary.update(
                {
                    "confidence": "MEDIUM",
                    "ventas_detectadas": [],
                    "rows_processed": 0,
                    "row_count": 0,
                    "columns": [],
                    "preview_rows": [],
                }
            )
            return summary

        headers = rows[0]
        data_rows = rows[1:]
        analysis = analyze_headers(headers)
        preview_rows = rows_to_dicts(headers, data_rows[:50])
        summary.update(analysis)
        summary["headers"] = headers
        summary["rows_processed"] = len(data_rows)
        summary["ventas_detectadas"] = preview_rows
        summary["row_count"] = len(data_rows)
        summary["columns"] = headers
        summary["preview_rows"] = preview_rows[:10]
        return summary

    import openpyxl  # noqa: PLC0415

    workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    try:
        worksheet = workbook.active
        all_rows = list(worksheet.iter_rows(values_only=True))  # type: ignore[union-attr]
        if not all_rows:
            summary.update(
                {
                    "confidence": "MEDIUM",
                    "ventas_detectadas": [],
                    "rows_processed": 0,
                    "row_count": 0,
                    "columns": [],
                    "preview_rows": [],
                }
            )
            return summary

        headers = [
            str(cell) if cell is not None else f"col_{index}"
            for index, cell in enumerate(all_rows[0])
        ]
        data_rows = [list(row) for row in all_rows[1:]]
        analysis = analyze_headers(headers)
        preview_rows = rows_to_dicts(headers, data_rows[:50])
        summary.update(analysis)
        summary["headers"] = headers
        summary["rows_processed"] = len(data_rows)
        summary["ventas_detectadas"] = preview_rows
        summary["row_count"] = len(data_rows)
        summary["columns"] = headers
        summary["preview_rows"] = preview_rows[:10]
        return summary
    finally:
        workbook.close()


def _parse_document(content: bytes, mime: str, filename: str) -> dict[str, Any]:
    source_format = infer_source_format(filename, mime)
    raw_text = extract_document_text(content, mime, filename)
    detected = extract_amounts_from_text(raw_text)
    preview_rows = _preview_from_detected_rows(detected)
    row_count = len([line for line in raw_text.splitlines() if line.strip()])
    return {
        "file_type": "text",
        "source_format": source_format,
        "confidence": "MEDIUM",
        "row_count": row_count,
        "columns": [],
        "preview_rows": preview_rows,
        "raw_text_preview": raw_text[:1200],
        "warnings": [],
        **detected,
    }


def _parse_image(content: bytes, mime: str, filename: str) -> dict[str, Any]:
    source_format = infer_source_format(filename, mime)
    summary: dict[str, Any] = {
        "file_type": "image",
        "source_format": source_format,
        "confidence": "LOW",
        "warnings": [],
    }

    try:
        import pytesseract  # noqa: PLC0415
        from PIL import Image, UnidentifiedImageError  # noqa: PLC0415

        try:
            image = Image.open(io.BytesIO(content))
            raw_text = pytesseract.image_to_string(image, lang="spa+eng")
        except UnidentifiedImageError:
            raw_text = ""
            summary["warnings"].append(
                "No se pudo abrir la imagen (formato no soportado por Pillow)."
            )

        detected = extract_amounts_from_text(raw_text)
        summary.update(
            {
                "raw_text_preview": raw_text[:1200],
                "row_count": len([line for line in raw_text.splitlines() if line.strip()]),
                "columns": [],
                "preview_rows": _preview_from_detected_rows(detected),
                **detected,
            }
        )
        return summary
    except ImportError:
        summary["error"] = "OCR no disponible en este entorno"
        summary["row_count"] = 0
        summary["columns"] = []
        summary["preview_rows"] = []
        summary["ventas_detectadas"] = []
        summary["gastos_detectados"] = []
        summary["stock_detectado"] = []
        return summary


def extract_document_text(content: bytes, mime: str, filename: str) -> str:
    source_format = infer_source_format(filename, mime)

    if source_format == "docx":
        import docx  # noqa: PLC0415

        document = docx.Document(io.BytesIO(content))
        return "\n".join(paragraph.text for paragraph in document.paragraphs)

    if source_format == "pdf":
        from pypdf import PdfReader  # noqa: PLC0415

        reader = PdfReader(io.BytesIO(content))
        page_text = [page.extract_text() or "" for page in reader.pages]
        return "\n".join(part.strip() for part in page_text if part and part.strip())

    if source_format == "pptx":
        from pptx import Presentation  # noqa: PLC0415

        presentation = Presentation(io.BytesIO(content))
        lines: list[str] = []
        for slide in presentation.slides:
            for shape in slide.shapes:
                text = getattr(shape, "text", "")
                if text and text.strip():
                    lines.append(text.strip())
        return "\n".join(lines)

    return content.decode("utf-8", errors="replace")


def preview_value_from_summary(summary: dict[str, Any]) -> str:
    """Return a compact preview string from parsed_summary_json."""
    preview_candidates = [
        summary.get("preview_rows"),
        summary.get("data"),
        summary.get("rows"),
        summary.get("ventas_detectadas"),
        summary.get("products"),
        summary.get("margins"),
        summary.get("totals"),
        summary.get("raw_text_preview"),
    ]
    for candidate in preview_candidates:
        if candidate:
            return str(candidate)[:400]
    return ""


def summary_row_count(summary: dict[str, Any]) -> int | str:
    row_count = summary.get("row_count")
    if isinstance(row_count, int):
        return row_count
    legacy_count = summary.get("rows_processed")
    if isinstance(legacy_count, int):
        return legacy_count
    preview_rows = summary.get("preview_rows")
    if isinstance(preview_rows, list):
        return len(preview_rows)
    legacy_rows = summary.get("ventas_detectadas")
    if isinstance(legacy_rows, list):
        return len(legacy_rows)
    return "?"


def summary_columns(summary: dict[str, Any]) -> list[str]:
    columns = summary.get("columns")
    if isinstance(columns, list):
        return [str(value) for value in columns]
    headers = summary.get("headers")
    if isinstance(headers, list):
        return [str(value) for value in headers]
    return []


def _preview_from_detected_rows(detected: dict[str, Any]) -> list[dict[str, Any]]:
    preview: list[dict[str, Any]] = []
    for key in ("ventas_detectadas", "gastos_detectados", "stock_detectado"):
        rows = detected.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows[:3]:
            if isinstance(row, dict):
                preview.append(row)
        if preview:
            break
    return preview
