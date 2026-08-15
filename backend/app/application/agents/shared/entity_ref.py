"""F-ID.10 — helper de display de sólo lectura para agentes.

Cualquier sub-agente que ya resolvió un UUID de producto/cliente/proveedor
(nunca lo inventa — lo recibe de un repo/query propio) puede pedirle a este
helper cómo mostrarlo: ``{id, code, display_name}``, p. ej. "Juan Pérez
(CLI-0042)". No resuelve identidad (eso es `identity_resolution.py`) ni reemplaza el
tooling interno de cada agente — es sólo formateo, de sólo lectura, sobre una
entidad que YA se conoce.

No inventa: si la entidad no existe (o no pertenece al tenant) devuelve
``None`` en vez de fabricar un display — no-invention rule.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.entity_code import EntityKind
from app.persistence.models.customer import Customer
from app.persistence.models.product import Product
from app.persistence.models.supplier import Supplier

#: Constreñido a las 3 entidades — evita que mypy infiera el `Base` genérico
#: (mismo problema/fix que `EntityCodeSpec.code_field` en `backfill_entity_code.py`).
_M = TypeVar("_M", Product, Customer, Supplier)

#: Producto usa `sku` como su código Véktor (decisión F-S, no se migra).
#: Cliente/proveedor usan la columna denormalizada `vektor_code` (F-ID.2).
_CODE_FIELD_BY_KIND: dict[EntityKind, str] = {
    "product": "sku",
    "customer": "vektor_code",
    "supplier": "vektor_code",
}


async def _fetch(
    session: AsyncSession, model: type[_M], tenant_id: uuid.UUID, entity_id: uuid.UUID
) -> _M | None:
    return (
        await session.execute(
            select(model).where(model.tenant_id == tenant_id, model.id == entity_id)
        )
    ).scalar_one_or_none()


@dataclass(frozen=True)
class EntityRef:
    id: str
    code: str | None
    display_name: str


async def get_entity_ref(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    entity_type: EntityKind,
    entity_id: uuid.UUID,
) -> EntityRef | None:
    """`None` si la entidad no existe o no pertenece al tenant — nunca inventa.

    Sin código (sentinela, o histórico previo a F-ID.6) → `display_name` es
    sólo el nombre, sin paréntesis vacíos.
    """
    row: Product | Customer | Supplier | None
    if entity_type == "product":
        row = await _fetch(session, Product, tenant_id, entity_id)
    elif entity_type == "customer":
        row = await _fetch(session, Customer, tenant_id, entity_id)
    else:
        row = await _fetch(session, Supplier, tenant_id, entity_id)
    if row is None:
        return None

    code = getattr(row, _CODE_FIELD_BY_KIND[entity_type], None)
    code = code.strip() if isinstance(code, str) else None
    name = row.name
    display_name = f"{name} ({code})" if code else name
    return EntityRef(id=str(row.id), code=code or None, display_name=display_name)
