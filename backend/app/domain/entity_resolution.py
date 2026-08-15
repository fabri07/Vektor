"""F-ID.3: resolvedor transversal de identidad, puro sobre índices cargados.

Mismo estilo que ``ingestion_import_service._resolve_product_identity``: los
índices se cargan UNA vez por corrida (fuera de este módulo — acá no hay
sesión ni I/O) y se consultan por fila. La regla central, la que corrige el
"gana el primero que matchea" de un resolvedor ingenuo: si dos identificadores
FUERTES de la misma fila apuntan a entidades DISTINTAS, el resultado es
``conflict`` — nunca se elige uno de los dos en silencio.

``references``/el índice esperan valores YA NORMALIZADOS — este módulo no
normaliza (barcode, SKU, email, teléfono y nombre normalizan cada uno
distinto; esa responsabilidad es del caller que arma el índice, ver F-ID.7
cuando esto se cablee contra columnas reales).

"name" es SIEMPRE el tier más débil, nunca "fuerte": un match único por
nombre alcanza para ``resolved``, pero nunca participa de un ``conflict``
contra un tier fuerte — un nombre parecido no puede contradecir un código.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal

from app.domain.entity_code import EntityKind

Status = Literal["resolved", "not_found", "ambiguous", "conflict"]

#: Orden de precedencia por entidad. "name" siempre último y siempre débil.
PRODUCT_TIER_ORDER: tuple[str, ...] = (
    "vektor_code",
    "barcode",
    "sku",
    "alias",
    "name_brand",
    "name",
)
CUSTOMER_TIER_ORDER: tuple[str, ...] = (
    "vektor_code",
    "business_code",
    "dni",
    "cuit",
    "email",
    "phone",
    "alias",
    "name",
)
SUPPLIER_TIER_ORDER: tuple[str, ...] = (
    "vektor_code",
    "business_code",
    "cuit",
    "email",
    "phone",
    "alias",
    "name",
)

_TIER_ORDER: dict[EntityKind, tuple[str, ...]] = {
    "product": PRODUCT_TIER_ORDER,
    "customer": CUSTOMER_TIER_ORDER,
    "supplier": SUPPLIER_TIER_ORDER,
}

_WEAK_TIER = "name"


@dataclass(frozen=True)
class IdentityConflict:
    """Dos (o más) tiers fuertes de la misma fila apuntaron a entidades
    distintas. ``by_tier`` deja explícito quién dijo qué, para que el mensaje
    al usuario pueda nombrar el identificador que se contradice."""

    by_tier: tuple[tuple[str, uuid.UUID], ...]


@dataclass(frozen=True)
class EntityResolution:
    status: Status
    entity_id: uuid.UUID | None = None
    matched_by: tuple[str, ...] = ()
    candidates: tuple[uuid.UUID, ...] = ()
    conflicts: tuple[IdentityConflict, ...] = ()


@dataclass(frozen=True)
class EntityReferenceIndex:
    """``by_tier[tier][valor_normalizado] = frozenset(entity_id, ...)``.

    Un tier con más de un ``entity_id`` para el mismo valor es, en sí mismo,
    ambiguo (dos entidades activas reclamando el mismo código — no debería
    pasar si la unicidad de arriba se respeta, pero el resolvedor no lo
    asume: lo verifica).
    """

    by_tier: dict[str, dict[str, frozenset[uuid.UUID]]] = field(default_factory=dict)

    def lookup(self, tier: str, normalized_value: str) -> frozenset[uuid.UUID]:
        return self.by_tier.get(tier, {}).get(normalized_value, frozenset())


def resolve_entity_reference(
    entity_type: EntityKind,
    index: EntityReferenceIndex,
    references: dict[str, str],
) -> EntityResolution:
    """``references``: ``{tier: valor_normalizado}`` para la fila a resolver.
    Tiers sin valor (ausentes o vacíos) se ignoran. Ver el docstring del
    módulo para las reglas de precedencia/conflicto/ambigüedad.
    """
    tier_order = _TIER_ORDER[entity_type]

    strong_hits: list[tuple[str, frozenset[uuid.UUID]]] = []
    for tier in tier_order:
        if tier == _WEAK_TIER:
            continue
        value = references.get(tier)
        if not value:
            continue
        hit = index.lookup(tier, value)
        if hit:
            strong_hits.append((tier, hit))

    if strong_hits:
        resolution = _resolve_from_hits(strong_hits)
        if resolution is not None:
            return resolution

    weak_value = references.get(_WEAK_TIER)
    if weak_value:
        weak_hit = index.lookup(_WEAK_TIER, weak_value)
        if len(weak_hit) == 1:
            return EntityResolution(
                status="resolved",
                entity_id=next(iter(weak_hit)),
                matched_by=(_WEAK_TIER,),
            )
        if len(weak_hit) > 1:
            return EntityResolution(
                status="ambiguous",
                candidates=tuple(sorted(weak_hit, key=str)),
            )

    return EntityResolution(status="not_found")


def _resolve_from_hits(
    strong_hits: list[tuple[str, frozenset[uuid.UUID]]],
) -> EntityResolution | None:
    """``None`` si ningún tier fuerte dio un resultado accionable (todos
    ambiguos entre sí de forma que ni siquiera se puede armar `conflict` —
    no ocurre en la práctica, pero el tipo de retorno lo deja explícito)."""
    distinct_entities: set[uuid.UUID] = set()
    for _, hit in strong_hits:
        distinct_entities |= hit

    if len(distinct_entities) == 1:
        matched_by = tuple(tier for tier, _ in strong_hits)
        return EntityResolution(
            status="resolved", entity_id=next(iter(distinct_entities)), matched_by=matched_by
        )

    # Algún tier individual ya es ambiguo por sí solo (>1 entidad para su
    # propio valor) y ningún otro tier lo desempata a una sola.
    if len(strong_hits) == 1:
        return EntityResolution(
            status="ambiguous", candidates=tuple(sorted(distinct_entities, key=str))
        )

    # Más de un tier fuerte, y entre todos señalan MÁS de una entidad: dos
    # identificadores fuertes de la misma fila se contradicen. Conflict,
    # nunca gana el primero en silencio.
    by_tier = tuple((tier, entity_id) for tier, hit in strong_hits for entity_id in hit)
    return EntityResolution(
        status="conflict",
        candidates=tuple(sorted(distinct_entities, key=str)),
        conflicts=(IdentityConflict(by_tier=by_tier),),
    )
