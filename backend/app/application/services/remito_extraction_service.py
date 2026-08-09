"""remito_extraction_service — extrae las líneas de un remito de proveedor.

Flujo: el usuario sube el archivo de un remito (foto/PDF/planilla); este servicio
EXTRAE las líneas (producto/cantidad/precio) y las devuelve como SUGERENCIA para
prellenar el formulario de alta. NO persiste transacciones — el alta la confirma el
usuario por ``POST /suppliers/{id}/receipts`` (human-in-the-loop).

Dos caminos:
- XLSX/CSV → parseo DETERMINÍSTICO (sin IA): se mapean columnas a líneas de remito
  con keywords (reutiliza ``column_mapping_service``). La aritmética de COGS/stock NO
  se hace acá — la hace ``import_receipt`` al confirmar.
- Foto (jpg/png/heic/webp) / PDF → ``claude-sonnet-4-6`` multimodal con **structured
  output via tool use**. La IA TRANSCRIBE lo impreso; NO calcula montos. El input de
  usuario (instrucción opcional) pasa por ``wrap_user_input()``.
"""

from __future__ import annotations

import base64
import csv
import io
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.application.security.prompt_defense import wrap_user_input
from app.application.services.column_mapping_service import (
    _normalize_col,
    heuristic_target,
)
from app.application.services.file_parsing import (
    IMAGE_MIMES,
    SPREADSHEET_MIMES,
    _decode_text_bytes,
    _detect_header_row,
    _sniff_delimiter,
    detect_supported_mime,
    normalize_numeric,
    rows_to_dicts,
)
from app.integrations.anthropic_client import (
    AnthropicConfigurationError,
    get_anthropic_async_client,
)
from app.observability.logger import get_logger

logger = get_logger(__name__)

_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 2048
# PDF como document block: la API multimodal de Anthropic acepta PDF nativo.
_PDF_MIME = "application/pdf"
# Tipos que la IA lee como imagen (los que ``detect_supported_mime`` reconoce):
# jpeg/png/heic/heif. La API multimodal de Anthropic los acepta.
_VISION_IMAGE_MIMES = IMAGE_MIMES


@dataclass
class ExtractedLine:
    """Una línea sugerida del remito (sin validar el alta — solo prellena)."""

    product_name: str
    sku: str | None
    qty: float
    # Decimal de punta a punta: el monto se transcribe y se devuelve sin pasar por
    # float (evita el round-trip float→Decimal que perdía precisión en el endpoint).
    unit_price: Decimal


@dataclass
class RemitoExtraction:
    """Resultado de la extracción: sugerencias + confianza + warnings.

    ``lines`` puede venir vacío (formato ilegible) — en ese caso ``warnings`` lo
    explica. ``source_upload_id`` lo setea el endpoint si guarda el archivo.
    """

    lines: list[ExtractedLine] = field(default_factory=list)
    shipping_cost: Decimal | None = None
    currency: str = "ARS"
    confidence: str = "LOW"
    warnings: list[str] = field(default_factory=list)
    source_upload_id: str | None = None


# Targets del ColumnMapper (entity_type="product") que nos interesan para un remito.
# product_name ← name; qty ← stock_units (cantidad/qty/unidades). El precio unitario de
# una línea de remito puede venir rotulado como costo ("costo_unitario" → unit_cost_ars)
# o como precio ("precio_unitario" → sale_price_ars): para una compra, AMBOS son el precio
# unitario de la línea, así que aceptamos los dos targets de precio.
_TARGET_PRODUCT_NAME = "name"
_TARGET_SKU = "sku"
_TARGET_QTY = "stock_units"
_PRICE_TARGETS = frozenset({"unit_cost_ars", "sale_price_ars"})
#: Un remito es un documento de LÍNEAS, no un catálogo: «Precio» a secas no tiene
#: acá la ambigüedad de los tres precios de un producto. El costo es la lectura
#: correcta —es lo que el proveedor está cobrando— y se declara para que la
#: columna no se pierda por una duda que este documento ya responde.
_PREFERENCIA_DE_PRECIO = ("unit_cost_ars", "sale_price_ars")
#: En un remito la descripción de la línea ES el nombre del producto: no hay un
#: campo «descripción» aparte que llenar.
_DESCRIPTION_COMO_NOMBRE = "description"
_TARGET_UNIT_PRICE = "unit_price"  # target sintético propio del remito

# Keywords de envío/flete: una columna o fila con esto es el shipping_cost del remito,
# no una línea de producto.
_SHIPPING_KEYWORDS = {"envio", "envío", "flete", "shipping", "logistica", "logística"}


def _map_columns(headers: list[str]) -> dict[str, str]:
    """Mapea headers → target de remito (product_name/sku/qty/unit_price/shipping).

    Determinístico: reutiliza el ``heuristic_target`` del ColumnMapper sobre
    entity_type="product", declarando lo que este documento resuelve por su tipo
    (ver `_PREFERENCIA_DE_PRECIO` y `_DESCRIPTION_COMO_NOMBRE`). Devuelve
    ``{header: target}`` solo para columnas mapeadas.
    """
    mapping: dict[str, str] = {}
    for header in headers:
        if header is None:
            continue
        normalized = _normalize_col(str(header))
        if any(k in normalized for k in _SHIPPING_KEYWORDS):
            mapping[header] = "shipping"
            continue
        target = heuristic_target(normalized, "product", prefer=_PREFERENCIA_DE_PRECIO)
        if target == _DESCRIPTION_COMO_NOMBRE:
            target = _TARGET_PRODUCT_NAME
        if target in _PRICE_TARGETS:
            mapping[header] = _TARGET_UNIT_PRICE
        elif target in (_TARGET_PRODUCT_NAME, _TARGET_SKU, _TARGET_QTY):
            mapping[header] = target
    return mapping


def _parse_tabular(content: bytes, mime: str, filename: str) -> RemitoExtraction:
    """Extrae líneas de un remito tabular (XLSX/CSV) de forma determinística."""
    headers, rows = _read_table(content, mime, filename)
    if not headers:
        return RemitoExtraction(
            warnings=["No se pudo leer el contenido tabular del archivo."]
        )

    colmap = _map_columns(headers)
    name_cols = [h for h, t in colmap.items() if t == _TARGET_PRODUCT_NAME]
    sku_cols = [h for h, t in colmap.items() if t == _TARGET_SKU]
    qty_cols = [h for h, t in colmap.items() if t == _TARGET_QTY]
    price_cols = [h for h, t in colmap.items() if t == _TARGET_UNIT_PRICE]
    shipping_cols = [h for h, t in colmap.items() if t == "shipping"]

    warnings: list[str] = []
    if not name_cols:
        warnings.append(
            "No se identificó la columna de producto. Revisá el encabezado del archivo."
        )
    if not qty_cols:
        warnings.append("No se identificó la columna de cantidad.")
    if not price_cols:
        warnings.append("No se identificó la columna de precio unitario.")

    lines: list[ExtractedLine] = []
    shipping_cost: Decimal | None = None
    for row in rows:
        name = _first_nonempty(row, name_cols)
        if name is None:
            # Sin nombre de producto: puede ser una fila de envío/total al pie.
            if shipping_cols and shipping_cost is None:
                ship = normalize_numeric(_first_nonempty_raw(row, shipping_cols))
                if ship is not None:
                    shipping_cost = ship
            continue
        qty = normalize_numeric(_first_nonempty_raw(row, qty_cols)) or Decimal("0")
        unit_price = normalize_numeric(_first_nonempty_raw(row, price_cols)) or Decimal("0")
        sku = _first_nonempty(row, sku_cols)
        lines.append(
            ExtractedLine(
                product_name=str(name).strip()[:300],
                sku=(str(sku).strip()[:100] if sku else None),
                qty=float(qty),
                unit_price=unit_price,
            )
        )
        if shipping_cols and shipping_cost is None:
            ship = normalize_numeric(_first_nonempty_raw(row, shipping_cols))
            if ship is not None:
                shipping_cost = ship

    if not lines:
        warnings.append("No se detectaron líneas de producto en el archivo.")

    # Confianza: HIGH si se mapearon las 3 columnas clave y hay líneas; si no, MEDIUM/LOW.
    if lines and name_cols and qty_cols and price_cols:
        confidence = "HIGH"
    elif lines:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"

    return RemitoExtraction(
        lines=lines,
        shipping_cost=shipping_cost,
        confidence=confidence,
        warnings=warnings,
    )


def _read_table(
    content: bytes, mime: str, filename: str
) -> tuple[list[str], list[dict[str, Any]]]:
    """Devuelve (headers, filas-como-dicts) de un XLSX/CSV, sin clasificar buckets.

    A diferencia del pipeline de ingestión, NO ruteamos a ventas/gastos/stock: un
    remito SIEMPRE rinde líneas de producto, sea cual sea la clasificación que el
    pipeline le daría.
    """
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

    # XLSX: hoja activa (un remito típico es una sola hoja).
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


def _first_nonempty(row: dict[str, Any], cols: list[str]) -> str | None:
    """Primer valor no vacío entre ``cols`` (como string)."""
    raw = _first_nonempty_raw(row, cols)
    return str(raw) if raw is not None else None


def _first_nonempty_raw(row: dict[str, Any], cols: list[str]) -> Any:
    for c in cols:
        v = row.get(c)
        if v is not None and str(v).strip() not in ("", "None", "nan"):
            return v
    return None


# ── Camino IA (foto/PDF) ──────────────────────────────────────────────────────

_EXTRACTION_TOOL: dict[str, Any] = {
    "name": "registrar_lineas_remito",
    "description": (
        "Transcribe las líneas impresas en el remito del proveedor. Devolvé EXACTAMENTE "
        "lo que figura: por cada renglón, el nombre del producto, el código/SKU si lo hay, "
        "la cantidad y el precio unitario. NO calcules totales ni subtotales — solo transcribí. "
        "Si el remito tiene un costo de envío/flete por separado, ponelo en shipping_cost."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "lines": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "product_name": {"type": "string"},
                        "sku": {"type": ["string", "null"]},
                        "qty": {"type": "number"},
                        "unit_price": {"type": "number"},
                    },
                    "required": ["product_name", "qty", "unit_price"],
                },
            },
            "shipping_cost": {"type": ["number", "null"]},
        },
        "required": ["lines"],
    },
}

_SYSTEM_PROMPT = (
    "Sos un asistente que transcribe remitos de proveedores argentinos. Leé la imagen o "
    "el PDF y transcribí SOLO las líneas de producto impresas (nombre, código si hay, "
    "cantidad, precio unitario) más el costo de envío si aparece. NO inventes datos, NO "
    "calcules totales. Usá la herramienta registrar_lineas_remito para devolver el resultado."
)


async def _extract_with_ai(
    content: bytes,
    mime: str,
    *,
    user_hint: str | None,
    client_factory: Callable[..., Any] | None,
) -> tuple[RemitoExtraction, dict[str, int] | None]:
    """Extrae líneas de una foto/PDF con la IA (structured output via tool use).

    Devuelve la extracción + el uso de tokens (input/output) para que el caller lo
    registre como el resto. Fail-soft: errores de configuración/red → warnings.
    """
    if mime == _PDF_MIME:
        source_block: dict[str, Any] = {
            "type": "document",
            "source": {
                "type": "base64",
                "media_type": _PDF_MIME,
                "data": base64.standard_b64encode(content).decode("ascii"),
            },
        }
    else:
        media_type = "image/jpeg" if mime == "image/jpg" else mime
        source_block = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.standard_b64encode(content).decode("ascii"),
            },
        }

    instruction = "Transcribí las líneas de este remito."
    if user_hint:
        # Toda instrucción libre del usuario pasa por prompt-defense.
        instruction = f"{instruction}\n{wrap_user_input(user_hint)}"

    factory_kwargs: dict[str, Any] = {}
    if client_factory is not None:
        factory_kwargs["client_factory"] = client_factory
    try:
        client = get_anthropic_async_client(**factory_kwargs)
    except AnthropicConfigurationError:
        return (
            RemitoExtraction(
                warnings=[
                    "La lectura por IA no está disponible (falta configuración). "
                    "Cargá las líneas manualmente o subí una planilla."
                ]
            ),
            None,
        )

    try:
        response = await client.messages.create(
            model=_MODEL,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            tools=[_EXTRACTION_TOOL],
            tool_choice={"type": "tool", "name": _EXTRACTION_TOOL["name"]},
            messages=[
                {
                    "role": "user",
                    "content": [source_block, {"type": "text", "text": instruction}],
                }
            ],
        )
    except Exception as exc:  # noqa: BLE001 — fail-soft: la lectura es best-effort
        logger.warning("remito_extraction.ai_failed", error=str(exc))
        return (
            RemitoExtraction(
                warnings=[
                    "No se pudo leer el remito con IA. "
                    "Probá con otra foto o cargá manualmente."
                ]
            ),
            None,
        )

    usage = None
    if getattr(response, "usage", None) is not None:
        usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        }

    tool_input = _extract_tool_input(response)
    if tool_input is None:
        return (
            RemitoExtraction(
                warnings=["La IA no devolvió líneas estructuradas del remito."]
            ),
            usage,
        )

    return _extraction_from_tool_input(tool_input), usage


def _extract_tool_input(response: Any) -> dict[str, Any] | None:
    """Saca el ``input`` del bloque tool_use de la respuesta del modelo."""
    for block in getattr(response, "content", None) or []:
        if getattr(block, "type", None) == "tool_use":
            raw = getattr(block, "input", None)
            if isinstance(raw, dict):
                return raw
            if isinstance(raw, str):
                try:
                    parsed = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


def _extraction_from_tool_input(data: dict[str, Any]) -> RemitoExtraction:
    """Convierte el ``input`` de la tool en ``RemitoExtraction`` (defensivo)."""
    warnings: list[str] = []
    lines: list[ExtractedLine] = []
    for item in data.get("lines") or []:
        if not isinstance(item, dict):
            continue
        name = item.get("product_name")
        if not name or not str(name).strip():
            continue
        qty = normalize_numeric(item.get("qty"))
        unit_price = normalize_numeric(item.get("unit_price"))
        sku = item.get("sku")
        lines.append(
            ExtractedLine(
                product_name=str(name).strip()[:300],
                sku=(str(sku).strip()[:100] if sku else None),
                qty=float(qty) if qty is not None else 0.0,
                unit_price=unit_price if unit_price is not None else Decimal("0"),
            )
        )

    shipping = normalize_numeric(data.get("shipping_cost"))

    if not lines:
        warnings.append("La IA no pudo transcribir líneas de producto del remito.")
        confidence = "LOW"
    else:
        # Confianza media por default: la IA transcribe pero el usuario debe revisar.
        confidence = "MEDIUM"
        missing = [
            ln.product_name
            for ln in lines
            if ln.qty <= 0 or ln.unit_price <= 0
        ]
        if missing:
            warnings.append(
                f"{len(missing)} línea(s) con cantidad o precio faltante — "
                "revisá antes de confirmar."
            )

    return RemitoExtraction(
        lines=lines,
        shipping_cost=shipping,
        confidence=confidence,
        warnings=warnings,
    )


async def extract_remito(
    content: bytes,
    filename: str,
    *,
    content_type: str | None = None,
    user_hint: str | None = None,
    client_factory: Callable[..., Any] | None = None,
) -> tuple[RemitoExtraction, dict[str, int] | None]:
    """Punto de entrada: detecta el tipo de archivo y delega al camino correcto.

    Devuelve ``(extraction, usage)`` donde ``usage`` es ``{input_tokens, output_tokens}``
    cuando hubo llamada a la IA, o ``None`` para el camino tabular determinístico.
    No persiste nada — solo sugiere.
    """
    try:
        mime = detect_supported_mime(content, filename)
    except ValueError as exc:
        return (
            RemitoExtraction(warnings=[str(exc)]),
            None,
        )

    if mime in SPREADSHEET_MIMES:
        return _parse_tabular(content, mime, filename), None

    if mime == _PDF_MIME or mime in _VISION_IMAGE_MIMES:
        return await _extract_with_ai(
            content, mime, user_hint=user_hint, client_factory=client_factory
        )

    # docx/pptx/txt: no son formatos de remito reconocidos para extracción.
    return (
        RemitoExtraction(
            warnings=[
                "Formato no soportado para lectura de remito. Subí una foto, un PDF "
                "o una planilla (XLSX/CSV)."
            ]
        ),
        None,
    )
