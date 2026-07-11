"""Stock service — inventory operations without LLM."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.shared.event_bus import EventBus
from app.application.agents.shared.heuristic_engine import HeuristicEngine
from app.application.services.inventory_movement_origin import ensure_utc
from app.domain.product import effective_threshold
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.inventory import InventoryBalance, InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.transaction import SaleEntry

# Prefijo canónico del source_event_id de un descuento de venta EN VIVO. UNA venta ⇒
# UN movimiento 'sale' vivo (garantizado por el índice único parcial de la migración
# 20260729_0001). Habilita reversa exacta por venta e idempotencia sync+evento.
_SALE_SOURCE_PREFIX = "sale:"


def sale_source_event_id(sale_id: uuid.UUID) -> str:
    return f"{_SALE_SOURCE_PREFIX}{sale_id}"


async def _get_or_create_balance(
    product: Product,
    tenant_id: uuid.UUID,
    db: AsyncSession,
) -> InventoryBalance:
    result = await db.execute(
        select(InventoryBalance).where(
            InventoryBalance.product_id == product.id,
            InventoryBalance.tenant_id == tenant_id,
        )
    )
    balance = result.scalar_one_or_none()
    if balance is None:
        balance = InventoryBalance(
            tenant_id=tenant_id,
            product_id=product.id,
            current_qty=product.stock_units,
            reserved_qty=0,
        )
        db.add(balance)
        await db.flush()
    return balance


async def decrement_stock(
    product_id: uuid.UUID,
    tenant_id: uuid.UUID,
    qty: int,
    source_event_id: str | None,
    db: AsyncSession,
    *,
    occurred_at: datetime | None = None,
) -> InventoryMovement:
    product = await db.get(Product, product_id)
    if product is None or product.tenant_id != tenant_id:
        raise ValueError(f"Product {product_id} not found for tenant {tenant_id}")

    balance = await _get_or_create_balance(product, tenant_id, db)
    balance.current_qty -= qty
    # Mantener Product.stock_units (lo que lee la UI) en sync también al vender:
    # sin esto, las compras subían el stock visible pero las ventas nunca lo
    # bajaban y stock_units inflaba monotónicamente.
    # SIN clamp a 0 (a propósito): stock_units y balance.current_qty deben quedar en
    # lockstep para que la reversa de void_movement sea EXACTA (misma razón que el
    # invariante de void — no recomputar/no clampar en pasos intermedios). Una venta en
    # vivo sin stock suficiente (no bloqueada) puede dejarlo transitoriamente negativo:
    # eso es una señal real de sobreventa/stock sin cargar, y el void la revierte exacto.
    product.stock_units -= qty

    movement = InventoryMovement(
        tenant_id=tenant_id,
        product_id=product_id,
        movement_type="sale",
        qty=-qty,
        source_event_id=source_event_id,
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
) -> InventoryMovement:
    product = await db.get(Product, product_id)
    if product is None or product.tenant_id != tenant_id:
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

    EventBus.emit(
        "STOCK_INCREASED",
        {
            "tenant_id": str(tenant_id),
            "product_id": str(product_id),
            "qty": qty,
        },
    )

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
    # Fecha de negocio: la carga masiva (POST /sales/bulk) usa un `date` (period_date);
    # el resto, `datetime`. `datetime` es subclase de `date`, así que normalizamos solo
    # el `date` puro a medianoche antes de que ensure_utc (que espera datetime) lo procese.
    occurred = sale.transaction_date
    if isinstance(occurred, date) and not isinstance(occurred, datetime):
        occurred = datetime(occurred.year, occurred.month, occurred.day)
    try:
        async with db.begin_nested():
            return await decrement_stock(
                product_id=sale.product_id,
                tenant_id=sale.tenant_id,
                qty=qty,
                source_event_id=sale_source_event_id(sale.id),
                db=db,
                occurred_at=occurred,
            )
    except IntegrityError:
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
    await void_movement(movement, db)
    return True


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
    business_type: str,
    db: AsyncSession,
) -> list[Product]:
    """Returns products whose rotation_days exceeds the heuristic overstock threshold."""
    config = HeuristicEngine.get(business_type)

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
