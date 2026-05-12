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

# Columnas que indican transacciones de venta (monto cobrado)
VENTA_COLS = {
    "precio_venta",
    "venta",
    "ventas",
    "ingreso",
    "monto",
    "importe",
    "total_venta",
    "total_cobrado",
    "cobro",
}
# "precio" y "total" solos son ambiguos (también aparecen en inventarios)
# — se pesan por separado en infer_spreadsheet_type

GASTO_COLS = {"costo", "gasto", "gastos", "egreso", "compra", "deuda", "pago", "proveedor"}

# Señales fuertes: inequívocamente un catálogo/inventario — no aparecen en transacciones
CATALOGO_COLS = {"sku", "codigo", "inventario", "articulo", "item"}

# Señales débiles: pueden aparecer en ventas/gastos también (descripcion de venta, nombre del proveedor)
NOMBRE_COLS = {"producto", "nombre"}

# PRODUCTO_COLS = unión para retrocompatibilidad con código que ya lo usa
PRODUCTO_COLS = CATALOGO_COLS | NOMBRE_COLS | {"stock", "descripcion"}
FECHA_COLS = {"fecha", "date", "dia", "mes", "periodo"}

VENTA_CTX = {"venta", "ingreso", "cobro", "ticket", "recibo", "pago recibido", "cobrado"}
GASTO_CTX = {"gasto", "compra", "pago", "factura", "proveedor", "egreso", "gaste"}
STOCK_CTX = {"stock", "inventario", "unidades", "cantidad", "mercaderia", "mercadería"}

AMOUNT_RE = re.compile(r"\$\s*[\d.,]+")

# Strings que representan ausencia de valor en imports
_NULL_STRINGS = {"", "nan", "none", "null", "n/a", "na", "-", "nd"}

# Umbral de % de nulls por columna a partir del cual se advierte al usuario
NULL_COLUMN_WARN_THRESHOLD = 0.35


def normalize_numeric(
    value: object,
    *,
    required: bool = False,
    field_label: str = "campo",
) -> "Decimal | None":
    """Normaliza un valor numérico de import o API.

    - None / string vacío / strings nulos → None (o 422 si required)
    - math.nan / math.inf → None (o 422 si required)
    - Strings con $, comas, puntos → parseo ARS (1.234,56 → 1234.56)
    """
    import math  # noqa: PLC0415
    from decimal import Decimal, InvalidOperation  # noqa: PLC0415

    if value is None:
        if required:
            raise ValueError(f"{field_label} es obligatorio y no puede ser nulo.")
        return None

    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        if required:
            raise ValueError(f"{field_label} contiene un valor inválido (NaN/Inf).")
        return None

    str_val = str(value).strip().lower()
    if str_val in _NULL_STRINGS:
        if required:
            raise ValueError(f"{field_label} es obligatorio.")
        return None

    # Normalización de formato ARS: "1.234,56" → "1234.56"
    if "," in str_val and "." in str_val:
        str_val = str_val.replace(".", "").replace(",", ".")
    elif "," in str_val:
        str_val = str_val.replace(",", ".")
    str_val = str_val.lstrip("$").strip()

    try:
        return Decimal(str_val)
    except InvalidOperation:
        if required:
            raise ValueError(f"{field_label} tiene un formato numérico inválido: {value!r}")
        return None


def normalize_categorical(
    value: object,
    *,
    required: bool = False,
    default: "str | None" = None,
    field_label: str = "campo",
) -> "str | None":
    """Normaliza un valor categórico de import o API."""
    if value is None:
        return default if not required else (_ for _ in ()).throw(
            ValueError(f"{field_label} es obligatorio.")
        )
    str_val = str(value).strip()
    if str_val.lower() in _NULL_STRINGS:
        if required:
            raise ValueError(f"{field_label} es obligatorio.")
        return default
    return str_val or (default if not required else (_ for _ in ()).throw(
        ValueError(f"{field_label} no puede estar vacío.")
    ))


def compute_column_null_stats(rows: list[dict]) -> dict[str, float]:
    """Calcula el porcentaje de valores nulos por columna en una lista de dicts.

    Retorna {col: null_pct} para cada columna presente en al menos una fila.
    """
    if not rows:
        return {}
    all_keys: set[str] = set()
    for row in rows:
        all_keys.update(row.keys())

    stats: dict[str, float] = {}
    total = len(rows)
    for col in all_keys:
        null_count = sum(
            1 for row in rows
            if row.get(col) is None or str(row.get(col, "")).strip().lower() in _NULL_STRINGS
        )
        stats[col] = null_count / total
    return stats


def flag_columns_at_risk(
    null_stats: dict[str, float],
    threshold: float = NULL_COLUMN_WARN_THRESHOLD,
) -> list[dict]:
    """Retorna lista de columnas que superan el umbral de nulls, con recomendación."""
    return [
        {"column": col, "null_pct": round(pct, 4), "recommendation": "drop"}
        for col, pct in null_stats.items()
        if pct > threshold
    ]


def impute_column(values: list, field_type: str) -> list:
    """Imputa valores nulos en una columna según su tipo.

    - field_type='quantity': nulos → 0
    - field_type='numeric':  nulos → mediana de los valores válidos
    - field_type='categorical': nulos → None (sin imputación)
    """
    import math  # noqa: PLC0415
    from decimal import Decimal  # noqa: PLC0415

    def _is_null(v: object) -> bool:
        if v is None:
            return True
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return True
        return str(v).strip().lower() in _NULL_STRINGS

    if field_type == "quantity":
        return [0 if _is_null(v) else v for v in values]

    if field_type == "numeric":
        median = impute_column_median(values)
        fill = median if median is not None else Decimal("0")
        return [fill if _is_null(v) else v for v in values]

    # categorical — sin imputación
    return [None if _is_null(v) else v for v in values]


def impute_column_median(values: list) -> "Decimal | None":
    """Calcula la mediana de una lista de valores numéricos.
    Usa mediana (resistente a outliers de precios) en vez de media.
    Retorna None si no hay valores válidos.
    """
    from decimal import Decimal  # noqa: PLC0415
    import math  # noqa: PLC0415

    nums: list[Decimal] = []
    for v in values:
        if v is None:
            continue
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            continue
        try:
            nums.append(Decimal(str(v)))
        except Exception:
            continue

    if not nums:
        return None
    nums_sorted = sorted(nums)
    mid = len(nums_sorted) // 2
    if len(nums_sorted) % 2 == 0:
        return (nums_sorted[mid - 1] + nums_sorted[mid]) / 2
    return nums_sorted[mid]


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
    """Classify headers and infer spreadsheet type."""
    normalized = [h.lower().strip().replace(" ", "_") for h in headers]

    has_fecha = any(any(k in col for k in FECHA_COLS) for col in normalized)
    has_venta = any(any(k in col for k in VENTA_COLS) for col in normalized)
    has_gasto = any(any(k in col for k in GASTO_COLS) for col in normalized)
    has_producto = any(any(k in col for k in PRODUCTO_COLS) for col in normalized)
    # Señal fuerte de catálogo: sku/codigo/inventario/articulo/item — inequívocamente no transacción
    has_catalogo_fuerte = any(any(k in col for k in CATALOGO_COLS) for col in normalized)
    # Señal de nombre: producto/nombre — puede aparecer en ventas/gastos también
    has_nombre = any(any(k in col for k in NOMBRE_COLS) for col in normalized)

    # Señales ambiguas: "precio" / "total" solos pueden ser precio de catálogo
    has_precio_ambiguo = any(
        col in ("precio", "total", "price", "valor") for col in normalized
    )

    inferred_type = infer_spreadsheet_type(
        has_fecha=has_fecha,
        has_venta=has_venta,
        has_gasto=has_gasto,
        has_producto=has_producto,
        has_precio_ambiguo=has_precio_ambiguo,
        has_catalogo_fuerte=has_catalogo_fuerte,
        has_nombre=has_nombre,
    )

    has_catalogo_signal = has_catalogo_fuerte or has_nombre
    confidence = "HIGH" if (has_fecha and has_venta and not has_catalogo_signal) else "MEDIUM"

    return {
        "has_fecha": has_fecha,
        "has_venta": has_venta,
        "has_gasto": has_gasto,
        "has_producto": has_producto,
        "inferred_type": inferred_type,
        "confidence": confidence,
    }


def infer_spreadsheet_type(
    *,
    has_fecha: bool,
    has_venta: bool,
    has_gasto: bool,
    has_producto: bool,
    has_precio_ambiguo: bool = False,
    has_catalogo_fuerte: bool = False,
    has_nombre: bool = False,
) -> str:
    """Determina el tipo más probable del archivo tabular.

    Reglas (en orden de prioridad):
    1. Señal fuerte de catálogo (sku/codigo/inventario/articulo) → siempre stock.
    2. Señal de nombre/producto sin venta explícita → stock.
    3. Señal de nombre/producto sin fecha → stock (lista de precios, catálogo).
    4. Señal de nombre/producto + precio ambiguo (no venta transaccional) → stock.
    5. Sin señales de catálogo: fecha + venta → ventas; fecha + gasto → gastos.
    6. Fallbacks por señales sueltas.
    """
    # Señal fuerte (sku, inventario, articulo, codigo, item) → inequívocamente catálogo
    if has_catalogo_fuerte:
        return "stock"

    # Nombre/producto sin venta explícita → catálogo (descripcion en ventas suele ir con monto)
    if has_nombre and not has_venta:
        return "stock"

    # Nombre/producto sin fecha → lista de precios/catálogo
    if has_nombre and not has_fecha:
        return "stock"

    # Nombre/producto + precio ambiguo (ej: nombre+precio sin monto/importe) → catálogo
    if has_nombre and has_precio_ambiguo and not has_venta:
        return "stock"

    # Nombre/producto + fecha + venta transaccional → ambiguo; preferimos stock en Véktor
    # porque las listas de precios con precio_venta son más comunes que las exportaciones
    # de ventas con columna "nombre" en el contexto de PYMEs argentinas.
    if has_nombre:
        return "stock"

    # Sin señales de catálogo: transacciones de venta con fecha explícita
    if has_venta and has_fecha:
        return "ventas"

    # Sin señales de catálogo: transacciones de gasto con fecha explícita
    if has_gasto and has_fecha:
        return "gastos"

    # Solo precio ambiguo sin catálogo ni fecha → asumimos venta
    if has_precio_ambiguo and not has_producto:
        return "ventas"

    # Fallbacks por señales sueltas
    if has_venta:
        return "ventas"
    if has_gasto:
        return "gastos"
    return "general"


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


def _store_rows_by_type(
    summary: dict[str, Any],
    rows: list[dict[str, Any]],
    inferred_type: str,
) -> None:
    """Almacena las filas en la clave correcta según el tipo inferido.

    El campo `ventas_detectadas` siempre se rellena para compatibilidad con
    `_insert_confirmed_data`, que lo usa como fuente de filas independientemente
    del tipo. La clave específica sirve para que el agente y la UI sepan qué son.
    """
    if inferred_type == "stock":
        summary["stock_detectado"] = rows
        summary["ventas_detectadas"] = []
        summary["gastos_detectados"] = []
    elif inferred_type == "gastos":
        summary["gastos_detectados"] = rows
        summary["ventas_detectadas"] = rows  # backward compat para _insert_confirmed_data
    else:
        summary["ventas_detectadas"] = rows
        summary["gastos_detectados"] = []


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
        all_dicts = rows_to_dicts(headers, data_rows)
        preview_rows = all_dicts[:10]
        null_stats = compute_column_null_stats(all_dicts)
        at_risk = flag_columns_at_risk(null_stats)
        summary.update(analysis)
        summary["headers"] = headers
        summary["rows_processed"] = len(data_rows)
        summary["row_count"] = len(data_rows)
        summary["columns"] = headers
        summary["preview_rows"] = preview_rows
        if at_risk:
            summary["columns_at_risk"] = at_risk
            summary["warnings"].append(
                f"{len(at_risk)} columna(s) con más del 35% de datos vacíos."
            )
        _store_rows_by_type(summary, all_dicts[:50], analysis.get("inferred_type", "general"))
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
                    "inferred_type": "general",
                }
            )
            return summary

        headers = [
            str(cell) if cell is not None else f"col_{index}"
            for index, cell in enumerate(all_rows[0])
        ]
        data_rows = [list(row) for row in all_rows[1:]]
        analysis = analyze_headers(headers)
        all_dicts = rows_to_dicts(headers, data_rows)
        preview_rows = all_dicts[:10]
        null_stats = compute_column_null_stats(all_dicts)
        at_risk = flag_columns_at_risk(null_stats)
        summary.update(analysis)
        summary["headers"] = headers
        summary["rows_processed"] = len(data_rows)
        summary["row_count"] = len(data_rows)
        summary["columns"] = headers
        summary["preview_rows"] = preview_rows
        if at_risk:
            summary["columns_at_risk"] = at_risk
            summary["warnings"].append(
                f"{len(at_risk)} columna(s) con más del 35% de datos vacíos."
            )
        _store_rows_by_type(summary, all_dicts[:50], analysis.get("inferred_type", "general"))
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
        except (UnidentifiedImageError, OSError):
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
