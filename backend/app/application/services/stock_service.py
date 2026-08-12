"""Stock service — inventory operations without LLM."""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.shared.event_bus import EventBus
from app.application.agents.shared.heuristic_engine import HeuristicEngine
from app.application.services import maintenance_lock_service
from app.application.services._savepoint import (
    SavepointConflictError,
    guarded_savepoint,
    unique_violation_classifier,
)
from app.application.services.inventory_movement_origin import ensure_utc
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
INSUFFICIENT_STOCK_MESSAGE = (
    "No hay stock suficiente para registrar esta venta. "
    "Cargá primero la compra o stock inicial de mercadería."
)


PRODUCT_NOT_FOUND_MESSAGE = "El producto de la venta no existe o no pertenece al negocio."


class InsufficientStockError(Exception):
    """Venta EN VIVO con producto pero sin stock suficiente. NO se permite stock negativo.

    Lleva ``user_message`` para que ``execute_local_action`` (chat) la muestre tal cual y
    el handler global de ``main.py`` la mapee a 400 con el mismo mensaje. La venta NO se
    persiste (el rollback de la transacción del request deshace cualquier SaleEntry).
    """

    def __init__(self, product_id: uuid.UUID, available: int, requested: int) -> None:
        self.product_id = product_id
        self.available = available
        self.requested = requested
        self.user_message = INSUFFICIENT_STOCK_MESSAGE
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
    await maintenance_lock_service.acquire_write_lock_shared(db, tenant_id)
    product = (
        await db.execute(
            select(Product)
            .where(Product.id == product_id, Product.tenant_id == tenant_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if product is None:
        raise SaleProductNotFoundError(product_id)
    if product.stock_units < qty:
        raise InsufficientStockError(product_id, product.stock_units, qty)
    return product


async def _get_or_create_balance(
    product: Product,
    tenant_id: uuid.UUID,
    db: AsyncSession,
) -> InventoryBalance:
    # F3-T3: chokepoint COMÚN de toda mutación de balance/stock (decrement_stock,
    # increment_stock, register_stock_loss, void_movement, unvoid_movement — y
    # transitivamente decrement_for_sale/revert_sale_stock, que pasan por acá).
    # Shared lock ANTES de mutar: barrera de exclusión mutua real contra el dedup
    # (que toma el exclusive). No-op en SQLite.
    await maintenance_lock_service.acquire_write_lock_shared(db, tenant_id)
    result = await db.execute(
        select(InventoryBalance).where(
            InventoryBalance.product_id == product.id,
            InventoryBalance.tenant_id == tenant_id,
        )
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
    product = await db.get(Product, product_id)
    if product is None or product.tenant_id != tenant_id:
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
        occurred_at=ensure_utc(occurred_at) or datetime.now(UTC),
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
    ids = list({p.id for p in productos})
    encontrados: dict[uuid.UUID, InventoryBalance] = {}
    for inicio in range(0, len(ids), BULK_DECREMENT_CHUNK_SIZE):
        filas = await db.execute(
            select(InventoryBalance).where(
                InventoryBalance.tenant_id == tenant_id,
                InventoryBalance.product_id.in_(ids[inicio : inicio + BULK_DECREMENT_CHUNK_SIZE]),
            )
        )
        for balance in filas.scalars():
            encontrados[balance.product_id] = balance
    for producto in productos:
        if producto.id not in encontrados:
            encontrados[producto.id] = await _get_or_create_balance(producto, tenant_id, db)
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
        occurred_at=ensure_utc(item.occurred_at) or datetime.now(UTC),
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

    # El saldo se acumula en memoria y se escribe UNA vez por producto. El clamp a 0
    # se replica **paso a paso**, no al final: ``stock_units`` no puede ir abajo de
    # cero, así que una venta que pasa por el piso deja las siguientes restando desde
    # 0. Colapsarlo en un solo ``max(0, ...)`` sobre el total daría otro número justo
    # en el caso que el clamp existe para cubrir. ``current_qty`` sí es resta pura:
    # el balance registra el saldo real, negativo incluido.
    for item in aplicados:
        producto = item.product
        producto.stock_units = max(0, producto.stock_units - item.qty)
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
    update_product_cost: bool = True,
    occurred_at: datetime | None = None,
    defer_event: bool = False,
) -> InventoryMovement:
    # F3-T3 review: advisory shared ANTES del FOR UPDATE de fila (orden fijo, evita
    # deadlock AB-BA contra el exclusive del dedup).
    await maintenance_lock_service.acquire_write_lock_shared(db, tenant_id)
    # Serializa compras concurrentes del mismo producto para no perder incrementos
    # en ``stock_units``/``InventoryBalance.current_qty``.
    product = (
        await db.execute(
            select(Product)
            .where(Product.id == product_id, Product.tenant_id == tenant_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if product is None:
        raise ValueError(f"Product {product_id} not found for tenant {tenant_id}")

    balance = await _get_or_create_balance(product, tenant_id, db)
    balance.current_qty += qty
    # Product.stock_units es la representación canónica que lee la UI: sin esto,
    # una compra confirmada subía el balance pero el stock visible no cambiaba.
    product.stock_units += qty
    # El costo histórico SIEMPRE queda en el movimiento (unit_cost abajo). El costo
    # del catálogo (product.unit_cost_ars) solo se pisa si update_product_cost: una
    # compra de un producto existente no debe cambiar el costo de referencia salvo
    # que el usuario lo confirme.
    if unit_cost is not None and update_product_cost:
        product.unit_cost_ars = unit_cost

    movement = InventoryMovement(
        tenant_id=tenant_id,
        product_id=product_id,
        movement_type="purchase",
        qty=qty,
        unit_cost=unit_cost,
        source_event_id=source_event_id,
        supplier_id=supplier_id,
        # Fecha de NEGOCIO de la compra: si el caller la conoce, se usa esa; si no,
        # el momento del registro.
        occurred_at=ensure_utc(occurred_at) or datetime.now(UTC),
    )
    db.add(movement)
    await db.flush()

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
    product = await db.get(Product, product_id)
    if product is None or product.tenant_id != tenant_id:
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
        # distinta del momento del registro.
        occurred_at=datetime.now(UTC),
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
    """
    if movement.voided_at is not None:
        return
    # F3-T3 review: acquire al tope del entry point, antes de cualquier lectura/lock.
    await maintenance_lock_service.acquire_write_lock_shared(db, movement.tenant_id)
    product = await db.get(Product, movement.product_id)
    if product is not None and product.tenant_id == movement.tenant_id:
        # Sembrar el balance ANTES de mutar stock_units: _get_or_create_balance inicializa
        # current_qty desde product.stock_units, así que si mutáramos primero el balance
        # nuevo arrancaría ya descontado y la resta siguiente lo descontaría dos veces.
        balance = await _get_or_create_balance(product, movement.tenant_id, db)
        product.stock_units -= movement.qty
        balance.current_qty -= movement.qty
    movement.voided_at = datetime.now(UTC)
    await db.flush()


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
    product = await db.get(Product, movement.product_id)
    if product is not None and product.tenant_id == movement.tenant_id:
        balance = await _get_or_create_balance(product, movement.tenant_id, db)
        product.stock_units += movement.qty
        balance.current_qty += movement.qty
    movement.voided_at = None
    await db.flush()


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
    (venta sin producto, o histórica importada que nunca descontó — el import no
    descuenta a propósito). El caller de PATCH usa el bool para NO empezar a trackear
    stock de una venta que nunca lo trackeó (editar historia importada no debe descontar).
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
    """Valida (SIN mutar) el efecto neto de editar una venta EN VIVO: NO stock negativo.

    Modela ``revert_sale_stock`` (repone ``prev_quantity`` al ``prev_product_id`` si la
    venta tenía movimiento vivo) + ``decrement_for_sale`` (descuenta ``new_quantity`` de
    ``new_product_id``). Levanta ``InsufficientStockError`` si el producto nuevo no
    alcanza. Se llama ANTES de mutar/persistir la venta para que un rechazo no deje
    estado sucio (la validación de ``decrement_for_sale`` corre igual después, como red).

    Solo hace SELECTs. No-op si la venta pasa a quedar sin producto (solo repone) o con
    ``new_quantity`` <= 0.
    """
    if new_product_id is None or new_quantity <= 0:
        return
    had_movement = await _live_sale_movement(sale_id, tenant_id, db) is not None
    product = await db.get(Product, new_product_id)
    if product is None or product.tenant_id != tenant_id:
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
