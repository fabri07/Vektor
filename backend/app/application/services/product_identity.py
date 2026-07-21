"""Identidad fuerte de producto: get-or-create y guard de unicidad (Fase 5).

Fase 2 dejó las columnas ``*_normalized`` y un motor de identidad a nivel
aplicación, pero los chequeos siguen siendo SELECT-antes-de-INSERT, con ventana
TOCTOU. Fase 5 agrega los índices únicos parciales
(``uq_products_tenant_{barcode,sku}_norm``, sobre productos ACTIVOS) y este módulo
es el que absorbe la colisión en vez de dejarla explotar.

Dos semánticas, deliberadamente separadas:

- :func:`add_product_or_reuse` — altas INTERNAS (import, reparación). Una violación
  del unique NO es ambigüedad: es un match exacto por clave fuerte que el índice
  precargado en memoria no vio. Reusar es lo correcto, y es lo que el resolver
  habría devuelto como ``"linked"``. La ambigüedad real (varios candidatos por
  nombre) la sigue resolviendo ``_resolve_product_identity`` mandando la fila a la
  bandeja "Otros" — eso no cambia.
- :func:`product_identity_guard` — POST/PATCH interactivos. **Nunca** reusa: una API
  no puede devolver el producto de otra request como si lo hubiera creado. Traduce
  la colisión a :class:`ProductIdentityConflictError`, que el router mapea a 409.

Sobre el ordenamiento del SAVEPOINT (el `add`/`setattr` va DENTRO del bloque, no
antes), ver el docstring de :mod:`app.application.services._savepoint`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.inspection import inspect as sa_inspect

from app.application.services._savepoint import SavepointConflictError, guarded_savepoint
from app.domain.text_norm import normalize_barcode, normalize_sku
from app.observability.logger import get_logger
from app.persistence.models.product import Product

logger = get_logger(__name__)

MatchedBy = Literal["barcode", "sku"]

# PostgreSQL/asyncpg exponen el NOMBRE del constraint...
_UQ_NAMES: dict[str, MatchedBy] = {
    "uq_products_tenant_barcode_norm": "barcode",
    "uq_products_tenant_sku_norm": "sku",
}
# ...pero SQLite reporta las COLUMNAS: "UNIQUE constraint failed:
# products.tenant_id, products.sku_normalized". Sin esta segunda forma, el
# clasificador devuelve None en la suite y la colisión se re-propaga.
_UQ_COLUMNS: dict[str, MatchedBy] = {
    "products.barcode_normalized": "barcode",
    "products.sku_normalized": "sku",
}


class ProductIdentityConflictError(Exception):
    """Otro producto ACTIVO del tenant ya ocupa esta clave fuerte.

    ``existing`` es el ocupante de la clave que reportó la DB; ``matched_by`` es
    ``"barcode"`` o ``"sku"``. ``ambiguous`` marca el caso en que barcode y sku
    apuntan a productos DISTINTOS: ahí no hay reuso posible y ni siquiera con
    ``on_conflict="reuse"`` se elige uno.

    ``other`` es el ocupante de la OTRA clave, y solo está presente cuando
    ``ambiguous``. Se expone para que la fila que va a "Otros" muestre los DOS
    productos en disputa: con uno solo, quien revisa no puede entender por qué la
    fila no se resolvió.
    """

    def __init__(
        self,
        existing: Product,
        matched_by: MatchedBy,
        *,
        ambiguous: bool = False,
        other: Product | None = None,
    ) -> None:
        detalle = " (barcode y sku apuntan a productos distintos)" if ambiguous else ""
        super().__init__(
            f"identidad de producto ya ocupada por {matched_by}: {existing.id}{detalle}"
        )
        self.existing = existing
        self.matched_by = matched_by
        self.ambiguous = ambiguous
        self.other = other

    @property
    def candidates(self) -> list[Product]:
        """Los productos en disputa, sin duplicar. Forma lista para ``match_candidates``."""
        return [self.existing] if self.other is None else [self.existing, self.other]


def _violated_identity_index(exc: IntegrityError) -> MatchedBy | None:
    """Qué índice de identidad se violó, o ``None`` si la violación es de otra cosa.

    Discriminar importa: un ``except IntegrityError`` a secas tragaría una violación
    de FK o de NOT NULL y la convertiría en un "ya existía, lo reuso" desconcertante,
    persistiendo datos equivocados en silencio.
    """
    orig = getattr(exc, "orig", None)
    name = getattr(orig, "constraint_name", None)
    if name:
        return _UQ_NAMES.get(str(name))
    text = str(orig or exc)
    for uq, kind in _UQ_NAMES.items():
        if uq in text:
            return kind
    if "UNIQUE" not in text.upper():
        return None
    for column, kind in _UQ_COLUMNS.items():
        if column in text:
            return kind
    return None


def _classify(exc: IntegrityError) -> str | None:
    matched = _violated_identity_index(exc)
    return matched if matched is None else str(matched)


async def find_active_by_identity(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    barcode: str | None = None,
    sku: str | None = None,
    exclude_id: uuid.UUID | None = None,
) -> tuple[Product | None, MatchedBy | None]:
    """Busca el producto ACTIVO que ocupa una clave fuerte del tenant.

    Normaliza con los MISMOS helpers que alimentan las columnas del índice
    (``text_norm``), para no divergir del predicado que evalúa la DB. El barcode
    tiene prioridad: es la clave más fuerte (identifica el artículo físico, no el
    código interno del comercio).
    """
    candidates: tuple[tuple[MatchedBy, str | None], ...] = (
        ("barcode", normalize_barcode(barcode)),
        ("sku", normalize_sku(sku)),
    )
    for kind, value in candidates:
        if not value:
            continue
        found = await _find_by_key(session, tenant_id, kind, value, exclude_id=exclude_id)
        if found is not None:
            return found, kind
    return None, None


async def find_active_owner_of_key(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    kind: MatchedBy,
    raw_value: str | None,
    *,
    exclude_id: uuid.UUID | None = None,
) -> tuple[Product | None, str | None]:
    """Dueño ACTIVO de UNA clave concreta, sin la prioridad barcode-sobre-sku.

    :func:`find_active_by_identity` responde "¿esta identidad está ocupada?" y para
    eso ordena las claves. Quien necesita razonar clave POR clave —el revert del
    dedup, que decide por separado si el barcode y el sku van a colisionar— no puede
    usar esa prioridad: le esconde la colisión de sku cuando el barcode ya matcheó.

    Devuelve además el valor NORMALIZADO (``None`` si la clave viene vacía), para que
    el caller compare claves entre sí con la misma normalización que evalúa el índice
    en vez de re-implementarla.
    """
    normalized = normalize_barcode(raw_value) if kind == "barcode" else normalize_sku(raw_value)
    if not normalized:
        return None, None
    found = await _find_by_key(session, tenant_id, kind, normalized, exclude_id=exclude_id)
    return found, normalized


async def _find_by_key(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    kind: MatchedBy,
    normalized: str,
    *,
    exclude_id: uuid.UUID | None = None,
) -> Product | None:
    """Dueño ACTIVO de UNA clave normalizada concreta (sin prioridades)."""
    column = Product.barcode_normalized if kind == "barcode" else Product.sku_normalized
    stmt = select(Product).where(
        Product.tenant_id == tenant_id,
        Product.is_active.is_(True),
        column == normalized,
    )
    if exclude_id is not None:
        stmt = stmt.where(Product.id != exclude_id)
    return (await session.execute(stmt)).scalars().first()


async def _resolve_conflict_owner(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    matched_by: MatchedBy,
    *,
    barcode: str | None,
    sku: str | None,
    exclude_id: uuid.UUID | None = None,
) -> tuple[Product, Product | None]:
    """Ubica al ocupante de la clave que la DB reportó, y detecta ambigüedad.

    Consulta EXCLUSIVAMENTE ``matched_by``: reconsultar con las dos claves y dejar
    que gane la prioridad de barcode devuelve el producto equivocado cuando el
    barcode pertenece a A, el sku a B y lo que violamos fue el índice de sku.

    Después chequea la OTRA clave: si tiene un dueño ACTIVO distinto, el candidato
    "es" dos productos a la vez. Eso es ambigüedad real y no se resuelve reusando
    ninguno — se marca ``ambiguous`` para que el import lo mande a "Otros".

    Returns:
        ``(ocupante, otro_dueño)``. ``otro_dueño`` es ``None`` salvo que haya
        ambigüedad, en cuyo caso es el producto que ocupa la otra clave.

    Raises:
        LookupError: la clave reportada no tiene dueño (no debería pasar: el índice
            garantiza que exista; el caller re-propaga el IntegrityError original).
    """
    keys: dict[MatchedBy, str | None] = {
        "barcode": normalize_barcode(barcode),
        "sku": normalize_sku(sku),
    }
    reported = keys[matched_by]
    owner = (
        await _find_by_key(session, tenant_id, matched_by, reported, exclude_id=exclude_id)
        if reported
        else None
    )
    if owner is None:
        raise LookupError(matched_by)

    other_kind: MatchedBy = "sku" if matched_by == "barcode" else "barcode"
    other_value = keys[other_kind]
    if not other_value:
        return owner, None
    other_owner = await _find_by_key(
        session, tenant_id, other_kind, other_value, exclude_id=exclude_id
    )
    if other_owner is not None and other_owner.id != owner.id:
        return owner, other_owner
    return owner, None


async def add_product_or_reuse(
    session: AsyncSession,
    product: Product,
    *,
    on_conflict: Literal["reuse", "raise"] = "reuse",
) -> tuple[Product, bool]:
    """Persiste ``product``; si su identidad fuerte ya está ocupada, resuelve.

    ``product`` debe llegar **transient** (sin ``session.add()`` previo): si ya está
    pendiente, el flush incondicional de ``begin_nested()`` emite el INSERT fuera del
    savepoint y la colisión aborta la transacción entera.

    Returns:
        ``(producto, creado)``. Con ``on_conflict="reuse"`` y colisión devuelve
        ``(existente, False)``.

    Raises:
        ProductIdentityConflictError: con ``on_conflict="raise"``, y SIEMPRE que
            barcode y sku pertenezcan a productos distintos (ambigüedad real: no hay
            un "el existente" que reusar — el import debe mandarlo a "Otros").
        IntegrityError: si la violación no era de identidad (FK, NOT NULL, ...).
    """
    state = sa_inspect(product)
    if not state.transient:
        raise AssertionError(
            "add_product_or_reuse espera un Product TRANSIENT: no hagas session.add() "
            "antes de llamarlo, o el INSERT se emite fuera del savepoint "
            "(ver app/application/services/_savepoint.py)."
        )

    tenant_id, barcode, sku = product.tenant_id, product.barcode, product.sku
    try:
        async with guarded_savepoint(session, _classify):
            session.add(product)
    except SavepointConflictError as conflict:
        matched_by: MatchedBy = conflict.constraint  # type: ignore[assignment]
        try:
            existing, other = await _resolve_conflict_owner(
                session, tenant_id, matched_by, barcode=barcode, sku=sku
            )
        except LookupError:  # pragma: no cover — el índice garantiza que exista
            raise conflict.original from conflict
        if other is not None or on_conflict == "raise":
            raise ProductIdentityConflictError(
                existing, matched_by, ambiguous=other is not None, other=other
            ) from conflict.original
        logger.warning(
            "product_identity.create_race",
            tenant_id=str(tenant_id),
            matched_by=matched_by,
            existing_id=str(existing.id),
        )
        return existing, False
    return product, True


@asynccontextmanager
async def product_identity_guard(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    barcode: str | None = None,
    sku: str | None = None,
    exclude_id: uuid.UUID | None = None,
) -> AsyncIterator[None]:
    """Protege un alta o edición interactiva de producto contra la colisión.

    El caller **muta dentro del bloque** — hace el ``session.add()`` de un producto
    nuevo, o los ``setattr`` de un PATCH. Pasarle un objeto ya mutado no sirve: si el
    ``setattr`` ya ocurrió, el objeto está ``dirty`` y el flush incondicional de
    ``begin_nested()`` emite el UPDATE fuera del savepoint.

    ``barcode``/``sku`` son los valores que va a tener el producto DESPUÉS de la
    mutación; se usan para ubicar al ocupante si hay colisión. ``exclude_id`` es el
    id del producto que se está editando (para no reportarse a sí mismo).

    Raises:
        ProductIdentityConflictError: el router lo mapea a 409.
    """
    try:
        async with guarded_savepoint(session, _classify):
            yield
    except SavepointConflictError as conflict:
        matched_by: MatchedBy = conflict.constraint  # type: ignore[assignment]
        try:
            existing, other = await _resolve_conflict_owner(
                session, tenant_id, matched_by, barcode=barcode, sku=sku, exclude_id=exclude_id
            )
        except LookupError:  # pragma: no cover — el índice garantiza que exista
            raise conflict.original from conflict
        raise ProductIdentityConflictError(
            existing, matched_by, ambiguous=other is not None, other=other
        ) from conflict.original
