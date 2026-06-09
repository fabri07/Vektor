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
from typing import TYPE_CHECKING, Any

import filetype

if TYPE_CHECKING:
    from decimal import Decimal

from app.observability.logger import get_logger

logger = get_logger(__name__)

# Límite de seguridad (anti-DOS). Archivos > 16MB se rechazan con error claro.
# El manejo de archivos más grandes (streaming / re-arquitectura) queda para más
# adelante; con este techo el JSONB de uploaded_files no se acerca al límite de Neon.
MAX_FILE_SIZE_BYTES = 16 * 1024 * 1024  # 16 MB
MAX_FILE_SIZE_LABEL = "16 MB"

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

# Señales débiles: pueden aparecer en ventas/gastos también (descripcion de venta, nombre del
# proveedor)
NOMBRE_COLS = {"producto", "nombre"}

# PRODUCTO_COLS = unión para retrocompatibilidad con código que ya lo usa
PRODUCTO_COLS = CATALOGO_COLS | NOMBRE_COLS | {"stock", "descripcion"}
FECHA_COLS = {"fecha", "date", "dia", "mes", "periodo"}

# FASE 3: señales de compra de mercadería/insumos para reventa (→ inventario, no gasto).
# Conservador: solo se rerutea a stock si HAY mercadería Y una columna de cantidad.
MERCADERIA_COLS = {"mercaderia", "mercadería", "insumo", "insumos", "reposicion", "reposición"}
CANTIDAD_COLS = {"cantidad", "unidades", "unidad", "qty", "cant"}

# FASE 3: clasificación CONTEXTUAL venta vs gasto. Las columnas de dinero genéricas
# (monto/importe/total/precio/valor) son NEUTRALES — aparecen en cualquier documento
# financiero y no deben favorecer ventas por sí solas. El tipo se decide por señales
# FUERTES de contexto (scoring), y ante empate/ausencia → "general" (el usuario confirma).
MONEY_COLS = {"monto", "importe", "total", "precio", "valor", "monto_total", "importe_total"}
# Señales fuertes de venta (cliente, ticket, factura emitida, cobro, caja, medio de pago).
VENTA_SIGNAL_COLS = {
    "venta", "ventas", "vendido", "vendida", "ingreso", "ingresos", "facturacion",
    "facturación", "factura_emitida", "ticket", "cliente", "consumidor", "cobro",
    "cobrado", "caja", "medio_pago", "metodo_pago", "método_pago", "forma_pago",
    "fecha_venta",
}
# Señales fuertes de gasto/egreso (proveedor, categoría, concepto, servicio, etc.).
# Nota: "pago" suelto NO se incluye — colisiona con "metodo/medio_pago" (señal de venta).
GASTO_SIGNAL_COLS = {
    "gasto", "gastos", "egreso", "egresos", "proveedor", "categoria", "categoría",
    "rubro", "concepto", "servicio", "alquiler", "sueldo", "salario", "impuesto",
    "honorarios", "mantenimiento", "comision", "comisión", "flete", "logistica",
    "logística", "factura_recibida", "compra", "costo", "deuda",
}

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
) -> Decimal | None:
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
    except InvalidOperation as exc:
        if required:
            raise ValueError(
                f"{field_label} tiene un formato numérico inválido: {value!r}"
            ) from exc
        return None


def normalize_categorical(
    value: object,
    *,
    required: bool = False,
    default: str | None = None,
    field_label: str = "campo",
) -> str | None:
    """Normaliza un valor categórico de import o API."""
    if value is None:
        return (
            default
            if not required
            else (_ for _ in ()).throw(ValueError(f"{field_label} es obligatorio."))
        )
    str_val = str(value).strip()
    if str_val.lower() in _NULL_STRINGS:
        if required:
            raise ValueError(f"{field_label} es obligatorio.")
        return default
    return str_val or (
        default
        if not required
        else (_ for _ in ()).throw(ValueError(f"{field_label} no puede estar vacío."))
    )


def compute_column_null_stats(rows: list[dict[str, Any]]) -> dict[str, float]:
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
            1
            for row in rows
            if row.get(col) is None or str(row.get(col, "")).strip().lower() in _NULL_STRINGS
        )
        stats[col] = null_count / total
    return stats


def flag_columns_at_risk(
    null_stats: dict[str, float],
    threshold: float = NULL_COLUMN_WARN_THRESHOLD,
) -> list[dict[str, Any]]:
    """Retorna lista de columnas que superan el umbral de nulls, con recomendación."""
    return [
        {"column": col, "null_pct": round(pct, 4), "recommendation": "drop"}
        for col, pct in null_stats.items()
        if pct > threshold
    ]


def impute_column(values: list[Any], field_type: str) -> list[Any]:
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


def impute_column_median(values: list[Any]) -> Decimal | None:
    """Calcula la mediana de una lista de valores numéricos.
    Usa mediana (resistente a outliers de precios) en vez de media.
    Retorna None si no hay valores válidos.
    """
    import math  # noqa: PLC0415
    from decimal import Decimal  # noqa: PLC0415

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
    # FASE 3: venta/gasto por señales FUERTES de contexto (no por columna de dinero).
    venta_score = sum(any(k in col for k in VENTA_SIGNAL_COLS) for col in normalized)
    gasto_score = sum(any(k in col for k in GASTO_SIGNAL_COLS) for col in normalized)
    has_venta = venta_score > 0
    has_gasto = gasto_score > 0
    has_producto = any(any(k in col for k in PRODUCTO_COLS) for col in normalized)
    # Señal fuerte de catálogo: sku/codigo/inventario/articulo/item — inequívocamente no transacción
    has_catalogo_fuerte = any(any(k in col for k in CATALOGO_COLS) for col in normalized)
    # Señal de nombre: producto/nombre — puede aparecer en ventas/gastos también
    has_nombre = any(any(k in col for k in NOMBRE_COLS) for col in normalized)
    # FASE 3: señales de compra de mercadería + cantidad (inventario para reventa)
    has_mercaderia = any(any(k in col for k in MERCADERIA_COLS) for col in normalized)
    has_cantidad = any(any(k in col for k in CANTIDAD_COLS) for col in normalized)

    # Señales ambiguas: "precio" / "total" solos pueden ser precio de catálogo
    has_precio_ambiguo = any(col in ("precio", "total", "price", "valor") for col in normalized)

    inferred_type = infer_spreadsheet_type(
        has_fecha=has_fecha,
        has_venta=has_venta,
        has_gasto=has_gasto,
        has_producto=has_producto,
        has_precio_ambiguo=has_precio_ambiguo,
        has_catalogo_fuerte=has_catalogo_fuerte,
        has_nombre=has_nombre,
        has_mercaderia=has_mercaderia,
        has_cantidad=has_cantidad,
        venta_score=venta_score,
        gasto_score=gasto_score,
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
    has_mercaderia: bool = False,
    has_cantidad: bool = False,
    venta_score: int = 0,
    gasto_score: int = 0,
) -> str:
    """Determina el tipo más probable del archivo tabular.

    Reglas (en orden de prioridad):
    0. Compra de mercadería/insumos + cantidad → inventario (FASE 3, conservador).
    1. Señal fuerte de catálogo (sku/codigo/inventario/articulo) → siempre stock.
    2. Señal de nombre/producto sin venta explícita → stock.
    3. Señal de nombre/producto sin fecha → stock (lista de precios, catálogo).
    4. Señal de nombre/producto + precio ambiguo (no venta transaccional) → stock.
    5. Sin señales de catálogo: fecha + venta → ventas; fecha + gasto → gastos.
    6. Fallbacks por señales sueltas.
    """
    # FASE 3: compra de mercadería/insumos para reventa CON columna de cantidad →
    # inventario, NO gasto corriente. Conservador: requiere AMBAS señales para no
    # absorber "compra de servicios/alquiler" (sin columnas de inventario).
    if has_mercaderia and has_cantidad:
        return "stock"

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

    # FASE 3: discriminación venta vs gasto por CONTEXTO (scoring de señales fuertes).
    # Las columnas de dinero genéricas (monto/importe/total) NO cuentan acá. Empate de
    # señales o ausencia total → "general" (ambiguo): el usuario confirma el tipo, no se
    # importa como venta en silencio.
    if has_venta and has_gasto:
        if gasto_score > venta_score:
            return "gastos"
        if venta_score > gasto_score:
            return "ventas"
        return "general"  # señales de ambos, empate → ambiguo
    if has_gasto:
        return "gastos"
    if has_venta:
        return "ventas"
    # Sin señales fuertes de contexto (solo dinero/fecha/descripción) → ambiguo.
    return "general"


def rows_to_dicts(headers: list[str], rows: list[list[Any]]) -> list[dict[str, Any]]:
    """Mapea filas a dicts por header, tolerante a filas irregulares.

    Si una fila tiene menos celdas que headers, las faltantes quedan None
    (no se descartan en silencio como hacía `zip(strict=False)`). Filas None y
    celdas extra (más allá de los headers) se ignoran.
    """
    out: list[dict[str, Any]] = []
    n = len(headers)
    for row in rows:
        cells = list(row) if row is not None else []
        out.append(
            {
                headers[i]: (str(cells[i]) if i < len(cells) and cells[i] is not None else None)
                for i in range(n)
            }
        )
    return out


def _detect_header_row(rows: list[list[Any]], *, max_scan: int = 15) -> int:
    """Detecta el índice de la fila de encabezado, saltando títulos/filas vacías.

    Real-world: planillas exportadas suelen tener un título ("Ventas Junio") y/o
    filas en blanco arriba del encabezado real. Heurística: la primera fila con
    ≥2 celdas no vacías, mayoría de texto (no números), y cuya fila siguiente
    tenga una cantidad de celdas no vacías comparable (datos). Si ninguna
    califica, devuelve 0 (comportamiento previo).
    """

    def _nonempty(row: list[Any]) -> list[Any]:
        return [c for c in (row or []) if c is not None and str(c).strip() != ""]

    def _is_number(v: Any) -> bool:
        s = str(v).strip().replace(".", "").replace(",", "").replace("$", "").replace("%", "")
        return s.isdigit()

    limit = min(len(rows), max_scan)
    for i in range(limit):
        non_empty = _nonempty(rows[i])
        if len(non_empty) < 2:
            continue  # título o fila casi vacía
        numeric_ratio = sum(1 for c in non_empty if _is_number(c)) / len(non_empty)
        if numeric_ratio > 0.5:
            continue  # parece una fila de datos, no encabezados
        # La fila siguiente debería tener datos (≥1 celda no vacía).
        if i + 1 < len(rows) and len(_nonempty(rows[i + 1])) >= 1:
            return i
        if i + 1 >= len(rows):
            return i
    return 0


def _decode_text_bytes(content: bytes) -> str:
    """Decodifica bytes de CSV/texto probando encodings comunes (UTF-8 BOM, latin-1, cp1252)."""
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="replace")


def _sniff_delimiter(sample: str) -> str:
    """Detecta el delimitador de un CSV (`,` `;` tab `|`). Default `,` si no puede."""
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        return dialect.delimiter
    except csv.Error:
        # Heurística simple: el candidato más frecuente en la primera línea.
        first_line = sample.splitlines()[0] if sample.splitlines() else ""
        counts = {d: first_line.count(d) for d in (";", ",", "\t", "|")}
        best = max(counts, key=lambda d: counts[d])
        return best if counts[best] > 0 else ","


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
    ventas: list[dict[str, Any]] = []
    gastos: list[dict[str, Any]] = []
    stock: list[dict[str, Any]] = []

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

    Cada bucket recibe filas SOLO si corresponde a su tipo. `insert_confirmed_data`
    ya cae a `gastos_detectados` cuando `ventas_detectadas` está vacío, así que NO
    hace falta contaminar `ventas_detectadas` con filas de gastos (eso causaba
    riesgo de doble conteo venta+gasto).

    FASE F: el tipo ambiguo ("general") va a `otros_detectados` — nunca más datos
    ambiguos al bucket de ventas por default. El usuario los reasigna al confirmar
    (la inserción legacy también lee este bucket) o quedan en la bandeja "Otros"
    (`unclassified_records`).
    """
    if inferred_type == "stock":
        summary["stock_detectado"] = rows
        summary["ventas_detectadas"] = []
        summary["gastos_detectados"] = []
    elif inferred_type == "gastos":
        summary["gastos_detectados"] = rows
        summary["ventas_detectadas"] = []
        summary["stock_detectado"] = []
    elif inferred_type == "ventas":
        summary["ventas_detectadas"] = rows
        summary["gastos_detectados"] = []
        summary["stock_detectado"] = []
    else:
        summary["otros_detectados"] = rows
        summary["ventas_detectadas"] = []
        summary["gastos_detectados"] = []
        summary["stock_detectado"] = []


# Mapeo tipo-inferido / clasificación-de-hoja → entity_type del ColumnMapper.
_TYPE_TO_ENTITY: dict[str, str] = {
    "ventas": "sale",
    "gastos": "expense",
    "stock": "product",
}


def _build_table_context(
    inferred_type: str,
    headers: list[str],
    preview_rows: list[dict[str, Any]],
    row_count: int,
) -> dict[str, Any]:
    """Construye un único mapping_context para archivos de una tabla (csv / xlsx 1 hoja)."""
    return {
        "context_id": "table",
        "label": "Tabla",
        "source_kind": "table",
        "entity_type": _TYPE_TO_ENTITY.get(inferred_type),
        "headers": list(headers),
        "fields": None,
        "preview_rows": preview_rows,
        "row_count": row_count,
    }


def _build_text_contexts(
    detected: dict[str, list[dict[str, Any]]], source_kind: str
) -> list[dict[str, Any]]:
    """Construye mapping_contexts por grupo detectado en documentos de texto/imagen.

    Estos documentos NO tienen columnas: el "mapeo" es asignar cada grupo de líneas
    detectadas (ventas/gastos/stock) a un entity_type. Marca cada fila con
    `__context__` para separarla en la inserción. `headers=None` señala al frontend
    que muestre preview de líneas + selector de tipo, sin dropdowns de columna.
    """
    # Solo ventas y gastos: una línea de texto (`{linea, montos}`) no alcanza para
    # formar un producto, así que NO se emite contexto para stock_detectado. Esas
    # filas quedan en el summary pero no son importables (igual que el path legacy,
    # que nunca insertó stock desde texto).
    groups = [
        ("ventas_detectadas", "sale", "Ventas detectadas"),
        ("gastos_detectados", "expense", "Gastos detectados"),
    ]
    contexts: list[dict[str, Any]] = []
    for key, entity, label in groups:
        rows = detected.get(key) or []
        if not rows:
            continue
        ctx_id = f"text:{entity}"
        for r in rows:
            r["__context__"] = ctx_id
        fields = sorted({k for r in rows for k in r if not k.startswith("__")})
        contexts.append(
            {
                "context_id": ctx_id,
                "label": label,
                "source_kind": source_kind,
                "entity_type": entity,
                "headers": None,
                "fields": fields,
                "preview_rows": [
                    {k: v for k, v in r.items() if not k.startswith("__")} for r in rows[:10]
                ],
                "row_count": len(rows),
            }
        )
    return contexts


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
        # Encoding robusto (UTF-8 BOM, latin-1, cp1252) + delimitador auto (, ; \t |).
        text = _decode_text_bytes(content)
        delimiter = _sniff_delimiter(text[:8192])
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        rows = [r for r in reader if any((c or "").strip() for c in r)]  # descarta filas vacías
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

        # Detecta la fila de encabezado real (salta títulos/filas en blanco arriba).
        hdr_idx = _detect_header_row(rows)
        headers = rows[hdr_idx]
        data_rows = rows[hdr_idx + 1 :]
        analysis = analyze_headers(headers)
        all_dicts = rows_to_dicts(headers, data_rows)
        preview_rows = all_dicts[:10]
        null_stats = compute_column_null_stats(all_dicts)
        at_risk = flag_columns_at_risk(null_stats)
        summary.update(analysis)
        summary["delimiter"] = delimiter
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
        csv_inferred = analysis.get("inferred_type", "general")
        # Sin truncamiento: todas las filas se guardan (el [:50] previo perdía datos).
        _store_rows_by_type(summary, all_dicts, csv_inferred)
        summary["mapping_contexts"] = [
            _build_table_context(csv_inferred, headers, preview_rows, len(data_rows))
        ]
        return summary

    import unicodedata  # noqa: PLC0415

    import openpyxl  # noqa: PLC0415

    # Sin límite de filas: se importan todas las filas del archivo.
    # JSONB de Neon soporta hasta ~255 MB; un archivo típico de PYME es < 5 MB.
    _max_rows_per_type = None  # None = sin límite

    def _classify_sheet(name: str) -> str:
        """Clasifica una pestaña de xlsx por nombre → 'ventas'|'gastos'|'stock'|'unknown'."""
        norm = unicodedata.normalize("NFD", name.lower().strip())
        norm = "".join(c for c in norm if unicodedata.category(c) != "Mn")
        if any(k in norm for k in ["venta", "ingreso", "cobro", "sale"]):
            return "ventas"
        # Compras de mercadería son ingreso de inventario → productos/stock,
        # NO gasto. (Va antes que la regla de gastos para que "compras a
        # proveedores" se rutee a stock por el match de "compra".)
        if any(k in norm for k in ["compra", "mercaderia", "purchase"]):
            return "stock"
        if any(k in norm for k in ["gasto", "egreso", "operativo", "expense", "proveedor"]):
            return "gastos"
        if any(k in norm for k in ["producto", "inventario", "catalogo", "stock", "item"]):
            return "stock"
        return "unknown"

    workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    try:
        sheet_names = workbook.sheetnames
        is_multisheet = len(sheet_names) > 1

        if is_multisheet:
            # ── Multi-hoja: combinar datos de cada pestaña por tipo ──────────
            all_ventas: list[dict[str, Any]] = []
            all_gastos: list[dict[str, Any]] = []
            all_stock: list[dict[str, Any]] = []
            primary_headers: list[str] = []
            contexts: list[dict[str, Any]] = []
            total_rows = 0

            for sheet_name in sheet_names:
                ws = workbook[sheet_name]
                rows = [list(r) for r in ws.iter_rows(values_only=True)]
                if len(rows) < 2:
                    continue

                # Detecta la fila de encabezado real de la hoja.
                hdr_idx = _detect_header_row(rows)
                headers = [
                    str(c) if c is not None else f"col_{i}"
                    for i, c in enumerate(rows[hdr_idx])
                ]
                data_rows = rows[hdr_idx + 1 :]
                if not data_rows:
                    continue

                dicts = rows_to_dicts(headers, data_rows)
                if not primary_headers:
                    primary_headers = headers

                total_rows += len(data_rows)

                sheet_type = _classify_sheet(sheet_name)
                if sheet_type == "unknown":
                    # Fallback: inferir por columnas
                    sheet_type = analyze_headers(headers).get("inferred_type", "general")

                entity = _TYPE_TO_ENTITY.get(sheet_type)
                context_id = f"sheet:{sheet_name}"
                if entity is None:
                    # Hoja no clasificable: NO se descarta en silencio. Se preserva como
                    # contexto `unclassified` (entity_type=None) con warning, para que el
                    # usuario la vea y la reclasifique manualmente. El insert salta los
                    # contextos sin entidad, así que no se importa hasta que se asigne tipo.
                    contexts.append(
                        {
                            "context_id": context_id,
                            "label": sheet_name,
                            "source_kind": "sheet",
                            "entity_type": None,
                            "headers": headers,
                            "fields": None,
                            "preview_rows": dicts[:10],
                            "row_count": len(dicts),
                            "unclassified": True,
                        }
                    )
                    # FASE F: las filas completas se preservan en otros_detectados
                    # (antes solo quedaban las 10 de preview) → al confirmar, lo no
                    # reclasificado va a la bandeja "Otros" (unclassified_records).
                    summary.setdefault("otros_detectados", []).extend(
                        {**d, "__context__": context_id} for d in dicts
                    )
                    summary["warnings"].append(
                        f"La hoja '{sheet_name}' no se pudo clasificar automáticamente. "
                        "Revisala y asignale un tipo manualmente si querés importarla."
                    )
                    continue

                contexts.append(
                    {
                        "context_id": context_id,
                        "label": sheet_name,
                        "source_kind": "sheet",
                        "entity_type": entity,
                        "headers": headers,
                        "fields": None,
                        "preview_rows": dicts[:10],
                        "row_count": len(dicts),
                    }
                )
                # Marcador de origen: permite separar filas por contexto en la inserción
                # cuando varias hojas comparten tipo. Se ignora en el mapeo por keyword.
                marked = [{**d, "__context__": context_id} for d in dicts]
                if sheet_type == "ventas":
                    all_ventas.extend(marked)
                elif sheet_type == "gastos":
                    all_gastos.extend(marked)
                elif sheet_type == "stock":
                    all_stock.extend(marked)

            has_ventas = bool(all_ventas)
            has_gastos = bool(all_gastos)
            has_stock = bool(all_stock)

            if has_ventas and has_gastos and has_stock or has_ventas and has_gastos:
                inferred_type = "mixed"
            elif has_ventas:
                inferred_type = "ventas"
            elif has_gastos:
                inferred_type = "gastos"
            elif has_stock:
                inferred_type = "stock"
            else:
                inferred_type = "general"

            # Preview global desde los contexts (sin el marcador __context__).
            preview: list[dict[str, Any]] = []
            for _ctx in contexts:
                preview.extend(_ctx["preview_rows"])
            preview = preview[:10]

            summary.update(
                {
                    "multi_sheet": True,
                    "sheet_names": sheet_names,
                    "inferred_type": inferred_type,
                    "confidence": "HIGH" if (has_ventas or has_gastos or has_stock) else "LOW",
                    "has_venta": has_ventas,
                    "has_gasto": has_gastos,
                    "has_producto": has_stock,
                    "headers": primary_headers,
                    "columns": primary_headers,
                    "row_count": total_rows,
                    "rows_processed": total_rows,
                    "ventas_detectadas": all_ventas,
                    "gastos_detectados": all_gastos,
                    "stock_detectado": all_stock,
                    "preview_rows": preview,
                    "mapping_contexts": contexts,
                }
            )
            return summary

        # ── Una sola hoja: comportamiento original (con límite ampliado) ─────
        worksheet = workbook.active
        all_rows = list(worksheet.iter_rows(values_only=True))
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

        # Detecta la fila de encabezado real (salta títulos/filas en blanco arriba).
        _all = [list(r) for r in all_rows]
        hdr_idx = _detect_header_row(_all)
        headers = [
            str(cell) if cell is not None else f"col_{index}"
            for index, cell in enumerate(_all[hdr_idx])
        ]
        data_rows = _all[hdr_idx + 1 :]
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
        inferred = analysis.get("inferred_type", "general")
        _store_rows_by_type(summary, all_dicts, inferred)
        summary["mapping_contexts"] = [
            _build_table_context(inferred, headers, preview_rows, len(data_rows))
        ]
        return summary
    finally:
        workbook.close()


def _parse_document(content: bytes, mime: str, filename: str) -> dict[str, Any]:
    source_format = infer_source_format(filename, mime)
    raw_text = extract_document_text(content, mime, filename)
    detected = extract_amounts_from_text(raw_text)
    preview_rows = _preview_from_detected_rows(detected)  # limpio (antes de marcar)
    contexts = _build_text_contexts(detected, "text_group")  # marca filas con __context__
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
        "mapping_contexts": contexts,
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
        preview_rows = _preview_from_detected_rows(detected)  # limpio (antes de marcar)
        contexts = _build_text_contexts(detected, "ocr_group")  # marca filas con __context__
        summary.update(
            {
                "raw_text_preview": raw_text[:1200],
                "row_count": len([line for line in raw_text.splitlines() if line.strip()]),
                "columns": [],
                "preview_rows": preview_rows,
                "mapping_contexts": contexts,
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
