"""F-ID: asignación atómica del correlativo de código Véktor.

``assign_next_sequence`` es la única forma de sacar un número de
``entity_code_sequences`` — nunca ``SELECT MAX(...)+1``. El ``UPDATE ...
RETURNING`` toma un lock de fila que serializa cualquier otra transacción
pidiendo el mismo ``(tenant, entity_type, prefix)`` al mismo tiempo: dos
llamadas concurrentes nunca reciben el mismo valor, y un rollback deja un
hueco en vez de reciclar un número ya entregado (aceptable — no es
numeración contable).
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services._savepoint import (
    SavepointConflictError,
    guarded_savepoint,
    unique_violation_classifier,
)
from app.domain.entity_code import EntityKind
from app.persistence.models.entity_code_sequence import EntityCodeSequence

_SEQUENCE_CONFLICT = unique_violation_classifier(
    "sequence",
    constraint="uq_entity_code_sequences_tenant_type_prefix",
    columns=(
        "entity_code_sequences.tenant_id",
        "entity_code_sequences.entity_type",
        "entity_code_sequences.prefix",
    ),
)


async def _try_increment(
    session: AsyncSession, tenant_id: uuid.UUID, entity_type: str, prefix: str
) -> int | None:
    """``None`` si la fila todavía no existe; si no, el valor ASIGNADO (el
    ``next_value`` de antes de incrementar, no el de después)."""
    stmt = (
        sa.update(EntityCodeSequence)
        .where(
            EntityCodeSequence.tenant_id == tenant_id,
            EntityCodeSequence.entity_type == entity_type,
            EntityCodeSequence.prefix == prefix,
        )
        .values(next_value=EntityCodeSequence.next_value + 1)
        .returning(EntityCodeSequence.next_value)
    )
    result = await session.execute(stmt)
    row = result.first()
    if row is None:
        return None
    return int(row[0]) - 1


async def assign_next_sequence(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    entity_type: EntityKind,
    prefix: str,
) -> int:
    """Siguiente valor para ``(tenant_id, entity_type, prefix)``, atómico.

    Si la fila de secuencia ya existe, el ``UPDATE ... RETURNING`` alcanza —
    su lock de fila es la garantía de concurrencia. Si es la primera vez que
    se pide ese prefijo, la fila no existe: se inserta dentro de un
    savepoint, y el índice único es el árbitro real ante dos transacciones
    creándola al mismo tiempo — la que pierde la carrera reintenta el
    UPDATE, que ahora sí encuentra la fila que ganó.
    """
    assigned = await _try_increment(session, tenant_id, entity_type, prefix)
    if assigned is not None:
        return assigned

    try:
        async with guarded_savepoint(session, _SEQUENCE_CONFLICT):
            session.add(
                EntityCodeSequence(
                    tenant_id=tenant_id,
                    entity_type=entity_type,
                    prefix=prefix,
                    next_value=2,  # el valor 1 ya se le asigna a este caller
                )
            )
    except SavepointConflictError:
        pass  # otra transacción ganó la carrera; su fila ya existe, reintentar
    else:
        return 1

    assigned = await _try_increment(session, tenant_id, entity_type, prefix)
    if assigned is None:
        raise RuntimeError(
            "entity_code_sequences: no se pudo asignar secuencia para "
            f"({tenant_id}, {entity_type}, {prefix}) tras reintento"
        )
    return assigned
