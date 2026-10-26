"""Stock service — inventory operations without LLM."""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.shared.event_bus import EventBus
from app.application.agents.shared.heuristic_engine import HeuristicEngine
from app.application.services import apertura_service, maintenance_lock_service
from app.application.services._savepoint import (
    SavepointConflictError,
    guarded_savepoint,
    unique_violation_classifier,
)
from app.application.services.costo_vigente_service import (
    marcar_para_recalcular,
    recalcular_costo_vigente,
)
from app.application.services.inventory_movement_origin import (
    FECHA_DE_USUARIO,
    SOURCE_CATALOG_INITIAL_STOCK,
    SOURCE_MANUAL_ADJUSTMENT,
    SOURCE_RECONCILIATION,
    columnas_de_apertura_confirmada,
    ensure_utc,
    momento_de_negocio_rotulado,
)
from app.domain.contrato_de_costo import ContratoDeCosto
from app.domain.product import effective_threshold
from app.domain.verticals import Vertical
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.inventory import InventoryBalance, InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.transaction import SaleEntry

# Prefijo canónico del source_event_id de un descuento de venta EN VIVO. UNA venta ⇒
# UN movimiento 'sale' vivo (garantizado por el índice único parcial de la migración
# 20260729_0001). Habilita reversa exacta por venta e idempotencia sync+evento.
_SALE_SOURCE_PREFIX = "sale:"

# F5-A: a diferencia de los sentinelas, este índice es TOTAL (sin ``WHERE``), así que
# también existe en SQLite — donde el error viene por columnas y no por nombre. Se
# declaran las dos formas para que el clasificador discrimine en ambos motores.
# Público: lo comparte ``ingestion_import_service._apply_purchase_to_stock``, el otro
# sitio que crea balances. Un segundo clasificador podría divergir del nombre real.
BALANCE_CONFLICT = unique_violation_classifier(
    "balance",
    constraint="uq_inventory_balances_tenant_product",
    columns=("inventory_balances.tenant_id", "inventory_balances.product_id"),
)

# Idempotencia del descuento de venta EN VIVO: el índice parcial de la migración
# 20260729_0001 sobre (tenant_id, source_event_id) para los movimientos 'sale' vivos.
# Discriminar importa más desde F5-B: acá abajo había un ``except IntegrityError`` a
# secas que, además de esta carrera, se tragaba la violación del unique NUEVO de
# ``inventory_balances`` (y cualquier FK/NOT NULL) devolviendo ``None`` — o sea una
# venta persistida que NUNCA descuenta stock, sin log ni error, y que recién aparece
# semanas después como divergencia sin causa en el chequeo de integridad.
LIVE_SALE_EVENT_CONFLICT = unique_violation_classifier(
    "live_sale_event",
    constraint="uq_inventory_movements_live_sale_event",
    columns=("inventory_movements.tenant_id", "inventory_movements.source_event_id"),
)

# Mensaje canónico de rechazo por stock insuficiente en una venta EN VIVO. Se muestra
# TAL CUAL al usuario (chat vía user_message; endpoints REST vía handler global → 400).
# La parte ACCIONABLE del mensaje: qué tiene que hacer el usuario. Va en todas las
# variantes — decirle "no hay stock" sin decirle cómo arreglarlo no le sirve.
INSUFFICIENT_STOCK_ACTION = "Cargá primero la compra o stock inicial de mercadería."
INSUFFICIENT_STOCK_MESSAGE = (
    f"No hay stock suficiente para registrar esta venta. {INSUFFICIENT_STOCK_ACTION}"
)


PRODUCT_NOT_FOUND_MESSAGE = "El producto de la venta no existe o no pertenece al negocio."


class InsufficientStockError(Exception):
    """Venta EN VIVO con producto pero sin stock suficiente. NO se permite stock negativo.

    Lleva ``user_message`` para que ``execute_local_action`` (chat) la muestre tal cual y
    el handler global de ``main.py`` la mapee a 400 con el mismo mensaje. La venta NO se
    persiste (el rollback de la transacción del request deshace cualquier SaleEntry).
    """

    #: Código estable del contrato HTTP. El flush de la cola offline necesita
    #: distinguir ESTE 400 de cualquier otro (payload inválido, proveedor borrado)
    #: para decidir si reintenta capturando la discrepancia; leer el texto del
    #: mensaje para eso sería frágil y se rompería con cualquier cambio de copy.
    CODE = "INSUFFICIENT_STOCK"

    def __init__(
        self,
        product_id: uuid.UUID,
        available: int,
        requested: int,
        product_name: str | None = None,
    ) -> None:
        self.product_id = product_id
        self.available = available
        self.requested = requested
        self.product_name = product_name
        # Con el nombre a mano el mensaje dice de QUÉ producto se trata; sin él
        # (caminos que no cargaron el producto) queda el genérico. La frase
        # accionable va en los dos casos.
        self.user_message = (
            f"Stock insuficiente para «{product_name}»: disponible {available}, "
            f"pedís {requested}. {INSUFFICIENT_STOCK_ACTION}"
            if product_name
            else INSUFFICIENT_STOCK_MESSAGE
        )
        super().__init__(
            f"Stock insuficiente para {product_id}: disponible {available}, pedís {requested}."
        )


class SaleProductNotFoundError(Exception):
    """El ``product_id`` de una venta no existe o es de otro tenant (id viejo / IDOR).

    Mapeada a 400 por el handler global (mismo criterio que manual-batch), NO a 500. Solo
    la usan las validaciones de venta EN VIVO; ``decrement_stock``/``increment_stock``
    siguen levantando ``ValueError`` para sus callers internos (producto ya resuelto).
    """

    def __init__(self, product_id: uuid.UUID) -> None:
        self.product_id = product_id
        self.user_message = PRODUCT_NOT_FOUND_MESSAGE
        super().__init__(f"Product {product_id} not found for tenant")


def sale_source_event_id(sale_id: uuid.UUID) -> str:
    return f"{_SALE_SOURCE_PREFIX}{sale_id}"


async def bloquear_productos(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    product_ids: Iterable[uuid.UUID],
    *,
    lock_held: bool = False,
) -> dict[uuid.UUID, Product]:
    """Bloquea las filas de los productos (``FOR UPDATE``) y devuelve su versión
    VIGENTE. Es la única forma de leer un stock que después se va a escribir.

    T2 (revisión profunda 2026-10-24): ``stock_units`` y ``current_qty`` se
    escriben como valor absoluto calculado en memoria. Leídos sin candado, o con
    un ``FOR UPDATE`` que devuelve la instancia vieja del identity map, una venta
    de la otra caja que commitea en el medio se pierde: stock 10, la caja vende
    3, se anula un ticket de 2 → queda 12 en vez de 9.

    - Advisory shared ANTES de la fila (orden fijo contra el exclusive del dedup).
    - ``flush`` antes: la sesión no hace autoflush y ``populate_existing``
      descartaría un cambio pendiente de estas mismas instancias.
    - ``populate_existing``: si el producto ya estaba cargado, el candado espera
      a la otra transacción y el objeto se refresca con lo que ella dejó.
    - Ordenado por id, igual que la venta del POS: dos operaciones de varios
      productos toman los candados en el mismo orden y no se bloquean en cruz.

    Productos de otro tenant o inexistentes no vuelven (el caller decide).
    ``lock_held``: como en ``_get_or_create_balance`` — el caller ya tomó el
    advisory shared en esta transacción.
    """
    ids = sorted(set(product_ids), key=str)
    if not ids:
        return {}
    if not lock_held:
        await maintenance_lock_service.acquire_write_lock_shared(db, tenant_id)
    await db.flush()
    bloqueados: dict[uuid.UUID, Product] = {}
    # En trozos (el driver corta en 32.767 binds) y en orden: cada trozo es un
    # tramo ascendente de ids, así que el orden total se mantiene.
    for inicio in range(0, len(ids), BULK_DECREMENT_CHUNK_SIZE):
        filas = (
            await db.execute(
                select(Product)
                .where(
                    Product.tenant_id == tenant_id,
                    Product.id.in_(ids[inicio : inicio + BULK_DECREMENT_CHUNK_SIZE]),
                )
                .order_by(Product.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars()
        bloqueados.update({p.id: p for p in filas})
    return bloqueados


async def bloquear_balances(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    product_ids: Iterable[uuid.UUID],
) -> dict[uuid.UUID, InventoryBalance]:
    """Los balances existentes de esos productos, bloqueados y refrescados.

    Para los caminos que precargan un índice de balances (el remito): se pide
    DESPUÉS de ``bloquear_productos`` —producto y después balance, el orden de
    ``_get_or_create_balance`` en todos los caminos—. Un producto sin balance no
    vuelve: lo crea el camino de siempre.
    """
    ids = sorted(set(product_ids), key=str)
    if not ids:
        return {}
    await db.flush()
    bloqueados: dict[uuid.UUID, InventoryBalance] = {}
    for inicio in range(0, len(ids), BULK_DECREMENT_CHUNK_SIZE):  # ver bloquear_productos
        filas = (
            await db.execute(
                select(InventoryBalance)
                .where(
                    InventoryBalance.tenant_id == tenant_id,
                    InventoryBalance.product_id.in_(
                        ids[inicio : inicio + BULK_DECREMENT_CHUNK_SIZE]
                    ),
                )
                .order_by(InventoryBalance.product_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars()
        bloqueados.update({b.product_id: b for b in filas})
    return bloqueados


async def bloquear_producto(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    product_id: uuid.UUID,
    *,
    lock_held: bool = False,
) -> Product | None:
    """``bloquear_productos`` de uno solo."""
    bloqueados = await bloquear_productos(db, tenant_id, [product_id], lock_held=lock_held)
    return bloqueados.get(product_id)


@dataclass
class CatalogoBloqueado:
    """Los productos y balances del negocio, bloqueados y refrescados.

    Las referencias son FUERTES a propósito: el identity map es débil, y un
    producto que nadie retiene se libera y vuelve a leerse más tarde. Bajo el
    candado esa relectura igual daría el valor vigente, pero el import la pagaría
    con una consulta por fila.
    """

    productos: dict[uuid.UUID, Product]
    balances: dict[uuid.UUID, InventoryBalance]


async def bloquear_catalogo(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    lock_held: bool = False,
) -> CatalogoBloqueado:
    """Bloquea TODOS los productos del negocio (orden de id) y sus balances.

    T2-b: lo usan los caminos que escriben stock fila por fila sin saber de
    antemano qué productos van a tocar —el import, la relectura—. Bloquear cada
    producto la primera vez que se escribe dejaba el orden en manos del archivo:
    un import que toca P5 y después P3 contra un ticket del POS que toma P3 y
    P5 se bloqueaban en cruz. Tomar el catálogo entero de una, ordenado, es un
    superconjunto de lo que el archivo toca y sigue el orden de todos los demás
    caminos (``bloquear_productos``). Lo que cuesta: mientras el import escribe,
    una venta del POS sobre cualquier producto del negocio espera a que termine.

    Va ANTES de leer el stock o los balances: con ``populate_existing``, lo que
    ya estuviera cargado en la sesión queda con el valor vigente.
    """
    if not lock_held:
        await maintenance_lock_service.acquire_write_lock_shared(db, tenant_id)
    await db.flush()
    productos = {
        p.id: p
        for p in (
            await db.execute(
                select(Product)
                .where(Product.tenant_id == tenant_id)
                .order_by(Product.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars()
    }
    balances = {
        b.product_id: b
        for b in (
            await db.execute(
                select(InventoryBalance)
                .where(InventoryBalance.tenant_id == tenant_id)
                .order_by(InventoryBalance.product_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalars()
    }
    return CatalogoBloqueado(productos, balances)


async def check_stock_available(
    product_id: uuid.UUID,
    tenant_id: uuid.UUID,
    qty: int,
    db: AsyncSession,
) -> Product:
    """Valida stock suficiente para una venta EN VIVO, con lock de fila (anti-sobreventa).

    Carga el producto con ``SELECT ... FOR UPDATE`` (serializa contra ventas concurrentes
    del mismo producto) y levanta ``InsufficientStockError`` si ``stock_units < qty``.
    Devuelve el ``Product`` bloqueado (queda en el identity map: ``decrement_stock`` lo
    reusa sin reconsultar). Reusable por todos los caminos de venta en vivo.
    """
    # F3-T3 review: el advisory shared SIEMPRE antes que cualquier FOR UPDATE — si un
    # writer tomara la fila primero y pidiera el advisory después, podría deadlockear
    # contra el dedup (que toma el exclusive y luego intentaría lockear la misma fila).
    # `bloquear_producto` lo toma; T2: y refresca la instancia del identity map.
    product = await bloquear_producto(db, tenant_id, product_id)
    if product is None:
        raise SaleProductNotFoundError(product_id)
    if product.stock_units < qty:
        raise InsufficientStockError(product_id, product.stock_units, qty, product.name)
    return product


async def _get_or_create_balance(
    product: Product,
    tenant_id: uuid.UUID,
    db: AsyncSession,
    *,
    lock_held: bool = False,
) -> InventoryBalance:
    # F3-T3: chokepoint COMÚN de toda mutación de balance/stock (decrement_stock,
    # increment_stock, register_stock_loss, void_movement, unvoid_movement — y
    # transitivamente decrement_for_sale/revert_sale_stock, que pasan por acá).
    # Shared lock ANTES de mutar: barrera de exclusión mutua real contra el dedup
    # (que toma el exclusive). No-op en SQLite.
    #
    # ``lock_held``: el caller declara que YA tomó el shared lock de este tenant en
    # ESTA transacción. ``pg_advisory_xact_lock_shared`` se libera recién en el
    # commit/rollback, así que volver a pedirlo es un no-op semántico que igual
    # cuesta un round-trip entero — irrelevante en una request suelta, decisivo en
    # un bucle: la relectura de Asteria pagaba 810 de estos (2 por movimiento × 405),
    # el 15% de todos los statements del apply. Default ``False`` = comportamiento
    # de siempre; solo lo pasa quien pueda PROBAR que el lock está tomado aguas
    # arriba en la misma transacción, nunca "por las dudas".
    if not lock_held:
        await maintenance_lock_service.acquire_write_lock_shared(db, tenant_id)
    # T2: el balance se escribe en absoluto igual que el producto, así que se
    # bloquea y se refresca igual (después de la fila del producto: mismo orden
    # en todos los caminos).
    await db.flush()
    result = await db.execute(
        select(InventoryBalance)
        .where(
            InventoryBalance.product_id == product.id,
            InventoryBalance.tenant_id == tenant_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    balance = result.scalar_one_or_none()
    if balance is not None:
        return balance

    # F5-A: el SELECT de arriba corre bajo el lock SHARED, que no serializa writers
    # entre sí — dos requests concurrentes del mismo producto pasan las dos y crean
    # dos filas de balance. El savepoint deja que el índice único
    # (``uq_inventory_balances_tenant_product``, F5-B) resuelva la carrera sin
    # abortar la transacción de la perdedora.
    #
    # ``current_qty`` sale de ``product.stock_units``, NO de sumar el ledger: el
    # stock no es Σ(inventory_movements) y recomputarlo desde ahí lo corrompería.
    try:
        async with guarded_savepoint(db, BALANCE_CONFLICT):
            balance = InventoryBalance(
                tenant_id=tenant_id,
                product_id=product.id,
                current_qty=product.stock_units,
                reserved_qty=0,
            )
            db.add(balance)
    except SavepointConflictError as conflict:
        existing = (
            await db.execute(
                select(InventoryBalance).where(
                    InventoryBalance.product_id == product.id,
                    InventoryBalance.tenant_id == tenant_id,
                )
            )
        ).scalar_one_or_none()
        if existing is None:  # pragma: no cover — el índice garantiza que exista
            raise conflict.original from conflict
        return existing
    return balance


async def decrement_stock(
    product_id: uuid.UUID,
    tenant_id: uuid.UUID,
    qty: int,
    source_event_id: str | None,
    db: AsyncSession,
    *,
    occurred_at: datetime | None = None,
    source_upload_id: uuid.UUID | None = None,
    source_type: str | None = None,
) -> InventoryMovement:
    # F3-T3 review: acquire al tope del entry point, antes de cualquier lectura/lock.
    await maintenance_lock_service.acquire_write_lock_shared(db, tenant_id)
    # T2: con candado y refrescado (una venta ya lo bloqueó en check_stock_available
    # y el re-pedido no espera; el ajuste «saqué N» del chat no lo bloqueaba).
    product = await bloquear_producto(db, tenant_id, product_id, lock_held=True)
    if product is None:
        raise ValueError(f"Product {product_id} not found for tenant {tenant_id}")

    balance = await _get_or_create_balance(product, tenant_id, db)
    balance.current_qty -= qty
    # Mantener Product.stock_units (lo que lee la UI) en sync también al vender:
    # sin esto, las compras subían el stock visible pero las ventas nunca lo
    # bajaban y stock_units inflaba monotónicamente.
    # Piso a 0 (NO se permite stock negativo): las ventas EN VIVO ya validan stock
    # suficiente ANTES de llamar acá (check_stock_available levanta InsufficientStockError),
    # así que para una venta el clamp es inerte (stock_units - qty >= 0) y la reversa del
    # void queda EXACTA. El clamp es el piso defensivo para el único otro caller
    # (UPDATE_STOCK, ajuste manual "saqué N"), que no valida: nunca deja stock negativo.
    product.stock_units = max(0, product.stock_units - qty)

    movement = InventoryMovement(
        tenant_id=tenant_id,
        product_id=product_id,
        movement_type="sale",
        qty=-qty,
        source_event_id=source_event_id,
        # F-H3.d.4: procedencia, cuando el descuento viene de aplicar la historia de
        # un archivo. Sin `source_upload_id` el movimiento quedaría fuera de la
        # reversa por borrado (que voidea por archivo) y borrar el import dejaría el
        # descuento aplicado para siempre. NULL en el descuento de una venta en vivo,
        # que no viene de ningún archivo.
        source_upload_id=source_upload_id,
        source_type=source_type,
        # Fecha de NEGOCIO del movimiento (venta): si el caller la conoce (fecha de
        # la venta), se usa esa; si no, el momento del registro.
        # Sin fecha del caller: AHORA en hora AR rotulada UTC, como todo el ledger
        # (M·2a; antes ``now(UTC)`` real corría al día siguiente de 21 a 24 AR).
        occurred_at=ensure_utc(occurred_at) or momento_de_negocio_rotulado(None),
    )
    db.add(movement)
    await db.flush()

    EventBus.emit(
        "STOCK_DECREASED",
        {
            "tenant_id": str(tenant_id),
            "product_id": str(product_id),
            "qty": qty,
            "source_event_id": source_event_id,
        },
    )

    if balance.current_qty <= effective_threshold(product.low_stock_threshold_units):
        EventBus.emit(
            "STOCK_ALERT_CREATED",
            {
                "tenant_id": str(tenant_id),
                "product_id": str(product_id),
                "alert_type": "stockout_risk",
                "current_qty": balance.current_qty,
            },
        )

    return movement


#: Descuentos por lote al escribir. Mismo criterio que ``CLAVES_CHUNK_SIZE`` del
#: replay: un archivo no tiene tope de filas y Postgres corta en 65.535 binds por
#: statement (un movimiento tiene ~14 columnas). Acota además el costo del camino
#: de excepción: cuando un lote choca, lo que se rehace de a una es un lote, no el
#: archivo entero.
BULK_DECREMENT_CHUNK_SIZE = 500


@dataclass(frozen=True)
class BulkDecrementItem:
    """Un descuento pedido dentro de un lote.

    Lleva el ``Product`` ya resuelto y no su id: el caller que arma el lote lo
    tiene cargado, y volver a buscarlo acá reintroduciría por producto la consulta
    que este camino existe para evitar.
    """

    product: Product
    qty: int
    source_event_id: str
    occurred_at: datetime | None = None


@dataclass
class BulkDecrementOutcome:
    """Cuántos descuentos escribió el lote y cuántos ya estaban."""

    applied: int = 0
    #: El movimiento ya existía: la venta se descontó por otro camino (en vivo, o
    #: una corrida anterior). Es el no-op idempotente, no un error.
    already_applied: int = 0


async def _balances_por_producto(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    productos: list[Product],
) -> dict[uuid.UUID, InventoryBalance]:
    """Los balances de todos los productos del lote, en una consulta por chunk.

    Es el reemplazo del ``SELECT`` por venta de ``_get_or_create_balance``: sobre un
    archivo grande esa consulta se repetía una vez por fila aunque cuarenta ventas
    hablaran del mismo producto. Los que no existen se crean por el camino de
    siempre —uno por producto faltante, no uno por venta—, que es el que sabe
    resolver la carrera contra el índice único.
    """
    # T2-b: bloqueados y refrescados, como los productos (que el caller ya
    # bloqueó: producto y después balance, el orden de todos los caminos).
    encontrados = await bloquear_balances(db, tenant_id, {p.id for p in productos})
    for producto in productos:
        if producto.id not in encontrados:
            encontrados[producto.id] = await _get_or_create_balance(
                producto, tenant_id, db, lock_held=True
            )
    return encontrados


def _movimiento_de_venta(
    tenant_id: uuid.UUID,
    item: BulkDecrementItem,
    source_upload_id: uuid.UUID | None,
    source_type: str | None,
) -> InventoryMovement:
    """El mismo movimiento que arma ``decrement_stock``, para un item del lote.

    Se construye acá y no se reusa el objeto entre intentos a propósito: cuando un
    lote choca, el rollback del SAVEPOINT deja transient todo lo que se había
    agregado (``_restore_snapshot``), así que el camino de a una necesita objetos
    nuevos.
    """
    return InventoryMovement(
        tenant_id=tenant_id,
        product_id=item.product.id,
        movement_type="sale",
        qty=-item.qty,
        source_event_id=item.source_event_id,
        source_upload_id=source_upload_id,
        source_type=source_type,
        occurred_at=ensure_utc(item.occurred_at) or momento_de_negocio_rotulado(None),
    )


async def decrement_stock_bulk(
    tenant_id: uuid.UUID,
    items: list[BulkDecrementItem],
    db: AsyncSession,
    *,
    source_upload_id: uuid.UUID | None = None,
    source_type: str | None = None,
) -> BulkDecrementOutcome:
    """Aplica muchos descuentos de venta con un puñado de sentencias, no cuatro por venta.

    **Por qué existe.** ``decrement_stock`` cuesta ~4 sentencias y un envío al broker
    por llamada, lo cual es correcto para UNA venta y ruinoso para las 1.187 de un
    archivo: son ~4.700 sentencias adentro del request del confirm (medido). Acá el
    costo pasa a depender de la cantidad de **productos**, no de la de ventas —
    cuarenta ventas del mismo producto escriben su saldo una vez, no cuarenta.

    **Vive en el núcleo, no en el llamador.** Lo que aplica el confirm y lo que
    aplica el reintento del panel tienen que ser la misma operación; un lote armado
    del lado del caller volvería a separarlas.

    **Escribir primero el movimiento y recién después el saldo** no es un detalle de
    orden: el movimiento es lo único que puede chocar (índice único de
    ``source_event_id``), así que resolverlo antes deja el saldo calculado sobre lo
    que *realmente* entró. Al revés habría que adivinar cuánto revertir.

    **La carrera se paga por lote y no por archivo.** Si entre el pre-chequeo del
    caller y este INSERT una venta en vivo descontó el mismo registro, el lote entero
    se rechaza y se rehace de a una por el camino de siempre: cada fila conflictiva
    cuenta como ``already_applied`` y las demás entran igual. Se rehace el lote
    completo —no se lo parte— porque la colisión es rara y el camino de a una es el
    que ya estaba probado.
    """
    outcome = BulkDecrementOutcome()
    if not items:
        return outcome

    # Uno por corrida y no dos por venta: el advisory es transaccional
    # (``pg_advisory_xact_lock_shared``), así que tomarlo de nuevo en cada iteración
    # no agregaba exclusión, sólo sentencias.
    await maintenance_lock_service.acquire_write_lock_shared(db, tenant_id)

    # T2-b: el saldo se escribe en absoluto, así que se valida y se escribe sobre
    # los productos bloqueados y refrescados, nunca sobre la lectura del caller.
    # Antes el lote recortaba a 0 en silencio: si otra caja vendía en el medio,
    # el stock quedaba en 0 con más ventas descontadas que unidades.
    bloqueados = await bloquear_productos(
        db, tenant_id, {i.product.id for i in items}, lock_held=True
    )
    pedido: dict[uuid.UUID, int] = {}
    for item in items:
        pedido[item.product.id] = pedido.get(item.product.id, 0) + item.qty
    for product_id, qty in pedido.items():
        producto = bloqueados.get(product_id)
        if producto is None:
            raise ValueError(f"Product {product_id} not found for tenant {tenant_id}")
        # Un faltante histórico NO llega hasta acá: el caller lo deja pendiente
        # (``rows_without_stock_backing``) sobre este mismo saldo bloqueado. Si
        # igual falta, el lote no escribe nada y el caller revierte todo.
        if producto.stock_units < qty:
            raise InsufficientStockError(product_id, producto.stock_units, qty, producto.name)

    balances = await _balances_por_producto(db, tenant_id, [i.product for i in items])

    aplicados: list[BulkDecrementItem] = []
    for inicio in range(0, len(items), BULK_DECREMENT_CHUNK_SIZE):
        lote = items[inicio : inicio + BULK_DECREMENT_CHUNK_SIZE]
        # Contrato de ``_savepoint``: drenar lo pendiente FUERA del try. Acá no es
        # higiene sino requisito — el rollback del SAVEPOINT expunga TODO lo que la
        # sesión tenga sin flushear, incluidas las filas que el import acaba de
        # agregar y que no tienen nada que ver con este lote.
        await db.flush()
        try:
            async with guarded_savepoint(db, LIVE_SALE_EVENT_CONFLICT):
                for item in lote:
                    db.add(_movimiento_de_venta(tenant_id, item, source_upload_id, source_type))
                await db.flush()
            aplicados.extend(lote)
            outcome.applied += len(lote)
        except SavepointConflictError:
            for item in lote:
                await db.flush()
                try:
                    async with guarded_savepoint(db, LIVE_SALE_EVENT_CONFLICT):
                        db.add(
                            _movimiento_de_venta(tenant_id, item, source_upload_id, source_type)
                        )
                        await db.flush()
                    aplicados.append(item)
                    outcome.applied += 1
                except SavepointConflictError:
                    outcome.already_applied += 1

    if not aplicados:
        return outcome

    # El saldo se acumula en memoria y se escribe UNA vez por producto. Resta pura:
    # lo validado arriba cubre todos los items y ``aplicados`` es un subconjunto
    # (sin los que ya estaban descontados), así que ningún saldo baja de cero.
    for item in aplicados:
        producto = bloqueados[item.product.id]
        producto.stock_units -= item.qty
        balances[producto.id].current_qty -= item.qty

    # Y se escribe antes de volver. NO es redundante con el commit del caller:
    # ``decrement_stock`` dejaba cada descuento flusheado, así que quien leyera el
    # stock más adelante en la MISMA transacción veía el saldo ya descontado. Sin
    # este flush el cambio queda sólo en memoria y cualquier lectura previa al commit
    # —una query sin autoflush, un ``refresh()``— devuelve el saldo viejo, o peor, lo
    # pisa. Sigue siendo un flush por corrida y P ``UPDATE``, no uno por venta.
    await db.flush()

    # Un aviso por corrida, no uno por venta: ``events.stock_decreased`` sólo encola
    # el recálculo de score del tenant, así que emitirlo 1.187 veces encolaba 1.187
    # veces el mismo recálculo del mismo negocio.
    EventBus.emit(
        "STOCK_DECREASED",
        {
            "tenant_id": str(tenant_id),
            "product_id": None,
            "qty": sum(i.qty for i in aplicados),
            "source_event_id": None,
        },
    )
    # Y una alerta por producto que queda bajo el umbral, no una por venta posterior
    # al cruce: un producto con cuarenta ventas que cruzaba en la doceava emitía
    # veintinueve alertas idénticas.
    # Por dict y no con un ``next(...)`` adentro del recorrido: buscar el producto de
    # cada alerta escaneando la lista de items sería cuadrático, que es exactamente lo
    # que esta función existe para no hacer.
    tocados = {i.product.id: i.product for i in aplicados}
    for product_id, producto in tocados.items():
        balance = balances[product_id]
        if balance.current_qty <= effective_threshold(producto.low_stock_threshold_units):
            EventBus.emit(
                "STOCK_ALERT_CREATED",
                {
                    "tenant_id": str(tenant_id),
                    "product_id": str(product_id),
                    "alert_type": "stockout_risk",
                    "current_qty": balance.current_qty,
                },
            )

    return outcome


async def increment_stock(
    product_id: uuid.UUID,
    tenant_id: uuid.UUID,
    qty: int,
    unit_cost: Decimal | None,
    source_event_id: str | None,
    db: AsyncSession,
    *,
    supplier_id: uuid.UUID | None = None,
    occurred_at: datetime | None = None,
    defer_event: bool = False,
    contrato: ContratoDeCosto | None = None,
    source_upload_id: uuid.UUID | None = None,
    source_row_ref: str | None = None,
) -> InventoryMovement:
    """Suma stock por una compra y deja su movimiento ``purchase``.

    ``source_upload_id`` / ``source_row_ref`` (B·5b): la procedencia de una compra
    que se resolvió desde una fila de un archivo («Otros»). Sin ellas, borrar el
    archivo (F11) no alcanzaba al movimiento y el stock sobrevivía sin rastro.

    ``contrato`` (M·1b): lo que la ruta sabe del costo de esta compra. ``None``
    deja el contrato vacío (NULL = desconocido), que es lo que corresponde a un
    alta de stock que no es una compra declarada (``UPDATE_STOCK`` del chat).
    ``occurred_at`` se guarda en hora argentina rotulada UTC, igual que la ingesta
    (``momento_de_negocio_rotulado``).

    M·1c: el costo del producto ya no lo pisa esta compra por llegar última. Se
    recalcula con ``recalcular_costo_vigente``: gana la fuente más reciente POR
    FECHA (compra, edición manual o catálogo), así que un comprobante atrasado no
    le gana a una compra más nueva ni a una edición posterior.
    """
    # F3-T3 review: advisory shared ANTES del FOR UPDATE de fila (orden fijo, evita
    # deadlock AB-BA contra el exclusive del dedup).
    await maintenance_lock_service.acquire_write_lock_shared(db, tenant_id)
    # Serializa compras concurrentes del mismo producto para no perder incrementos
    # en ``stock_units``/``InventoryBalance.current_qty`` (T2: refrescado).
    product = await bloquear_producto(db, tenant_id, product_id, lock_held=True)
    if product is None:
        raise ValueError(f"Product {product_id} not found for tenant {tenant_id}")

    balance = await _get_or_create_balance(product, tenant_id, db)
    balance.current_qty += qty
    # Product.stock_units es la representación canónica que lee la UI: sin esto,
    # una compra confirmada subía el balance pero el stock visible no cambiaba.
    product.stock_units += qty

    movement = InventoryMovement(
        tenant_id=tenant_id,
        product_id=product_id,
        movement_type="purchase",
        qty=qty,
        unit_cost=unit_cost,
        source_event_id=source_event_id,
        supplier_id=supplier_id,
        # Fecha de NEGOCIO de la compra: si el caller la conoce, se usa esa; si no,
        # el momento del registro. En hora AR rotulada UTC, como la ingesta (M·1b).
        occurred_at=momento_de_negocio_rotulado(occurred_at),
        source_upload_id=source_upload_id,
        source_row_ref=source_row_ref,
        **(contrato.columnas() if contrato is not None else {}),
    )
    db.add(movement)
    await db.flush()
    await recalcular_costo_vigente(db, tenant_id, [product_id], movimientos_nuevos={movement.id})

    event_payload = {
        "tenant_id": str(tenant_id),
        "product_id": str(product_id),
        "qty": qty,
    }
    if defer_event:
        EventBus.emit_after_commit(db, "STOCK_INCREASED", event_payload)
    else:
        EventBus.emit("STOCK_INCREASED", event_payload)

    return movement


async def register_reconciliation_adjustment(
    product_id: uuid.UUID,
    tenant_id: uuid.UUID,
    qty: int,
    reason: str,
    source_event_id: str,
    db: AsyncSession,
    *,
    occurred_at: datetime | None = None,
) -> InventoryMovement | None:
    """Entrada de ajuste AUDITADA: "estas unidades estaban y el sistema no las tenía".

    Es lo que cierra una salida pendiente de caja (B11) cuando el dueño confirma
    que la mercadería existía. Se registra como ``adjustment`` con
    ``source_type=reconciliation`` —no como ``purchase``— porque nadie compró
    nada: el CHECK ``ck_inventory_movements_adjustment_source_type`` exige esa
    procedencia y los dos chequeos de inventario la clasifican como ajuste
    auditado, no como compra.

    ``occurred_at`` es la fecha de NEGOCIO del ajuste, y el caller manda la de la
    venta: el dueño está afirmando que esas unidades estaban **cuando se
    vendió**, y es lo que hace que el replay temporal cierre.

    Idempotente por ``source_event_id``: resolver dos veces no duplica el
    ajuste. Devuelve ``None`` si ya estaba aplicado.
    """
    if qty <= 0:
        raise ValueError("Un ajuste de entrada tiene que ser positivo.")
    await maintenance_lock_service.acquire_write_lock_shared(db, tenant_id)
    ya = (
        await db.execute(
            select(InventoryMovement).where(
                InventoryMovement.tenant_id == tenant_id,
                InventoryMovement.source_event_id == source_event_id,
                InventoryMovement.voided_at.is_(None),
            )
        )
    ).scalars().first()
    if ya is not None:
        return None

    product = await bloquear_producto(db, tenant_id, product_id, lock_held=True)
    if product is None:
        raise ValueError(f"Product {product_id} not found for tenant {tenant_id}")

    balance = await _get_or_create_balance(product, tenant_id, db)
    balance.current_qty += qty
    product.stock_units += qty

    movement = InventoryMovement(
        tenant_id=tenant_id,
        product_id=product_id,
        movement_type="adjustment",
        qty=qty,
        reason=reason,
        source_event_id=source_event_id,
        source_type=SOURCE_RECONCILIATION,
        # Sin fecha del caller: AHORA en hora AR rotulada UTC, como todo el ledger
        # (M·2a; antes ``now(UTC)`` real corría al día siguiente de 21 a 24 AR).
        occurred_at=ensure_utc(occurred_at) or momento_de_negocio_rotulado(None),
    )
    db.add(movement)
    await db.flush()
    return movement


class CostoRequeridoError(Exception):
    """Se carga stock a mano sin precio de compra (M·P2c)."""


COSTO_REQUERIDO_MESSAGE = (
    "Para cargar stock a mano hace falta el precio de compra (el costo por unidad). "
    "Si incluye el envío o no, es opcional."
)


def exigir_costo_con_stock(entra_stock: bool, costo: Decimal | None) -> None:
    """Decisión del dueño (2026-10-14): lo que se carga a mano tiene costo.

    Sólo cuando ENTRA stock (alta con stock o una suba): bajar el stock no lo
    exige. Un costo 0 no es un costo (igual que ``costo_de_referencia``).
    """
    if entra_stock and (costo is None or costo <= 0):
        raise CostoRequeridoError(COSTO_REQUERIDO_MESSAGE)


class ProductoNoEncontradoError(ValueError):
    """El producto no existe en el tenant (ValueError, por los llamadores viejos)."""


class AperturaExistenteError(Exception):
    """El producto ya tiene stock inicial vivo (B·5c): no se suma otro en silencio."""

    def __init__(self, apertura: InventoryMovement) -> None:
        super().__init__("El producto ya tiene stock inicial cargado.")
        self.apertura = apertura


class ProductoInactivoError(Exception):
    """Stock inicial para un producto dado de baja (B·5c)."""


class StockCambioError(Exception):
    """El stock que el usuario veía al editar ya no es el actual (M·P2c)."""

    def __init__(self, esperado: int, actual: int) -> None:
        super().__init__(f"stock esperado {esperado}, actual {actual}")
        self.esperado = esperado
        self.actual = actual


class StockNegativoError(Exception):
    """Un ajuste manual dejaría el stock por debajo de 0 (prohibido)."""


async def _producto_para_stock_manual(
    product_id: uuid.UUID, tenant_id: uuid.UUID, db: AsyncSession
) -> Product:
    # Mismo orden de locks que ``increment_stock``: advisory shared y después
    # la fila del producto. ``populate_existing``: la ruta ya cargó el producto
    # sin lock (PATCH, chat); sin esto el ``FOR UPDATE`` devuelve la instancia
    # del identity map con el stock VIEJO, el 409 de ``esperado`` no salta en la
    # carrera y ``stock_units += delta`` pisa una venta commiteada en el medio
    # (la misma trampa que documenta ``pos_discrepancy_service``).
    product = await bloquear_producto(db, tenant_id, product_id)
    if product is None:
        raise ProductoNoEncontradoError(f"Product {product_id} not found for tenant {tenant_id}")
    return product


async def registrar_apertura_manual(
    product_id: uuid.UUID,
    tenant_id: uuid.UUID,
    qty: int,
    contrato: ContratoDeCosto,
    db: AsyncSession,
    *,
    fecha: datetime,
    confirmado_por: uuid.UUID | None,
    source_upload_id: uuid.UUID | None = None,
    exigir_sin_apertura: bool = False,
) -> InventoryMovement:
    """Stock inicial cargado a mano en el alta de un producto (M·P2c).

    Deja el MISMO movimiento que un catálogo importado en «ya la tenía»:
    ``adjustment`` + ``catalog_initial_stock`` con su costo. Así el producto no
    queda distinto de uno importado: tiene ledger, costo de referencia
    («stock inicial») y costo promedio medido. ``qty`` en unidades base; el
    costo, por unidad de venta, como en el resto del ledger.

    ``source_upload_id``: el archivo del que vino el registro (alta desde
    «Otros»). Así borrar ese archivo anula también esta apertura, como pasa con
    el stock de un catálogo importado.

    ``fecha`` (M·2d): desde cuándo el negocio tiene ese stock, la que tipeó el
    usuario. La apertura nace CONFIRMADA en esa fecha de negocio (no en la de la
    carga): una venta anterior a la carga y posterior a esa fecha tiene fuente.

    ``exigir_sin_apertura`` (B·5c, producto que ya existía): con el producto
    bloqueado, si ya tiene una apertura viva → ``AperturaExistenteError``, y si
    está dado de baja → ``ProductoInactivoError``. Dos cargas simultáneas se
    serializan en el ``FOR UPDATE``: la segunda ve la apertura de la primera.
    """
    if qty <= 0:
        raise ValueError("El stock inicial tiene que ser positivo.")
    product = await _producto_para_stock_manual(product_id, tenant_id, db)
    if exigir_sin_apertura:
        if not product.is_active:
            raise ProductoInactivoError(str(product_id))
        existente = await apertura_service.apertura_viva_de(db, tenant_id, product_id)
        if existente is not None:
            raise AperturaExistenteError(existente)
    balance = await _get_or_create_balance(product, tenant_id, db)
    balance.current_qty += qty
    product.stock_units += qty
    movement = InventoryMovement(
        tenant_id=tenant_id,
        product_id=product_id,
        movement_type="adjustment",
        qty=qty,
        unit_cost=contrato.unit_cost_final,
        reason="Stock inicial cargado a mano",
        source_event_id=f"manual:alta:{product_id}",
        source_type=SOURCE_CATALOG_INITIAL_STOCK,
        source_upload_id=source_upload_id,
        occurred_at=momento_de_negocio_rotulado(fecha),
        **contrato.columnas(),
        **columnas_de_apertura_confirmada(FECHA_DE_USUARIO, confirmado_por),
    )
    db.add(movement)
    await db.flush()
    await recalcular_costo_vigente(db, tenant_id, [product_id], movimientos_nuevos={movement.id})
    return movement


async def registrar_ajuste_manual(
    product_id: uuid.UUID,
    tenant_id: uuid.UUID,
    db: AsyncSession,
    *,
    nuevo: int | None = None,
    delta: int | None = None,
    esperado: int | None = None,
    contrato: ContratoDeCosto | None,
    actor: str,
) -> InventoryMovement | None:
    """Corrección manual del stock a un valor absoluto (M·P2c).

    Escribe el delta como ``adjustment`` + ``manual_adjustment`` y mueve
    ``stock_units`` en forma incremental. NO es ``catalog_initial_stock``: ese
    origen es el saldo de apertura, y el chequeo temporal y la deduplicación lo
    leen como tal. Una suba lleva el costo declarado (``contrato``), que el costo
    promedio usa; una baja es una salida.

    Se pasa ``nuevo`` (el valor absoluto que fijó el usuario: pantalla de
    productos) o ``delta`` (cuánto sube o baja: el chat). ``esperado``: el stock
    que el usuario veía al editar. Si cambió en el medio (una venta), se rechaza
    con ``StockCambioError`` en vez de pisarla. Devuelve ``None`` si no hay
    cambio.
    """
    if (nuevo is None) == (delta is None):
        raise ValueError("Se pasa nuevo o delta, uno de los dos.")
    product = await _producto_para_stock_manual(product_id, tenant_id, db)
    actual = product.stock_units
    if esperado is not None and esperado != actual:
        raise StockCambioError(esperado, actual)
    if delta is None:
        assert nuevo is not None
        delta = nuevo - actual
    if actual + delta < 0:
        raise StockNegativoError("El stock no puede quedar negativo.")
    if delta == 0:
        return None
    balance = await _get_or_create_balance(product, tenant_id, db)
    balance.current_qty += delta
    product.stock_units += delta
    columnas = contrato.columnas() if (contrato is not None and delta > 0) else {}
    movement = InventoryMovement(
        tenant_id=tenant_id,
        product_id=product_id,
        movement_type="adjustment",
        qty=delta,
        unit_cost=columnas.get("unit_cost_final"),
        reason=f"Ajuste manual de stock ({actor})",
        source_event_id=f"manual:ajuste:{uuid.uuid4()}",
        source_type=SOURCE_MANUAL_ADJUSTMENT,
        occurred_at=momento_de_negocio_rotulado(None),
        **columnas,
    )
    db.add(movement)
    await db.flush()
    # Sin ``recalcular_costo_vigente``: un ajuste manual no es fuente del costo
    # de referencia (sólo compras y apertura lo son); el costo declarado ya es el
    # del producto o entra como edición manual por la ruta.
    return movement


async def register_stock_loss(
    product_id: uuid.UUID,
    tenant_id: uuid.UUID,
    qty: int,
    reason: str,
    actor_user_id: uuid.UUID | None,
    db: AsyncSession,
) -> InventoryMovement:
    """HIGH risk: decrements stock and creates a reinforced audit entry."""
    # F3-T3 review: acquire al tope del entry point.
    await maintenance_lock_service.acquire_write_lock_shared(db, tenant_id)
    # T2: la merma aprobada por chat descontaba sobre una lectura sin candado.
    product = await bloquear_producto(db, tenant_id, product_id, lock_held=True)
    if product is None:
        raise ValueError(f"Product {product_id} not found for tenant {tenant_id}")

    balance = await _get_or_create_balance(product, tenant_id, db)
    balance.current_qty -= qty
    # Mantener Product.stock_units (lo que lee la UI) en sync con el balance.
    product.stock_units = max(0, product.stock_units - qty)

    movement = InventoryMovement(
        tenant_id=tenant_id,
        product_id=product_id,
        movement_type="loss",
        qty=-qty,
        reason=reason,
        # La merma se registra cuando se DESCUBRE, no tiene fecha de negocio propia
        # distinta del momento del registro (en hora AR rotulada UTC, M·2a).
        occurred_at=momento_de_negocio_rotulado(None),
    )
    db.add(movement)
    await db.flush()

    audit = DecisionAuditLog(
        tenant_id=tenant_id,
        decision_type="STOCK_LOSS",
        decision_data={
            "product_id": str(product_id),
            "product_name": product.name,
            "qty_lost": qty,
            "reason": reason,
            "movement_id": str(movement.id),
        },
        triggered_by="agent_stock",
        actor_user_id=actor_user_id,
        context={"timestamp": datetime.now(UTC).isoformat()},
        created_at=datetime.now(UTC),
    )
    db.add(audit)
    await db.flush()

    return movement


async def void_movement(
    movement: InventoryMovement,
    db: AsyncSession,
    *,
    lock_held: bool = False,
) -> None:
    """Soft-delete de un movimiento revirtiendo EXACTAMENTE su efecto en stock/balance.

    ``qty`` es con signo (compra +, venta/merma −), así que restar ``movement.qty``
    deshace exactamente lo que ese movimiento aportó. Idempotente: si ya está voidado,
    no hace nada. Lo usan el reread (borra la lectura anterior antes de reimportar) y la
    reparación.

    Reversa INCREMENTAL a propósito (no recomputar desde el ledger): ``stock_units`` NO
    es siempre ``Σ(movimientos)`` — hay base no-ledger (alta manual vía POST /products,
    chat, seed, y el catálogo que setea stock absoluto pero registra solo el delta).
    Recomputar desde el ledger destruiría esa base. Tampoco se clampa a 0 acá: el clamp
    intermedio se comería, por ejemplo, ventas posteriores a una compra que se relee
    (void −100 sobre stock 60 ⇒ −40; el reimport +100 ⇒ 60). El piso a 0 va, si hace
    falta, en el estado final del llamador, nunca en un paso de la reversa.

    ``lock_held``: ver ``_get_or_create_balance``. Solo lo pasa un caller que ya tomó
    el shared lock del tenant en ESTA transacción (hoy: el bucle de void de la
    relectura, bajo el lock que ``apply_reread`` toma en su primera línea). No cambia
    qué se protege — cambia cuántas veces se pide lo mismo.
    """
    if movement.voided_at is not None:
        return
    # F3-T3 review: acquire al tope del entry point, antes de cualquier lectura/lock.
    if not lock_held:
        await maintenance_lock_service.acquire_write_lock_shared(db, movement.tenant_id)
    # T2: con candado y refrescado. Leído sin candado, una venta de la otra caja
    # que commitea en el medio se perdía (stock 10, vende 3, se anula 2 → 12).
    # El advisory ya está tomado: arriba, o por el caller con `lock_held`.
    product = await bloquear_producto(
        db, movement.tenant_id, movement.product_id, lock_held=True
    )
    if product is not None:
        # Sembrar el balance ANTES de mutar stock_units: _get_or_create_balance inicializa
        # current_qty desde product.stock_units, así que si mutáramos primero el balance
        # nuevo arrancaría ya descontado y la resta siguiente lo descontaría dos veces.
        balance = await _get_or_create_balance(
            product, movement.tenant_id, db, lock_held=lock_held
        )
        product.stock_units -= movement.qty
        balance.current_qty -= movement.qty
    movement.voided_at = datetime.now(UTC)
    await db.flush()
    # M·1c-2: anular una compra puede cambiar el costo vigente (si era la que
    # había ganado). Se recalcula al final del lote que la anuló.
    if movement.movement_type == "purchase":
        marcar_para_recalcular(db, movement.tenant_id, movement.product_id)


async def unvoid_movement(
    movement: InventoryMovement,
    db: AsyncSession,
) -> None:
    """Reversa de ``void_movement``: re-activa el movimiento y vuelve a aplicar su efecto.

    Incremental (suma ``movement.qty`` de vuelta), por la misma razón que ``void_movement``
    no recomputa: preserva la base no-ledger del stock. Idempotente si ya está vivo.
    """
    if movement.voided_at is None:
        return
    # F3-T3 review: acquire al tope del entry point, antes de cualquier lectura/lock.
    await maintenance_lock_service.acquire_write_lock_shared(db, movement.tenant_id)
    product = await bloquear_producto(  # T2
        db, movement.tenant_id, movement.product_id, lock_held=True
    )
    if product is not None:
        balance = await _get_or_create_balance(product, movement.tenant_id, db)
        product.stock_units += movement.qty
        balance.current_qty += movement.qty
    movement.voided_at = None
    await db.flush()
    if movement.movement_type == "purchase":
        marcar_para_recalcular(db, movement.tenant_id, movement.product_id)


async def _live_sale_movement(
    sale_id: uuid.UUID,
    tenant_id: uuid.UUID,
    db: AsyncSession,
) -> InventoryMovement | None:
    """El movimiento 'sale' VIVO de una venta, si ya se descontó. None si no."""
    result = await db.execute(
        select(InventoryMovement).where(
            InventoryMovement.tenant_id == tenant_id,
            InventoryMovement.source_event_id == sale_source_event_id(sale_id),
            InventoryMovement.movement_type == "sale",
            InventoryMovement.voided_at.is_(None),
        )
    )
    return result.scalar_one_or_none()


async def venta_descontada(
    sale_id: uuid.UUID,
    tenant_id: uuid.UUID,
    db: AsyncSession,
) -> bool:
    """¿La venta tiene HOY un movimiento 'sale' vivo en el ledger?

    T3 (C-4, B-9): es la pregunta que decide si editar o anular una venta toca el
    stock, y se contesta mirando el ledger, no el origen de la fila. Una venta
    importada puede estar descontada (el confirm aplica la historia, F-F.4) y una
    en vivo legada puede no estarlo (anterior a que la carga manual descontara).
    """
    return await _live_sale_movement(sale_id, tenant_id, db) is not None


async def decrement_for_sale_id(
    sale_id: uuid.UUID,
    tenant_id: uuid.UUID,
    db: AsyncSession,
) -> InventoryMovement | None:
    """Variante por id: carga la venta desde la DB y delega en ``decrement_for_sale``.

    La usa el backstop por evento (``on_sale_recorded``), que solo tiene el id. Los
    flujos síncronos ya tienen el ``SaleEntry`` cargado y llaman directo a
    ``decrement_for_sale`` sin reconsultar.
    """
    sale = (
        await db.execute(
            select(SaleEntry).where(
                SaleEntry.id == sale_id,
                SaleEntry.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    if sale is None:
        return None
    return await decrement_for_sale(sale, db)


async def decrement_for_sale(
    sale: SaleEntry,
    db: AsyncSession,
    *,
    source_upload_id: uuid.UUID | None = None,
    source_type: str | None = None,
) -> InventoryMovement | None:
    """Descuenta stock por una venta EN VIVO. Idempotente por ``source_event_id="sale:{id}"``.

    Único punto de descuento de ventas en vivo (chat, POST /sales, /sales/bulk,
    manual-batch y el backstop por evento ``SALE_RECORDED``). No-op (``None``) si la
    venta no tiene producto, ``quantity`` <= 0, está anulada, o ya existe su movimiento
    'sale' vivo.

    **NO se permite stock negativo:** si el producto no tiene stock suficiente levanta
    ``InsufficientStockError`` (mensaje claro; el caller la mapea a 400 / la propaga al
    chat) ANTES de crear el movimiento. La validación usa lock de fila (FOR UPDATE), así
    que ventas concurrentes del mismo producto se serializan y no sobrevenden.

    Idempotencia en dos capas:
    - Fast-path app: el SELECT de arriba cubre el doble-call en la MISMA sesión (el
      movimiento ya flusheado es visible) y el evento corriendo DESPUÉS del commit.
    - Race sync (request) + async (evento, otra sesión sin ver lo no-commiteado): el
      índice único parcial de la migración rechaza el 2º INSERT → ``IntegrityError``
      dentro del savepoint → se revierte solo ese descuento y se trata como no-op.

    ``source_upload_id``/``source_type`` (T3): sólo para REHACER el descuento de una
    venta importada que se edita. Sin ellos el movimiento nuevo quedaría sin
    procedencia y el borrado o la relectura del archivo (que anulan por
    ``source_upload_id``) ya no lo encontrarían: el stock quedaría descontado por
    una venta que el borrado sí anuló.
    """
    if sale.product_id is None or sale.voided_at is not None:
        return None
    qty = int(sale.quantity or 0)
    if qty <= 0:
        return None
    if await _live_sale_movement(sale.id, sale.tenant_id, db) is not None:
        return None
    # F3-T3 review: acquire explícito acá también (aunque check_stock_available ya lo
    # toma) para que el orden quede garantizado en este entry point sin depender de
    # que el próximo cambio en check_stock_available lo preserve.
    await maintenance_lock_service.acquire_write_lock_shared(db, sale.tenant_id)
    # NO se permite stock negativo: validar (con lock) antes de descontar. Si no alcanza,
    # levanta InsufficientStockError y la venta NO se descuenta (ni se persiste: el request
    # revierte). El producto bloqueado queda en el identity map → decrement_stock lo reusa.
    await check_stock_available(sale.product_id, sale.tenant_id, qty, db)
    # Fecha de negocio: la carga masiva (POST /sales/bulk) usa un `date` (period_date);
    # el resto, `datetime`. `datetime` es subclase de `date`, así que normalizamos solo
    # el `date` puro a medianoche antes de que ensure_utc (que espera datetime) lo procese.
    occurred = sale.transaction_date
    if isinstance(occurred, date) and not isinstance(occurred, datetime):
        occurred = datetime(occurred.year, occurred.month, occurred.day)
    try:
        async with guarded_savepoint(db, LIVE_SALE_EVENT_CONFLICT):
            return await decrement_stock(
                product_id=sale.product_id,
                tenant_id=sale.tenant_id,
                qty=qty,
                source_event_id=sale_source_event_id(sale.id),
                db=db,
                occurred_at=occurred,
                source_upload_id=source_upload_id,
                source_type=source_type,
            )
    except SavepointConflictError:
        # El movimiento ya lo creó la otra rama (request vs evento) → no-op, que es la
        # semántica idempotente documentada arriba.
        return None


async def revert_sale_stock(
    sale_id: uuid.UUID,
    tenant_id: uuid.UUID,
    db: AsyncSession,
) -> bool:
    """Revierte el descuento de una venta anulada/editada. Incremental (``void_movement``).

    Devuelve ``True`` si había un movimiento de stock que revertir, ``False`` si no
    (venta sin producto, o que nunca descontó). Busca por ``sale:{id}`` sin mirar el
    origen: revierte igual el descuento de una venta en vivo que el que el confirm
    aplicó a una importada. El PATCH decide ANTES, con ``venta_descontada``, si
    rehace el stock (T3): una venta que nunca descontó no empieza a descontar.
    """
    movement = await _live_sale_movement(sale_id, tenant_id, db)
    if movement is None:
        return False
    # F3-T3 review: acquire explícito acá también (void_movement ya lo toma) para
    # fijar el orden en este entry point independientemente de void_movement.
    await maintenance_lock_service.acquire_write_lock_shared(db, tenant_id)
    await void_movement(movement, db)
    return True


async def validate_sale_update_stock(
    sale_id: uuid.UUID,
    tenant_id: uuid.UUID,
    prev_product_id: uuid.UUID | None,
    prev_quantity: int,
    new_product_id: uuid.UUID | None,
    new_quantity: int,
    db: AsyncSession,
) -> None:
    """Valida (SIN mutar) el efecto neto de editar una venta: NO stock negativo.

    T3: vale para cualquier venta con movimiento vivo (en vivo o importada y ya
    descontada) y para la que recién se asocia a un producto; quién llega acá lo
    decide el caller mirando el ledger (``venta_descontada``).

    Modela ``revert_sale_stock`` (repone ``prev_quantity`` al ``prev_product_id`` si la
    venta tenía movimiento vivo) + ``decrement_for_sale`` (descuenta ``new_quantity`` de
    ``new_product_id``). Levanta ``InsufficientStockError`` si el producto nuevo no
    alcanza. Se llama ANTES de mutar/persistir la venta para que un rechazo no deje
    estado sucio (la validación de ``decrement_for_sale`` corre igual después, como red).

    No muta. T2: BLOQUEA el producto viejo y el nuevo, en orden de id, hasta el
    commit: la validación y la reposición/descuento que siguen ven el mismo stock,
    y una venta concurrente no se cuela entre ellas. No-op si la venta pasa a
    quedar sin producto (solo repone) o con ``new_quantity`` <= 0.
    """
    if new_product_id is None or new_quantity <= 0:
        return
    had_movement = await venta_descontada(sale_id, tenant_id, db)
    bloqueados = await bloquear_productos(
        db, tenant_id, [pid for pid in (prev_product_id, new_product_id) if pid is not None]
    )
    product = bloqueados.get(new_product_id)
    if product is None:
        raise SaleProductNotFoundError(new_product_id)
    available = product.stock_units
    # Si el movimiento viejo se repone sobre el MISMO producto nuevo, ese stock vuelve a
    # estar disponible (efecto incremental real: subir de 3 a 5 solo necesita 2 más).
    if had_movement and new_product_id == prev_product_id:
        available += int(prev_quantity or 0)
    if available < new_quantity:
        raise InsufficientStockError(new_product_id, available, new_quantity)


async def get_low_stock_products(
    tenant_id: uuid.UUID,
    db: AsyncSession,
) -> list[Product]:
    result = await db.execute(
        select(Product)
        .join(
            InventoryBalance,
            (InventoryBalance.product_id == Product.id) & (InventoryBalance.tenant_id == tenant_id),
        )
        .where(
            Product.tenant_id == tenant_id,
            Product.is_active.is_(True),
            InventoryBalance.current_qty <= func.coalesce(Product.low_stock_threshold_units, 5),
        )
    )
    return list(result.scalars().all())


async def get_overstock_products(
    tenant_id: uuid.UUID,
    vertical: Vertical,
    db: AsyncSession,
) -> list[Product]:
    """Returns products whose rotation_days exceeds the heuristic overstock threshold."""
    config = HeuristicEngine.get(vertical)

    result = await db.execute(
        select(Product, InventoryBalance)
        .join(
            InventoryBalance,
            (InventoryBalance.product_id == Product.id) & (InventoryBalance.tenant_id == tenant_id),
        )
        .where(
            Product.tenant_id == tenant_id,
            Product.is_active.is_(True),
        )
    )
    rows = result.all()

    overstock: list[Product] = []
    for product, balance in rows:
        # Simple proxy: days of stock = current_qty / avg_daily_sales
        # Without sales history we approximate: if current_qty is very large relative
        # to a reference daily rate (1 unit/day), rotation_days ≈ current_qty.
        # In production this would use real sales velocity from the BSL.
        rotation_days = float(balance.current_qty)
        if config.is_overstock(rotation_days):
            overstock.append(product)

    return overstock
