"""Cambio 1 del plan de conservación de datos — catálogo de LECTURA combinado.

Compone dos fuentes en un único listado, sin duplicados, para
`GET /fields/definitions/available`:

1. `domain/business_field_catalog.py` — campos canónicos/evidencia, estáticos.
2. `field_definition_service.get_merged_definitions(include_disabled=True)` —
   campos del vertical + del tenant, DECLARADOS a mano o RECUPERADOS por el
   backfill de `scripts/backfill_field_definitions_from_history.py`.

No reemplaza `get_merged_definitions` (que sigue siendo el contrato de
EDICIÓN/ERD existente) — es una proyección de LECTURA por encima, pensada para
que una tabla o un exportador sepan, de una sola consulta, "¿qué campos de
esta entidad puedo mostrar/exportar y dónde vive cada uno?".

Invariante central, corregido en la revisión del primer commit: un campo del
VERTICAL (`is_base_field=True`, ej. "Color"/"Estilo" en decoración del hogar)
NO implica columna real del ORM — la mayoría vive en `custom_fields` porque el
modelo no tiene esa columna. La única fuente de verdad sobre "esto SÍ es una
columna real" es el catálogo estático (`descriptors_for`), que se construyó
leyendo `models/product.py`. Todo lo que llega por `get_merged_definitions`
y no está en ese catálogo estático es, por construcción, `custom_fields`.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.field_definition_service import get_merged_definitions
from app.domain.business_field_catalog import FieldDescriptor, descriptors_for, field_id_for
from app.domain.verticals import Vertical
from app.persistence.models.field_definitions import TenantFieldChangeLog

#: Marca que `scripts/backfill_field_definitions_from_history.py` escribe en
#: `TenantFieldChangeLog.new_state["source"]` al crear una definición. Un campo
#: recuperado así arrancó SIN que nadie lo haya revisado — auditar un tenant
#: (ASTERIA) no clasifica las claves de todos los negocios, así que el plan
#: exige que empiece de solo lectura.
_BACKFILL_SOURCE = "backfill_field_definitions_from_history"


def _descriptor_to_dict(d: FieldDescriptor, *, override: dict[str, Any] | None) -> dict[str, Any]:
    """Un descriptor estático puede tener una personalización real del tenant
    encima (label/enum_options) — SOLO si `override["is_base_field"]` es
    `True`, es decir, si de verdad es la misma fila de `VerticalFieldDefinition`
    customizada, y no un campo libre que coincide de casualidad en field_key
    (ver el docstring del módulo y `field_id_for`)."""
    override_valido = (
        override if override is not None and override.get("is_base_field") is True else None
    )
    return {
        "field_id": d.field_id,
        "entity_type": d.entity_type,
        "field_key": d.field_key,
        "label": override_valido["label"] if override_valido is not None else d.label,
        "data_type": d.data_type,
        "unit": d.unit,
        "origin": d.origin,
        "value_path": d.value_path,
        "enum_options": (
            override_valido.get("enum_options")
            if override_valido is not None
            else list(d.enum_options or ())
        ),
        "editable": d.editable,
        "exportable": d.exportable,
        "searchable": d.searchable,
        "default_visible": d.default_visible,
        # Un campo canónico/evidencia no tiene "apagado para nuevas cargas" —
        # esa noción es del mapeo de columnas, y estos campos no pasan por ahí.
        "enabled_for_new_entries": True,
        "financial_rule": d.financial_rule,
    }


def _additional_to_dict(
    entity_type: str, field: dict[str, Any], *, backfilled: bool
) -> dict[str, Any]:
    field_key = field["field_key"]
    # Cualquier entrada que llega ACÁ (no consumida como override de un
    # descriptor estático) vive en custom_fields — sea un campo del vertical
    # sin columna real (Color/Estilo) o uno libre del tenant. Ver docstring.
    value_path = f"custom_fields.{field_key}"
    return {
        "field_id": field_id_for(entity_type, value_path),
        "entity_type": entity_type,
        "field_key": field_key,
        "label": field["label"],
        "data_type": field["data_type"],
        "unit": None,
        "origin": "canonical" if field.get("is_base_field") else "additional",
        "value_path": value_path,
        "enum_options": field.get("enum_options"),
        # Recuperado por el backfill y todavía sin revisar → NUNCA editable de
        # entrada (regla explícita del plan). Declarado a mano por el tenant,
        # o ya tocado después de la recuperación → editable como siempre.
        "editable": not backfilled,
        "exportable": True,
        "searchable": True,
        "default_visible": True,
        "enabled_for_new_entries": field.get("is_enabled", True),
        "financial_rule": None,
    }


async def _claves_recuperadas_sin_revisar(
    session: AsyncSession, tenant_id: uuid.UUID, entity_type: str
) -> set[str]:
    """`field_key` de las definiciones que el backfill creó y que NADIE volvió
    a tocar desde entonces (ninguna entrada posterior en cualquier de las dos
    columnas de estado). Una vez editadas por un humano vía `PATCH
    /fields/{key}` (evidencia de revisión), dejan de contar como "sin revisar"
    — el plan pide arrancar de solo lectura, no quedar así para siempre."""
    rows = (
        await session.execute(
            select(TenantFieldChangeLog.field_key, TenantFieldChangeLog.new_state)
            .where(
                TenantFieldChangeLog.tenant_id == tenant_id,
                TenantFieldChangeLog.entity_type == entity_type,
            )
            .order_by(TenantFieldChangeLog.changed_at.asc())
        )
    ).all()

    origen_backfill: set[str] = set()
    tocado_despues: set[str] = set()
    for field_key, new_state in rows:
        if isinstance(new_state, dict) and new_state.get("source") == _BACKFILL_SOURCE:
            origen_backfill.add(field_key)
        elif field_key in origen_backfill:
            tocado_despues.add(field_key)
    return origen_backfill - tocado_despues


async def get_available_fields(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    vertical_code: Vertical,
    entity_type: str,
) -> list[dict[str, Any]]:
    """Todos los campos consultables de una entidad, con identidad estable.

    Orden: canónicos/evidencia primero (misma secuencia que ya usa el mapeo),
    adicionales después. Identidad = `(entity_type, value_path)` — ver
    `field_id_for` — así que dos campos que apuntan a lugares DISTINTOS nunca
    se pisan, aunque compartan `field_key` o label.

    Incluye deshabilitados (`include_disabled=True`): un campo apagado para
    nuevas cargas sigue siendo consultable en el histórico.
    """
    merged = await get_merged_definitions(
        session,
        tenant_id=tenant_id,
        vertical_code=vertical_code,
        entity_type=entity_type,
        include_disabled=True,
    )
    merged_by_key = {f["field_key"]: f for f in merged}
    sin_revisar = await _claves_recuperadas_sin_revisar(session, tenant_id, entity_type)

    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    consumidas: set[str] = set()  # field_keys de `merged` ya representadas como override estático

    for descriptor in descriptors_for(entity_type):
        override = merged_by_key.get(descriptor.field_key)
        entry = _descriptor_to_dict(descriptor, override=override)
        result.append(entry)
        seen_ids.add(entry["field_id"])
        if override is not None and override.get("is_base_field") is True:
            consumidas.add(descriptor.field_key)

    for field in merged:
        if field["field_key"] in consumidas:
            continue
        entry = _additional_to_dict(
            entity_type, field, backfilled=field["field_key"] in sin_revisar
        )
        if entry["field_id"] in seen_ids:
            continue
        result.append(entry)
        seen_ids.add(entry["field_id"])

    return result
