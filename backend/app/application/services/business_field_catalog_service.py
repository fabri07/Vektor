"""Cambio 1 del plan de conservación de datos — catálogo de LECTURA combinado.

Compone tres fuentes en un único listado, sin duplicados, para
`GET /fields/available`:

1. `domain/business_field_catalog.py` — campos canónicos/evidencia, estáticos.
2. `field_definition_service.get_merged_definitions` — adicionales del
   vertical + del tenant (declarados a mano o recuperados por el backfill de
   `scripts/backfill_field_definitions_from_history.py`).

No reemplaza `get_merged_definitions` (que sigue siendo el contrato de
EDICIÓN/ERD existente) — es una proyección de LECTURA por encima, pensada para
que una tabla o un exportador sepan, de una sola consulta, "¿qué campos de
esta entidad puedo mostrar/exportar y dónde vive cada uno?".
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.field_definition_service import get_merged_definitions
from app.domain.business_field_catalog import FieldDescriptor, descriptors_for
from app.domain.verticals import Vertical


def _descriptor_to_dict(d: FieldDescriptor) -> dict[str, Any]:
    return {
        "field_id": d.field_id,
        "entity_type": d.entity_type,
        "field_key": d.field_key,
        "label": d.label,
        "data_type": d.data_type,
        "unit": d.unit,
        "origin": d.origin,
        "value_path": d.value_path,
        "editable": d.editable,
        "exportable": d.exportable,
        "searchable": d.searchable,
        "default_visible": d.default_visible,
        "financial_rule": d.financial_rule,
    }


def _additional_to_dict(entity_type: str, field: dict[str, Any]) -> dict[str, Any]:
    field_key = field["field_key"]
    return {
        "field_id": f"{entity_type}:{field_key}",
        "entity_type": entity_type,
        "field_key": field_key,
        "label": field["label"],
        "data_type": field["data_type"],
        "unit": None,
        # Un campo adicional que HOY vive sobre un campo base del vertical
        # (`is_base_field=True`, ej. un override de label/requerido) sigue
        # siendo "canonical" para quien lee — la definición de vertical ya lo
        # describe como tal. Solo lo declarado libremente por el tenant es
        # "additional" de verdad.
        "origin": "canonical" if field.get("is_base_field") else "additional",
        # Los campos base tienen columna real (mismo `field_key` que el
        # atributo); los adicionales viven en `custom_fields`.
        "value_path": field_key if field.get("is_base_field") else f"custom_fields.{field_key}",
        "editable": True,
        "exportable": True,
        "searchable": True,
        "default_visible": True,
        "financial_rule": None,
    }


async def get_available_fields(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    vertical_code: Vertical,
    entity_type: str,
) -> list[dict[str, Any]]:
    """Todos los campos consultables de una entidad, con identidad estable.

    Orden: canónicos primero (misma secuencia que ya usa el mapeo), evidencia
    después, adicionales al final. Deduplicado por `field_id` — un campo
    estático (ej. `purchase_base_cost`) nunca aparece dos veces aunque
    `get_merged_definitions` también lo liste bajo otro nombre.
    """
    seen: set[str] = set()
    result: list[dict[str, Any]] = []

    for descriptor in descriptors_for(entity_type):
        result.append(_descriptor_to_dict(descriptor))
        seen.add(descriptor.field_id)

    merged = await get_merged_definitions(
        session, tenant_id=tenant_id, vertical_code=vertical_code, entity_type=entity_type
    )
    for field in merged:
        entry = _additional_to_dict(entity_type, field)
        if entry["field_id"] in seen:
            continue
        result.append(entry)
        seen.add(entry["field_id"])

    return result
