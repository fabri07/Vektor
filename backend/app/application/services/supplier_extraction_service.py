"""supplier_extraction_service — parser determinístico de planillas de proveedores.

Pieza compartida del import masivo (F7b, espejo de
``customer_extraction_service.parse_customer_records``): lee una planilla XLSX/CSV y
la mapea a una lista de dicts ``{campo: valor}`` del proveedor, sin clasificar ni
persistir. Reusa el mapeo de columnas de ``column_mapping_service`` para
``entity_type="supplier"`` en vez de duplicar una heurística propia — los campos y
keywords ya están definidos ahí (``CANONICAL_FIELDS["supplier"]`` / ``_HEURISTICS["supplier"]``,
F7a) y acotados a lo que persiste el modelo ``Supplier`` hoy (sin doc_type/address/etc.).

Determinístico, SIN IA — a diferencia de ``customer_extraction_service`` (que sí lee
fotos/PDF con IA para la ficha individual), acá la extracción multimodal no está en
alcance de F7b: el import masivo de proveedores solo soporta planilla.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from app.application.services.column_mapping_service import _normalize_col, heuristic_target
from app.application.services.file_parsing import (
    _decode_text_bytes,
    _detect_header_row,
    _sniff_delimiter,
    rows_to_dicts,
)

# Campos del proveedor que el import puede setear/actualizar — mismo subconjunto que
# persiste el modelo ``Supplier`` (ver ``models/supplier.py`` y
# ``CANONICAL_FIELDS["supplier"]`` en column_mapping_service).
SUPPLIER_FIELDS = (
    "name", "last_name", "cuil", "cuit", "iva_condition", "payment_method",
    "email", "phone", "notes",
)

_FIELD_MAXLEN = {
    "name": 300,
    "last_name": 200,
    "cuil": 13,
    "cuit": 13,
    "iva_condition": 25,
    "payment_method": 30,
    "email": 320,
    "phone": 50,
    "notes": 2000,
}


def _clean_str(raw: Any, *, maxlen: int) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if text in ("", "None", "nan"):
        return None
    return text[:maxlen]


def _map_supplier_columns(headers: list[str]) -> dict[str, str]:
    """``{header: campo}`` para las columnas que mapean a un campo del proveedor.

    Usa el reconocedor compartido (``column_mapping_service.heuristic_target``) en
    vez de un matcher propio — un solo lugar define qué encabezado va a qué campo.
    Un encabezado ambiguo no devuelve nada: acá no hay a quién preguntarle.
    """
    mapping: dict[str, str] = {}
    for header in headers:
        if header is None:
            continue
        field_name = heuristic_target(_normalize_col(str(header)), "supplier")
        # No pisar un mapeo ya encontrado (el primer header gana por campo).
        if field_name is not None and field_name not in mapping.values():
            mapping[header] = field_name
    return mapping


def _record_from_row(row: dict[str, Any], colmap: dict[str, str]) -> dict[str, Any]:
    record: dict[str, Any] = {}
    for header, field_name in colmap.items():
        cleaned = _clean_str(row.get(header), maxlen=_FIELD_MAXLEN.get(field_name, 300))
        if cleaned is None:
            continue
        record[field_name] = cleaned
    return record


def _read_table(content: bytes, mime: str) -> tuple[list[str], list[dict[str, Any]]]:
    """Devuelve (headers, filas-como-dicts) de un XLSX/CSV. Espejo de
    ``customer_extraction_service._read_table``."""
    if mime == "text/csv":
        text = _decode_text_bytes(content)
        delimiter = _sniff_delimiter(text[:8192])
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        all_rows = [r for r in reader if any((c or "").strip() for c in r)]
        if not all_rows:
            return [], []
        hdr_idx = _detect_header_row(all_rows)
        headers = [str(c) if c is not None else "" for c in all_rows[hdr_idx]]
        return headers, rows_to_dicts(headers, all_rows[hdr_idx + 1 :])

    import openpyxl  # noqa: PLC0415

    workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    try:
        worksheet = workbook.active
        if worksheet is None:
            return [], []
        all_rows = [list(r) for r in worksheet.iter_rows(values_only=True)]
        if not all_rows:
            return [], []
        hdr_idx = _detect_header_row(all_rows)
        headers = [
            str(cell) if cell is not None else f"col_{i}"
            for i, cell in enumerate(all_rows[hdr_idx])
        ]
        return headers, rows_to_dicts(headers, all_rows[hdr_idx + 1 :])
    finally:
        workbook.close()


def parse_supplier_records(
    content: bytes, filename: str, mime: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """Parsea una planilla de proveedores → lista de dicts ``{campo: valor}``.

    Determinístico, sin IA. Devuelve ``(records, warnings)``. Cada record tiene al
    menos un campo; las filas sin ningún campo del proveedor se descartan.
    ``filename`` se recibe por paridad de firma con ``parse_customer_records`` (no se
    usa hoy — el mime ya determina el camino de lectura).
    """
    del filename
    headers, rows = _read_table(content, mime)
    if not headers:
        return [], ["No se pudo leer el contenido tabular del archivo."]

    colmap = _map_supplier_columns(headers)
    warnings: list[str] = []
    if "name" not in colmap.values():
        warnings.append(
            "No se identificó la columna de nombre/razón social. Revisá el encabezado."
        )
    # CUIT o CUIL: alcanza con cualquiera de los dos como dato fuerte. Antes se
    # avisaba sólo por CUIL, así que un padrón de empresas —que trae CUIT— veía
    # una advertencia que no correspondía.
    if not {"cuil", "cuit"} & set(colmap.values()):
        warnings.append(
            "No se identificó una columna de CUIT ni de CUIL. Se puede importar igual por "
            "email/teléfono, pero sin ningún dato fuerte la fila queda pendiente de revisión."
        )

    records: list[dict[str, Any]] = []
    for row in rows:
        record = _record_from_row(row, colmap)
        if record:
            records.append(record)

    if not records:
        warnings.append("No se detectaron filas de proveedor en el archivo.")
    return records, warnings
