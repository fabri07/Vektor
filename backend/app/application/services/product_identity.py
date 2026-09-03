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
from collections.abc import AsyncIterator, Awaitable, Callable
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

#: Post-trabajo de un alta encolada: `(producto_final, creado)`. Corre recién
#: cuando la identidad está resuelta — ver `ProductCreateBatch`.
_PostAlta = Callable[[Product, bool], Awaitable[None]]

#: Qué hacer con un alta encolada que resultó AMBIGUA al resolver la carrera
#: (barcode y sku de productos distintos): no hay un existente que reusar.
_AlSerAmbiguo = Callable[["ProductIdentityConflictError"], Awaitable[None]]

#: `(producto, post-trabajo, qué-hacer-si-es-ambiguo)`.
_Pendiente = tuple[Product, _PostAlta, _AlSerAmbiguo | None]

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


class ProductCreateBatch:
    """Altas de producto por LOTE: un savepoint por lote en vez de uno por producto.

    Por qué existe
    --------------
    ``add_product_or_reuse`` cuesta 3 round-trips por producto —``flush`` previo,
    ``SAVEPOINT``, ``RELEASE``— y, peor, su ``flush`` **drena todo lo pendiente**: en
    un import el batch de 500 filas nunca llega a agrupar nada, así que cada
    ``InventoryMovement`` y cada ``InventoryBalance`` sale en su propio INSERT. Medido
    sobre el archivo real de Asteria (``scripts/bench_confirm_import.py``): 3.250
    statements, de los cuales **1.588 (48,9%) eran SAVEPOINT + RELEASE**.

    El molde es ``stock_service.decrement_stock_bulk`` (PR #53): lote optimista,
    fallback de a uno cuando el lote choca.

    El contrato que hace esto seguro
    --------------------------------
    **El post-trabajo de un alta NO corre hasta que la identidad final está
    resuelta.** El caller entrega, junto al producto, la corrutina que quiere correr
    después (``al_resolver(producto_final, creado)``), y el lote la ejecuta recién
    tras el flush. Sin eso, un alta que en el flush resulta ser una carrera —el SKU
    lo ocupó otra transacción entre medio— dejaría movimientos, vínculos y filas de
    ledger apuntando a un id que nunca se insertó, y habría que salir a remapearlos.
    Con este orden, esa situación no existe.

    Lo único que el caller SÍ tiene que registrar en el momento son sus índices en
    memoria de identidad (para que dos filas del mismo archivo no encolen dos altas
    del mismo producto). Si el flush sustituye una de esas altas, ``flush`` devuelve
    las sustituciones para que el caller corrija esos índices — un mapa acotado y
    verificable, no una búsqueda por toda la sesión.
    """

    def __init__(self, session: AsyncSession, *, chunk_size: int = 200) -> None:
        self._session = session
        self._chunk_size = chunk_size
        self._pendientes: list[_Pendiente] = []
        #: Índice de lo encolado. Un `any(...)` sobre la lista volvía `esta_encolado`
        #: O(n), y el caller lo consulta por fila repetida: en un catálogo grande con
        #: muchas identidades repetidas eso es O(n²) de CPU puro.
        self._ids_encolados: set[uuid.UUID] = set()

    def __len__(self) -> int:
        return len(self._pendientes)

    def esta_encolado(self, product_id: uuid.UUID) -> bool:
        """¿Este id corresponde a un alta que todavía no se insertó?

        El caller lo necesita antes de escribir CUALQUIER cosa que lo referencie por
        FK —un movimiento de inventario, un vínculo con proveedor—: mientras el alta
        está en la cola, el producto no existe en la base y ese INSERT explota con
        una violación de foreign key en el próximo flush.
        """
        return product_id in self._ids_encolados

    def encolar(
        self,
        product: Product,
        al_resolver: _PostAlta,
        *,
        al_ser_ambiguo: _AlSerAmbiguo | None = None,
    ) -> None:
        """Encola un alta. El producto debe llegar **transient** (igual que
        ``add_product_or_reuse``: el ``session.add`` lo hace el lote, dentro del
        savepoint).

        ``al_ser_ambiguo``: qué hacer si al resolver la carrera resulta que el
        barcode y el sku pertenecen a productos DISTINTOS. No hay "el existente" que
        reusar, así que el lote no puede decidir solo; sin este callback la
        excepción se propaga (que es lo correcto para un caller que no sabe qué
        hacer con una fila ambigua).
        """
        if not sa_inspect(product).transient:
            raise AssertionError(
                "ProductCreateBatch.encolar espera un Product TRANSIENT: no hagas "
                "session.add() antes de encolarlo (ver _savepoint.py)."
            )
        self._pendientes.append((product, al_resolver, al_ser_ambiguo))
        self._ids_encolados.add(product.id)

    @property
    def lleno(self) -> bool:
        """¿Conviene vaciar ya? El caller decide CUÁNDO, porque el flush también
        dispara su post-trabajo y sólo él sabe si está en un punto seguro."""
        return len(self._pendientes) >= self._chunk_size

    async def flush(self) -> dict[uuid.UUID, Product | None]:
        """Persiste lo encolado y corre el post-trabajo de cada alta.

        Returns:
            ``{id_encolado: producto_final}`` SOLO para las altas cuyo id encolado
            dejó de valer: el producto existente si la carrera se resolvió reusando,
            o ``None`` si el alta se descartó por ambigüedad. Vacío en el camino
            feliz, que es el único que ocurre sin un import concurrente del mismo
            tenant.
        """
        sustituciones: dict[uuid.UUID, Product | None] = {}
        while self._pendientes:
            lote = self._pendientes[: self._chunk_size]
            self._pendientes = self._pendientes[self._chunk_size :]
            for producto, _, _ in lote:
                self._ids_encolados.discard(producto.id)
            sustituciones.update(await self._persistir_lote(lote))
        return sustituciones

    async def _persistir_lote(
        self, lote: list[_Pendiente]
    ) -> dict[uuid.UUID, Product | None]:
        try:
            async with guarded_savepoint(self._session, _classify):
                self._session.add_all([p for p, _, _ in lote])
        except SavepointConflictError:
            # El savepoint revirtió el LOTE ENTERO: `_restore_snapshot` expunga todo
            # lo que estaba pendiente, así que los productos vuelven a ser transient
            # —justo lo que `add_product_or_reuse` exige— y se rehacen de a uno. No se
            # puede saber cuál chocó desde el error del lote, y adivinar descartaría
            # altas legítimas: por eso se reintenta el lote completo, no una parte.
            logger.warning(
                "product_identity.batch_conflict_fallback",
                tenant_id=str(lote[0][0].tenant_id) if lote else None,
                lote=len(lote),
            )
            return await self._reintentar_de_a_uno(lote)
        for product, al_resolver, _ in lote:
            await al_resolver(product, True)
        return {}

    async def _reintentar_de_a_uno(
        self, lote: list[_Pendiente]
    ) -> dict[uuid.UUID, Product | None]:
        sustituciones: dict[uuid.UUID, Product | None] = {}
        for product, al_resolver, al_ser_ambiguo in lote:
            encolado_id = product.id
            try:
                resuelto, creado = await add_product_or_reuse(self._session, product)
            except ProductIdentityConflictError as conflicto:
                # Ambigüedad real (barcode y sku de dueños distintos): no hay "el
                # existente" que reusar. El alta se descarta y el caller decide su
                # destino (el import la manda a "Otros").
                if al_ser_ambiguo is None:
                    raise
                sustituciones[encolado_id] = None
                await al_ser_ambiguo(conflicto)
                continue
            if not creado:
                sustituciones[encolado_id] = resuelto
            await al_resolver(resuelto, creado)
        return sustituciones


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
