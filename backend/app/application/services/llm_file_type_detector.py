"""FASE 2 (A1): detección del propósito de un archivo por su CONTENIDO con LLM.

Complementa la inferencia determinística por nombre de columna (`analyze_headers`
→ `infer_spreadsheet_type`). Corre SOLO cuando esa inferencia quedó ambigua
(`inferred_type == "general"`): el LLM mira headers + muestra de filas y decide si
el archivo es de ventas, gastos o stock. NO calcula ni transforma valores
(alineado con DeterministicFinance): solo clasifica el tipo.

- Una sola llamada por archivo (barato), solo en el caso ambiguo.
- Fail-silent: si el flag está apagado, no hay key, o el LLM falla → no toca el
  summary y el pipeline sigue con `inferred_type == "general"`.
- Feature flag: ENABLE_LLM_FILE_TYPE_DETECTION (default False).
- Modelo configurable: settings.LLM_INGESTION_MODEL (no hardcodeado).
- Cada decisión queda trazada en pipeline_events (stage="mapping").
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import pipeline_event_service
from app.config.settings import get_settings
from app.observability.logger import get_logger
from app.persistence.models.pipeline_event import STAGE_MAPPING

logger = get_logger(__name__)

# Tipo ambiguo que dispara la detección por contenido.
_AMBIGUOUS_TYPE = "general"
# Tipos que el LLM puede asignar (deben coincidir con los de infer_spreadsheet_type).
_VALID_TYPES = frozenset({"ventas", "gastos", "stock"})
# Cuántas filas de muestra se le pasan al LLM como evidencia.
_MAX_SAMPLE_ROWS = 8

_SYSTEM_PROMPT = (
    "Sos un asistente que clasifica el propósito de una planilla de un negocio "
    "argentino. Te paso los nombres de columna y algunas filas de ejemplo. "
    "Decidí si la planilla registra VENTAS (ingresos/cobros a clientes), GASTOS "
    "(egresos/pagos a proveedores, servicios, alquiler) o STOCK (catálogo de "
    "productos / inventario / lista de precios).\n\n"
    "Reglas:\n"
    "- Respondé SOLO un objeto JSON, sin texto adicional ni markdown.\n"
    '- Formato: {"file_type": "ventas|gastos|stock|general", "confidence": <0..1>}.\n'
    '- Usá "general" si genuinamente no podés decidir con la evidencia.\n'
    "- No inventes datos. No calcules montos: solo clasificás el propósito.\n"
    "- confidence refleja qué tan seguro estás (1 = certeza, 0 = nada)."
)


def _build_user_message(headers: list[str], sample_rows: list[dict[str, Any]]) -> str:
    cols = ", ".join(str(h) for h in headers) or "—"
    lines: list[str] = []
    for row in sample_rows[:_MAX_SAMPLE_ROWS]:
        cells = "; ".join(f"{k}={v}" for k, v in row.items() if v not in (None, "", "nan"))
        if cells:
            lines.append(f"- {cells}")
    sample = "\n".join(lines) or "—"
    return (
        f"Columnas: {cols}\n\n"
        f"Filas de ejemplo:\n{sample}\n\n"
        "Devolvé el JSON con file_type y confidence."
    )


def _parse_llm_response(raw: str) -> tuple[str | None, float]:
    """Parsea el JSON del LLM → (file_type válido | None, confidence)."""
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None, 0.0
    if not isinstance(data, dict):
        return None, 0.0
    file_type = data.get("file_type")
    try:
        conf = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
    except (TypeError, ValueError):
        conf = 0.0
    if file_type not in _VALID_TYPES:
        return None, conf
    return str(file_type), conf


async def detect_file_type(
    headers: list[str],
    sample_rows: list[dict[str, Any]],
) -> tuple[str | None, float, str]:
    """Clasifica el propósito del archivo con el LLM. Devuelve (tipo|None, conf, model).

    Fail-silent: (None, 0.0, model) si el flag está apagado, no hay headers, o el
    LLM falla.
    """
    settings = get_settings()
    model = getattr(settings, "LLM_INGESTION_MODEL", "claude-sonnet-4-6")
    if not headers:
        return None, 0.0, model
    if not getattr(settings, "ENABLE_LLM_FILE_TYPE_DETECTION", False):
        return None, 0.0, model

    try:
        import anthropic  # noqa: PLC0415

        from app.integrations.anthropic_client import (  # noqa: PLC0415
            get_anthropic_async_client,
        )

        client = get_anthropic_async_client(anthropic.AsyncAnthropic)
        response = await client.messages.create(
            model=model,
            max_tokens=150,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _build_user_message(headers, sample_rows)}],
        )
        raw = (
            str(getattr(response.content[0], "text", "")).strip() if response.content else ""
        )
        file_type, conf = _parse_llm_response(raw)
        logger.info(
            "ingestion.llm_file_type.done",
            detected_type=file_type,
            confidence=conf,
            headers_in=len(headers),
        )
        return file_type, conf, model
    except Exception as exc:  # noqa: BLE001 — fail-silent: el pipeline sigue con "general"
        logger.warning("ingestion.llm_file_type.failed", error=str(exc))
        return None, 0.0, model


async def maybe_detect_file_type(
    session: AsyncSession,
    summary: dict[str, Any],
    *,
    trace_id: uuid.UUID | str,
    tenant_id: uuid.UUID | str,
    file_id: uuid.UUID | str | None,
) -> None:
    """Si el summary quedó ambiguo (`inferred_type == "general"`), consulta el LLM
    y sobrescribe `inferred_type` in-place. Emite un pipeline_event con la decisión.

    No-op (fail-silent) cuando el flag está apagado o no aplica. Nunca rompe el
    pipeline: cualquier error queda contenido en `detect_file_type`.
    """
    if summary.get("inferred_type") != _AMBIGUOUS_TYPE:
        return
    headers = summary.get("headers") or summary.get("columns") or []
    sample_rows = summary.get("preview_rows") or []
    if not headers:
        return

    detected, conf, model = await detect_file_type(headers, sample_rows)
    if detected is None:
        return

    previous_type = summary.get("inferred_type")
    summary["inferred_type"] = detected
    # Mantener coherente el contexto de mapeo único (CSV / single-sheet) si existe.
    contexts = summary.get("mapping_contexts")
    if isinstance(contexts, list) and len(contexts) == 1 and isinstance(contexts[0], dict):
        contexts[0]["entity_type"] = _TYPE_TO_ENTITY.get(detected, contexts[0].get("entity_type"))

    await pipeline_event_service.emit_event(
        session,
        trace_id=trace_id,
        tenant_id=tenant_id,
        stage=STAGE_MAPPING,
        file_id=file_id,
        confidence=_confidence_band(conf),
        detail={
            "type": "file_type_detection",
            "headers": [str(h) for h in headers],
            "sample_size": len(sample_rows[:_MAX_SAMPLE_ROWS]),
            "previous_type": previous_type,
            "detected_type": detected,
            "confidence": round(conf, 3),
            "model": model,
        },
    )


# Mapeo tipo de archivo → entity_type del mapping_context (coherente con file_parsing).
_TYPE_TO_ENTITY: dict[str, str] = {
    "ventas": "sale",
    "gastos": "expense",
    "stock": "product",
}


def _confidence_band(conf: float) -> str:
    """Float → banda HIGH/MEDIUM/LOW (la columna confidence de pipeline_events es String(10))."""
    if conf >= 0.75:
        return "HIGH"
    if conf >= 0.5:
        return "MEDIUM"
    return "LOW"
