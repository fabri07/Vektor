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
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services._savepoint import (
    SavepointConflictError,
    guarded_savepoint,
    unique_violation_classifier,
)
from app.domain.entity_code import (
    CUSTOMER_PREFIX,
    SUPPLIER_PREFIX,
    EntityKind,
    format_code,
    product_prefix_for,
)
from app.domain.text_norm import normalize_external_code
from app.domain.verticals import Vertical
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.customer import Customer
from app.persistence.models.entity_code_sequence import EntityCodeSequence
from app.persistence.models.entity_identifier import EntityIdentifier
from app.persistence.models.product import Product
from app.persistence.models.supplier import Supplier

_CodeableEntity = Product | Customer | Supplier

#: Decisión auditada en CADA asignación en vivo (los 8 sitios reales de F-ID.5:
#: HTTP, chat, imports, remitos) — distinta de "ENTITY_CODE_BACKFILL", que sólo
#: emite `backfill_entity_code.py` sobre lo que ya existía antes de F-ID.
_DECISION_TYPE_ASSIGNED = "ENTITY_CODE_ASSIGNED"


class EntityIdentifierConflictError(Exception):
    """El valor ya está registrado, VIGENTE, para una entidad DISTINTA dentro
    del mismo ``(tenant, entity_type, identifier_type, namespace)``.

    Nunca se resuelve solo — ver el índice único de ``entity_identifiers``
    (``uq_entity_identifiers_active_value``): dos identificadores fuertes que
    apuntan a entidades distintas es un conflicto real, no algo para que el
    primero en llegar gane en silencio (regla del resolvedor, F-ID.3).
    """

    def __init__(self, identifier_type: str, value: str, existing_entity_id: uuid.UUID) -> None:
        super().__init__(
            f"{identifier_type}={value!r} ya pertenece a otra entidad ({existing_entity_id})"
        )
        self.identifier_type = identifier_type
        self.value = value
        self.existing_entity_id = existing_entity_id

_SEQUENCE_CONFLICT = unique_violation_classifier(
    "sequence",
    constraint="uq_entity_code_sequences_tenant_type_prefix",
    columns=(
        "entity_code_sequences.tenant_id",
        "entity_code_sequences.entity_type",
        "entity_code_sequences.prefix",
    ),
)

_IDENTIFIER_CONFLICT = unique_violation_classifier(
    "identifier",
    constraint="uq_entity_identifiers_active_value",
    columns=(
        "entity_identifiers.tenant_id",
        "entity_identifiers.entity_type",
        "entity_identifiers.identifier_type",
        "entity_identifiers.namespace",
        "entity_identifiers.normalized_value",
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


@dataclass(frozen=True)
class EntityCodeSpec:
    """Qué campo lleva el código Véktor de cada entidad y cómo distinguir su
    procedencia — producto usa ``sku`` (dual: puede venir del negocio, decisión
    ya cerrada por F-S) y necesita ``origin_key`` para no pisarlo jamás;
    cliente/proveedor usan ``vektor_code``, una columna que SÓLO Véktor
    escribe (el código que trae el negocio va a ``entity_identifiers`` con
    ``namespace="business"``, nunca a esta columna) — no necesitan distinguir
    procedencia, ``origin_key`` es ``None``.
    """

    kind: EntityKind
    code_field: str
    origin_key: str | None


PRODUCT_CODE_SPEC = EntityCodeSpec(kind="product", code_field="sku", origin_key="_sku_origin")
CUSTOMER_CODE_SPEC = EntityCodeSpec(kind="customer", code_field="vektor_code", origin_key=None)
SUPPLIER_CODE_SPEC = EntityCodeSpec(kind="supplier", code_field="vektor_code", origin_key=None)


def _prefix_for(
    spec: EntityCodeSpec, *, vertical: Vertical | None, category: str | None
) -> str:
    if spec.kind == "product":
        return product_prefix_for(vertical, category)
    return CUSTOMER_PREFIX if spec.kind == "customer" else SUPPLIER_PREFIX


def needs_vektor_code(entity: _CodeableEntity, spec: EntityCodeSpec) -> bool:
    """``True`` si :func:`assign_vektor_code_if_missing` le asignaría un código a
    esta entidad — sin tocar la sesión ni el lock de fila de
    ``assign_next_sequence``. Para previews (dry-run) que necesitan reportar
    cobertura sin pagar el costo de escritura real (ver ``backfill_entity_code.py``:
    un dry-run que igual llama a la asignación toma locks reales durante todo
    el scan, contradiciendo "read-only por default").

    Misma regla que la primera mitad de ``assign_vektor_code_if_missing`` — se
    extrae para que las dos no puedan divergir.
    """
    if getattr(entity, "is_sentinel", False):
        return False
    return not getattr(entity, spec.code_field)


async def assign_vektor_code_if_missing(
    session: AsyncSession,
    entity: _CodeableEntity,
    spec: EntityCodeSpec,
    tenant_id: uuid.UUID,
    *,
    vertical: Vertical | None = None,
    category: str | None = None,
    now: datetime | None = None,
) -> bool:
    """``True`` si asignó un código nuevo; ``False`` si ya tenía uno o es
    sentinela — nunca lanza por esas dos razones, son caminos normales.

    Precondición: ``entity`` ya tiene ``id`` asignado (flusheada al menos una
    vez) — sin eso la fila de ``entity_identifiers`` quedaría huérfana, y
    escribir un identificador sin saber a qué entidad apunta es exactamente
    lo que este módulo existe para evitar. Se verifica, no se asume.

    Nunca pisa un código existente (del negocio o ya generado antes) ni
    asigna al sentinela ("Local"/"No identificado", vía ``entity.is_sentinel``
    — ``Product`` no tiene esa property, ``getattr`` con default cubre el
    caso). La fila permanente en ``entity_identifiers`` es lo que hace el
    no-reciclo verificable después: desactivar o fusionar la entidad no la
    borra.
    """
    if not needs_vektor_code(entity, spec):
        return False
    if entity.id is None:
        raise ValueError(
            f"assign_vektor_code_if_missing: {spec.kind} sin id — flushear antes de llamar"
        )

    prefix = _prefix_for(spec, vertical=vertical, category=category)
    seq = await assign_next_sequence(session, tenant_id, spec.kind, prefix)
    code = format_code(prefix, seq)

    setattr(entity, spec.code_field, code)
    if spec.origin_key is not None:
        entity.custom_fields = {**(entity.custom_fields or {}), spec.origin_key: "vektor"}

    ts = now or datetime.now(UTC)
    session.add(
        EntityIdentifier(
            tenant_id=tenant_id,
            entity_type=spec.kind,
            entity_id=entity.id,
            identifier_type="vektor_code",
            namespace="vektor",
            raw_value=code,
            normalized_value=normalize_external_code(code) or code,
            origin="vektor",
            is_primary=True,
            first_seen_at=ts,
            last_seen_at=ts,
        )
    )
    session.add(
        DecisionAuditLog(
            tenant_id=tenant_id,
            decision_type=_DECISION_TYPE_ASSIGNED,
            decision_data={
                "entity_type": spec.kind,
                "entity_id": str(entity.id),
                "name": getattr(entity, "name", None),
                "codigo": code,
            },
            triggered_by="entity_code_service.assign_vektor_code_if_missing",
            created_at=ts,
        )
    )
    return True


async def record_identifier(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    entity_type: EntityKind,
    entity_id: uuid.UUID,
    identifier_type: str,
    namespace: str,
    raw_value: str,
    origin: str,
    *,
    source_upload_id: uuid.UUID | None = None,
    created_by_user_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> EntityIdentifier:
    """Registra (o refresca) un identificador externo para una entidad.

    Idempotente por valor NORMALIZADO dentro de ``(tenant, entity_type,
    identifier_type, namespace)``: si ya existe una fila VIGENTE con ese
    valor para la MISMA entidad, sólo actualiza ``last_seen_at`` (no duplica
    filas por releer el mismo dato en cada import). Si la fila vigente
    pertenece a OTRA entidad, es un conflicto real — se levanta
    ``EntityIdentifierConflictError`` en vez de reasignarla en silencio.

    El INSERT va DENTRO de un ``guarded_savepoint`` (mismo patrón que
    ``assign_next_sequence``): dos llamadas concurrentes que no ven la fila del
    otro en su SELECT insertarían ambas y la segunda chocaría contra
    ``uq_entity_identifiers_active_value`` con un ``IntegrityError`` crudo que,
    sin savepoint, aborta TODA la transacción externa en PostgreSQL (ver
    ``_savepoint.py``). Con el guard, la segunda re-consulta tras perder la
    carrera y resuelve igual que si hubiera visto la fila desde el principio.
    """
    normalized = normalize_external_code(raw_value)
    if not normalized:
        raise ValueError(f"record_identifier: valor vacío para {identifier_type!r}")

    ts = now or datetime.now(UTC)

    async def _find_active() -> EntityIdentifier | None:
        return (
            await session.execute(
                sa.select(EntityIdentifier).where(
                    EntityIdentifier.tenant_id == tenant_id,
                    EntityIdentifier.entity_type == entity_type,
                    EntityIdentifier.identifier_type == identifier_type,
                    EntityIdentifier.namespace == namespace,
                    EntityIdentifier.normalized_value == normalized,
                    EntityIdentifier.revoked_at.is_(None),
                )
            )
        ).scalar_one_or_none()

    existing = await _find_active()
    if existing is not None:
        if existing.entity_id != entity_id:
            raise EntityIdentifierConflictError(identifier_type, raw_value, existing.entity_id)
        existing.last_seen_at = ts
        return existing

    row = EntityIdentifier(
        tenant_id=tenant_id,
        entity_type=entity_type,
        entity_id=entity_id,
        identifier_type=identifier_type,
        namespace=namespace,
        raw_value=raw_value,
        normalized_value=normalized,
        origin=origin,
        is_primary=True,
        first_seen_at=ts,
        last_seen_at=ts,
        source_upload_id=source_upload_id,
        created_by_user_id=created_by_user_id,
    )
    try:
        async with guarded_savepoint(session, _IDENTIFIER_CONFLICT):
            session.add(row)
    except SavepointConflictError:
        existing = await _find_active()
        if existing is None:
            # El conflicto era real (el índice único disparó) pero la fila
            # vigente ya no aparece — otra transacción la revocó entre medio.
            # No hay nada seguro para devolver: quien llama tiene que reintentar.
            raise RuntimeError(
                "record_identifier: conflicto de unicidad sin fila vigente "
                f"para {identifier_type}={raw_value!r} — reintentar"
            ) from None
        if existing.entity_id != entity_id:
            raise EntityIdentifierConflictError(
                identifier_type, raw_value, existing.entity_id
            ) from None
        existing.last_seen_at = ts
        return existing
    return row
