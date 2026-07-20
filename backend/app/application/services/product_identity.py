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

    ``existing`` es el ocupante; ``matched_by`` es ``"barcode"`` o ``"sku"``.
    """

    def __init__(self, existing: Product, matched_by: MatchedBy) -> None:
        super().__init__(f"identidad de producto ya ocupada por {matched_by}: {existing.id}")
        self.existing = existing
        self.matched_by = matched_by


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
    for kind, value in (("barcode", normalize_barcode(barcode)), ("sku", normalize_sku(sku))):
        if not value:
            continue
        column = Product.barcode_normalized if kind == "barcode" else Product.sku_normalized
        stmt = select(Product).where(
            Product.tenant_id == tenant_id,
            Product.is_active.is_(True),
            column == value,
        )
        if exclude_id is not None:
            stmt = stmt.where(Product.id != exclude_id)
        found = (await session.execute(stmt)).scalars().first()
        if found is not None:
            return found, kind  # type: ignore[return-value]
    return None, None


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
        ProductIdentityConflictError: con ``on_conflict="raise"``.
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
        existing, hit_by = await find_active_by_identity(
            session, tenant_id, barcode=barcode, sku=sku
        )
        if existing is None:  # pragma: no cover — el índice garantiza que exista
            raise conflict.original from conflict
        resolved: MatchedBy = hit_by or matched_by
        if on_conflict == "raise":
            raise ProductIdentityConflictError(existing, resolved) from conflict.original
        logger.warning(
            "product_identity.create_race",
            tenant_id=str(tenant_id),
            matched_by=resolved,
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
        existing, hit_by = await find_active_by_identity(
            session, tenant_id, barcode=barcode, sku=sku, exclude_id=exclude_id
        )
        if existing is None:  # pragma: no cover — el índice garantiza que exista
            raise conflict.original from conflict
        raise ProductIdentityConflictError(existing, hit_by or matched_by) from conflict.original
