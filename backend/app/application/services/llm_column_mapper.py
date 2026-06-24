"""FASE 2: 4ª capa de mapeo de columnas con LLM (fallback).

Se invoca SOLO para columnas donde el mapeo determinístico (historial → heurística
→ fuzzy) quedó por debajo del umbral de confianza. El LLM DESAMBIGUA (no calcula
montos — alineado con DeterministicFinance): recibe el nombre de la columna +
valores de ejemplo y elige el campo canónico más probable, o "ignore".

- Una sola llamada batch para todas las columnas dudosas (barato).
- Fail-silent: si el flag está apagado, no hay key, o el LLM falla → devuelve {}
  y el pipeline sigue con el resultado determinístico.
- Feature flag: ENABLE_LLM_COLUMN_MAPPING (default False).
"""

from __future__ import annotations

import json
from typing import Any

from app.config.settings import get_settings
from app.observability.logger import get_logger

logger = get_logger(__name__)

# Modelo por defecto si no hay settings.LLM_INGESTION_MODEL (configurable por env).
_DEFAULT_MODEL = "claude-sonnet-4-6"
# Umbral: columnas con confianza determinística < esto van al LLM.
LLM_MAPPING_THRESHOLD = 0.5

_SYSTEM_PROMPT = (
    "Sos un asistente que mapea columnas de una planilla de un negocio argentino a los "
    "campos canónicos de Véktor. Para cada columna te paso su nombre y algunos valores "
    "de ejemplo. Tu tarea es ELEGIR el campo canónico más probable para cada columna, "
    "o 'ignore' si ninguno aplica.\n\n"
    "Reglas:\n"
    "- Respondé SOLO un objeto JSON, sin texto adicional ni markdown.\n"
    "- Formato: {\"<nombre_columna>\": {\"target_field\": \"<campo|ignore>\", "
    "\"confidence\": <0..1>}}.\n"
    "- target_field DEBE ser exactamente uno de los campos válidos que te paso, o 'ignore'.\n"
    "- No inventes campos. No calcules ni transformes valores: solo clasificás la columna.\n"
    "- confidence refleja qué tan seguro estás (1 = certeza, 0 = nada).\n"
    "- Usá los valores de ejemplo para desambiguar (ej.: fechas, montos, nombres, códigos)."
)


def _build_user_message(
    entity_type: str,
    valid_fields: dict[str, str],
    columns: list[dict[str, Any]],
) -> str:
    fields_desc = "\n".join(f"- {k}: {v}" for k, v in valid_fields.items())
    cols_desc = "\n".join(
        f"- {c['header']}  (ejemplos: {', '.join(map(str, c.get('sample_values', []))) or '—'})"
        for c in columns
    )
    return (
        f"Tipo de datos: {entity_type}\n\n"
        f"Campos válidos:\n{fields_desc}\n- ignore: la columna no corresponde a ninguno\n\n"
        f"Columnas a mapear:\n{cols_desc}\n\n"
        "Devolvé el JSON con el mapeo de cada columna."
    )


def _parse_llm_json(raw: str, valid_fields: set[str]) -> dict[str, dict[str, Any]]:
    """Parsea y valida el JSON del LLM. Descarta entradas con target_field inválido."""
    raw = raw.strip()
    # Tolerar fences ```json ... ```
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    allowed = valid_fields | {"ignore"}
    for col, val in data.items():
        if not isinstance(val, dict):
            continue
        target = val.get("target_field")
        if target not in allowed:
            continue
        try:
            conf = float(val.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        out[str(col)] = {"target_field": target, "confidence": max(0.0, min(1.0, conf))}
    return out


async def suggest_with_llm(
    entity_type: str,
    columns: list[dict[str, Any]],
    valid_fields: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """Mapea columnas dudosas con el LLM. Devuelve {header: {target_field, confidence}}.

    Fail-silent: {} si el flag está apagado, no hay columnas, o el LLM falla.
    """
    if not columns:
        return {}
    settings = get_settings()
    if not getattr(settings, "ENABLE_LLM_COLUMN_MAPPING", False):
        return {}
    model = getattr(settings, "LLM_INGESTION_MODEL", _DEFAULT_MODEL)

    try:
        import anthropic  # noqa: PLC0415

        from app.integrations.anthropic_client import (  # noqa: PLC0415
            get_anthropic_async_client,
        )

        client = get_anthropic_async_client(anthropic.AsyncAnthropic)
        response = await client.messages.create(
            model=model,
            max_tokens=600,
            system=_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": _build_user_message(entity_type, valid_fields, columns),
                }
            ],
        )
        raw = (
            str(getattr(response.content[0], "text", "")).strip()
            if response.content
            else ""
        )
        parsed = _parse_llm_json(raw, set(valid_fields.keys()))
        logger.info(
            "ingestion.llm_column_mapping.done",
            entity_type=entity_type,
            columns_in=len(columns),
            columns_mapped=len(parsed),
        )
        return parsed
    except Exception as exc:  # noqa: BLE001 — fallback determinístico ante cualquier fallo
        logger.warning("ingestion.llm_column_mapping.failed", error=str(exc))
        return {}
