"""customer_extraction_service — extrae la ficha de UN cliente desde un archivo.

Flujo (espejo de ``remito_extraction_service``): el usuario sube la foto/PDF de un
DNI/tarjeta/ficha de cliente, o una planilla de UN cliente; este servicio EXTRAE los
datos (tipo, nombre/razón social, documento, IVA, contacto, domicilio) y los devuelve
como SUGERENCIA para prellenar el formulario de alta. NO persiste nada — el alta la
confirma el usuario (human-in-the-loop).

Dos caminos:
- XLSX/CSV → parseo DETERMINÍSTICO (sin IA): mapea columnas → campos del cliente con
  keywords. Toma el PRIMER registro (es una ficha individual; el import masivo es otro
  endpoint que reutiliza ``parse_customer_records``).
- Foto (jpg/png/heic/webp) / PDF → ``claude-sonnet-4-6`` multimodal con **structured
  output via tool use**. La IA TRANSCRIBE lo impreso; NO calcula nada. El input libre
  del usuario pasa por ``wrap_user_input()``.

El parser tabular (``parse_customer_records``) es la pieza compartida con el import
masivo (``customer_import_service``): devuelve una lista de dicts con los campos del
cliente, sin clasificar ni persistir.
"""

from __future__ import annotations

import base64
import csv
import io
import json
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.application.security.prompt_defense import wrap_user_input
from app.application.services.column_mapping_service import _normalize_col
from app.application.services.file_parsing import (
    IMAGE_MIMES,
    SPREADSHEET_MIMES,
    _decode_text_bytes,
    _detect_header_row,
    _sniff_delimiter,
    detect_supported_mime,
    rows_to_dicts,
)
from app.domain.date_parsing import BIRTHDAY_CENTURY_PIVOT, parse_business_date
from app.integrations.anthropic_client import (
    AnthropicConfigurationError,
    get_anthropic_async_client,
)
from app.observability.logger import get_logger

logger = get_logger(__name__)

_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 1024
_PDF_MIME = "application/pdf"
_VISION_IMAGE_MIMES = IMAGE_MIMES

# Campos del cliente que sabemos prellenar (subconjunto del modelo). El resto
# (telegram, notas largas, custom_fields) no se extrae de una ficha.
CUSTOMER_FIELDS = (
    "customer_type",
    "name",
    "last_name",
    "doc_type",
    "dni",
    "cuit",
    "iva_condition",
    "email",
    "phone",
    "address",
    "locality",
    "province",
    "postal_code",
    "birthday",
)


@dataclass
class CustomerExtraction:
    """Resultado de leer un archivo de cliente: campos sugeridos + confianza.

    ``fields`` es un dict ``{campo: valor}`` listo para prellenar el form (puede venir
    vacío si el archivo es ilegible — ``warnings`` lo explica). ``source_upload_id`` lo
    setea el endpoint si guarda el archivo.
    """

    fields: dict[str, Any] = field(default_factory=dict)
    confidence: str = "LOW"
    warnings: list[str] = field(default_factory=list)
    source_upload_id: str | None = None


# ── Mapeo determinístico de columnas → campos del cliente ──────────────────────


def _strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    )


def _norm_header(header: str) -> str:
    """Normaliza un header para matching: minúsculas, sin tildes, con underscores."""
    return _strip_accents(_normalize_col(str(header)))


def _match_customer_field(normalized: str) -> str | None:
    """Mapea un header normalizado → campo del cliente (o None si no aplica).

    El orden importa: ``razon_social`` y ``apellido_y_nombre`` deben resolverse a
    ``name`` antes de que ``apellido`` los capture como ``last_name``.
    """
    n = normalized
    # Identidad — razón social siempre es el nombre (empresa).
    if "razon" in n:
        return "name"
    # "Apellido y Nombre" / "Nombre y Apellido" → nombre completo en ``name``.
    if "apellido" in n and "nombre" in n:
        return "name"
    if "apellido" in n:
        return "last_name"
    # Documento (antes de "nombre"/"cliente" no hace falta; son disjuntos).
    if "cuit" in n or "cuil" in n:
        return "cuit"
    if "dni" in n or (("documento" in n or n == "doc") and "tipo" not in n):
        return "dni"
    if "tipo" in n and ("client" in n or "persona" in n):
        return "customer_type"
    # IVA / condición fiscal.
    if "iva" in n or "condicion" in n:
        return "iva_condition"
    # Contacto.
    if "email" in n or "mail" in n or "correo" in n:
        return "email"
    if (
        "telefono" in n
        or "celular" in n
        or n == "cel"
        or n == "tel"
        or "whatsapp" in n
        or "movil" in n
        or "contacto" in n
    ):
        return "phone"
    # Domicilio.
    if "codigo_postal" in n or n == "cp" or "cod_postal" in n or "postal" in n:
        return "postal_code"
    if "localidad" in n or "ciudad" in n or "partido" in n or "barrio" in n:
        return "locality"
    if "provincia" in n or n == "prov":
        return "province"
    if "direccion" in n or "domicilio" in n or "calle" in n or n == "dir":
        return "address"
    # Cumpleaños.
    if "cumple" in n or "nacimiento" in n or "birthday" in n or "fecha_nac" in n:
        return "birthday"
    # Nombre / cliente (genérico) — último para no pisar lo de arriba.
    if "nombre" in n or "cliente" in n:
        return "name"
    return None


def _map_customer_columns(headers: list[str]) -> dict[str, str]:
    """``{header: campo}`` para las columnas que mapean a un campo del cliente."""
    mapping: dict[str, str] = {}
    for header in headers:
        if header is None:
            continue
        field_name = _match_customer_field(_norm_header(header))
        # No pisar un mapeo ya encontrado (el primer header gana por campo).
        if field_name is not None and field_name not in mapping.values():
            mapping[header] = field_name
    return mapping


def _parse_date_loose(raw: Any) -> date | None:
    """Fecha de cumpleaños. F6-C1: mismo parser que el resto del sistema, con el
    pivote de siglo de personas (nacido en "50" es 1950, no 2050).
    """
    return parse_business_date(raw, century_pivot=BIRTHDAY_CENTURY_PIVOT)


def _clean_str(raw: Any, *, maxlen: int) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if text in ("", "None", "nan"):
        return None
    return text[:maxlen]


_FIELD_MAXLEN = {
    "customer_type": 10,
    "name": 300,
    "last_name": 200,
    "doc_type": 10,
    "dni": 15,
    "cuit": 13,
    "iva_condition": 25,
    "email": 320,
    "phone": 50,
    "address": 2000,
    "locality": 120,
    "province": 120,
    "postal_code": 12,
}


def _record_from_row(row: dict[str, Any], colmap: dict[str, str]) -> dict[str, Any]:
    """Construye un dict ``{campo: valor}`` del cliente desde una fila tabular."""
    record: dict[str, Any] = {}
    for header, field_name in colmap.items():
        value = row.get(header)
        if field_name == "birthday":
            parsed = _parse_date_loose(value)
            if parsed is not None:
                record["birthday"] = parsed
            continue
        cleaned = _clean_str(value, maxlen=_FIELD_MAXLEN.get(field_name, 300))
        if cleaned is None:
            continue
        record[field_name] = cleaned
    _infer_doc_and_type(record)
    return record


def _infer_doc_and_type(record: dict[str, Any]) -> None:
    """Completa ``doc_type`` y ``customer_type`` cuando se pueden inferir.

    - ``doc_type``: "cuit" si hay CUIT, si no "dni" si hay DNI.
    - ``customer_type``: "company" si hay razón social sin apellido y con CUIT;
      "person" si hay apellido. Solo si no vino explícito y es inequívoco.
    """
    if not record.get("doc_type"):
        if record.get("cuit"):
            record["doc_type"] = "cuit"
        elif record.get("dni"):
            record["doc_type"] = "dni"
    ctype = record.get("customer_type")
    # Persona si hay apellido y no vino tipo explícito. Sin apellido no forzamos
    # "company" (el usuario decide en el form).
    if ctype not in ("person", "company") and record.get("last_name"):
        record["customer_type"] = "person"


def _read_table(
    content: bytes, mime: str, filename: str
) -> tuple[list[str], list[dict[str, Any]]]:
    """Devuelve (headers, filas-como-dicts) de un XLSX/CSV. Espejo de remito."""
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


def parse_customer_records(
    content: bytes, filename: str, mime: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """Parsea una planilla de clientes → lista de dicts ``{campo: valor}``.

    Determinístico, sin IA. Pieza compartida entre la ficha individual (toma el primer
    registro) y el import masivo. Devuelve ``(records, warnings)``. Cada record tiene al
    menos un campo; las filas sin ningún campo del cliente se descartan.
    """
    headers, rows = _read_table(content, mime, filename)
    if not headers:
        return [], ["No se pudo leer el contenido tabular del archivo."]

    colmap = _map_customer_columns(headers)
    warnings: list[str] = []
    if "name" not in colmap.values():
        warnings.append(
            "No se identificó la columna de nombre/razón social. Revisá el encabezado."
        )
    if not ({"dni", "cuit"} & set(colmap.values())):
        warnings.append("No se identificó una columna de documento (DNI o CUIT).")

    records: list[dict[str, Any]] = []
    for row in rows:
        record = _record_from_row(row, colmap)
        # Descartar filas vacías (ningún campo mapeado con valor).
        if record:
            records.append(record)

    if not records:
        warnings.append("No se detectaron filas de cliente en el archivo.")
    return records, warnings


# ── Camino IA (foto/PDF de una ficha individual) ───────────────────────────────

_EXTRACTION_TOOL: dict[str, Any] = {
    "name": "registrar_ficha_cliente",
    "description": (
        "Transcribe los datos del cliente que figuran en la imagen o el PDF (DNI, "
        "tarjeta, ficha o factura). Devolvé EXACTAMENTE lo impreso. NO inventes ni "
        "completes datos que no estén. Si es una persona, separá nombre y apellido; "
        "si es una empresa, poné la razón social en 'name' y dejá 'last_name' vacío."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "customer_type": {
                "type": ["string", "null"],
                "enum": ["person", "company", None],
                "description": "person (DNI/persona física) | company (razón social).",
            },
            "name": {
                "type": ["string", "null"],
                "description": "Nombre de pila (persona) o razón social (empresa).",
            },
            "last_name": {"type": ["string", "null"], "description": "Apellido (persona)."},
            "dni": {"type": ["string", "null"], "description": "DNI, solo dígitos."},
            "cuit": {
                "type": ["string", "null"],
                "description": "CUIT/CUIL formato XX-XXXXXXXX-X si está.",
            },
            "iva_condition": {
                "type": ["string", "null"],
                "enum": [
                    "consumidor_final",
                    "monotributo",
                    "responsable_inscripto",
                    "exento",
                    None,
                ],
            },
            "email": {"type": ["string", "null"]},
            "phone": {"type": ["string", "null"], "description": "Teléfono/celular."},
            "address": {"type": ["string", "null"], "description": "Calle y número."},
            "locality": {"type": ["string", "null"]},
            "province": {"type": ["string", "null"]},
            "postal_code": {"type": ["string", "null"]},
            "birthday": {
                "type": ["string", "null"],
                "description": "Fecha de nacimiento YYYY-MM-DD si está.",
            },
        },
        "required": [],
    },
}

_SYSTEM_PROMPT = (
    "Sos un asistente que transcribe fichas de clientes de comercios argentinos. Leé la "
    "imagen o el PDF (DNI, tarjeta, ficha, factura) y transcribí SOLO los datos impresos. "
    "NO inventes datos faltantes, NO calcules nada. Usá la herramienta "
    "registrar_ficha_cliente para devolver el resultado."
)


async def _extract_with_ai(
    content: bytes,
    mime: str,
    *,
    user_hint: str | None,
    client_factory: Callable[..., Any] | None,
) -> tuple[CustomerExtraction, dict[str, int] | None]:
    """Extrae la ficha de una foto/PDF con IA (structured output via tool use).

    Devuelve la extracción + uso de tokens. Fail-soft: errores de config/red → warnings.
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

    instruction = "Transcribí los datos de este cliente."
    if user_hint:
        instruction = f"{instruction}\n{wrap_user_input(user_hint)}"

    factory_kwargs: dict[str, Any] = {}
    if client_factory is not None:
        factory_kwargs["client_factory"] = client_factory
    try:
        client = get_anthropic_async_client(**factory_kwargs)
    except AnthropicConfigurationError:
        return (
            CustomerExtraction(
                warnings=[
                    "La lectura por IA no está disponible (falta configuración). "
                    "Cargá los datos manualmente o subí una planilla."
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
        logger.warning("customer_extraction.ai_failed", error=str(exc))
        return (
            CustomerExtraction(
                warnings=[
                    "No se pudo leer la ficha con IA. "
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
            CustomerExtraction(
                warnings=["La IA no devolvió datos estructurados de la ficha."]
            ),
            usage,
        )

    return _extraction_from_tool_input(tool_input), usage


def _extract_tool_input(response: Any) -> dict[str, Any] | None:
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


def _extraction_from_tool_input(data: dict[str, Any]) -> CustomerExtraction:
    """Convierte el ``input`` de la tool en ``CustomerExtraction`` (defensivo)."""
    record: dict[str, Any] = {}
    for fname in CUSTOMER_FIELDS:
        if fname == "doc_type":
            continue  # se infiere
        raw = data.get(fname)
        if fname == "birthday":
            parsed = _parse_date_loose(raw)
            if parsed is not None:
                record["birthday"] = parsed
            continue
        if fname == "customer_type":
            if raw in ("person", "company"):
                record["customer_type"] = raw
            continue
        if fname == "iva_condition":
            if raw in (
                "consumidor_final",
                "monotributo",
                "responsable_inscripto",
                "exento",
            ):
                record["iva_condition"] = raw
            continue
        cleaned = _clean_str(raw, maxlen=_FIELD_MAXLEN.get(fname, 300))
        if cleaned is not None:
            record[fname] = cleaned
    _infer_doc_and_type(record)

    warnings: list[str] = []
    if not record.get("name"):
        warnings.append("La IA no pudo transcribir el nombre/razón social del cliente.")
        confidence = "LOW"
    elif not (record.get("dni") or record.get("cuit")):
        warnings.append("No se transcribió un documento (DNI/CUIT) — completalo a mano.")
        confidence = "MEDIUM"
    else:
        confidence = "MEDIUM"
    return CustomerExtraction(fields=record, confidence=confidence, warnings=warnings)


async def extract_customer(
    content: bytes,
    filename: str,
    *,
    content_type: str | None = None,
    user_hint: str | None = None,
    client_factory: Callable[..., Any] | None = None,
) -> tuple[CustomerExtraction, dict[str, int] | None]:
    """Punto de entrada de la ficha individual: detecta el tipo y delega.

    Devuelve ``(extraction, usage)``: ``usage`` es ``{input_tokens, output_tokens}``
    cuando hubo IA, o ``None`` para el camino tabular. No persiste nada — solo sugiere.
    """
    try:
        mime = detect_supported_mime(content, filename)
    except ValueError as exc:
        return CustomerExtraction(warnings=[str(exc)]), None

    if mime in SPREADSHEET_MIMES:
        records, warnings = parse_customer_records(content, filename, mime)
        if not records:
            return CustomerExtraction(warnings=warnings), None
        # Ficha individual: el primer registro. (El masivo es otro endpoint.)
        first = records[0]
        extra_warn = list(warnings)
        if len(records) > 1:
            extra_warn.append(
                f"El archivo tiene {len(records)} filas; se tomó la primera. "
                "Para cargar varios clientes usá la importación masiva."
            )
        confidence = "HIGH" if first.get("name") and (
            first.get("dni") or first.get("cuit")
        ) else "MEDIUM"
        return (
            CustomerExtraction(fields=first, confidence=confidence, warnings=extra_warn),
            None,
        )

    if mime == _PDF_MIME or mime in _VISION_IMAGE_MIMES:
        return await _extract_with_ai(
            content, mime, user_hint=user_hint, client_factory=client_factory
        )

    return (
        CustomerExtraction(
            warnings=[
                "Formato no soportado para lectura de ficha. Subí una foto, un PDF "
                "o una planilla (XLSX/CSV)."
            ]
        ),
        None,
    )
