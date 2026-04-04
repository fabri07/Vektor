"""Stock service — inventory operations without LLM."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.shared.event_bus import EventBus
from app.application.agents.shared.heuristic_engine import HeuristicEngine
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.inventory import InventoryBalance, InventoryMovement
from app.persistence.models.product import Product


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
) -> InventoryMovement:
    product = await db.get(Product, product_id)
    if product is None or product.tenant_id != tenant_id:
        raise ValueError(f"Product {product_id} not found for tenant {tenant_id}")

    balance = await _get_or_create_balance(product, tenant_id, db)
    balance.current_qty -= qty

    movement = InventoryMovement(
        tenant_id=tenant_id,
        product_id=product_id,
        movement_type="sale",
        qty=-qty,
        source_event_id=source_event_id,
    )
    db.add(movement)
    await db.flush()

    EventBus.emit("STOCK_DECREASED", {
        "tenant_id": str(tenant_id),
        "product_id": str(product_id),
        "qty": qty,
        "source_event_id": source_event_id,
    })

    if balance.current_qty <= product.low_stock_threshold_units:
        EventBus.emit("STOCK_ALERT_CREATED", {
            "tenant_id": str(tenant_id),
            "product_id": str(product_id),
            "alert_type": "stockout_risk",
            "current_qty": balance.current_qty,
        })

    return movement


async def increment_stock(
    product_id: uuid.UUID,
    tenant_id: uuid.UUID,
    qty: int,
    unit_cost: Decimal | None,
    source_event_id: str | None,
    db: AsyncSession,
) -> InventoryMovement:
    product = await db.get(Product, product_id)
    if product is None or product.tenant_id != tenant_id:
        raise ValueError(f"Product {product_id} not found for tenant {tenant_id}")

    balance = await _get_or_create_balance(product, tenant_id, db)
    balance.current_qty += qty

    movement = InventoryMovement(
        tenant_id=tenant_id,
        product_id=product_id,
        movement_type="purchase",
        qty=qty,
        unit_cost=unit_cost,
        source_event_id=source_event_id,
    )
    db.add(movement)
    await db.flush()

    EventBus.emit("STOCK_INCREASED", {
        "tenant_id": str(tenant_id),
        "product_id": str(product_id),
        "qty": qty,
    })

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

    movement = InventoryMovement(
        tenant_id=tenant_id,
        product_id=product_id,
        movement_type="loss",
        qty=-qty,
        reason=reason,
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


async def get_low_stock_products(
    tenant_id: uuid.UUID,
    db: AsyncSession,
) -> list[Product]:
    result = await db.execute(
        select(Product)
        .join(
            InventoryBalance,
            (InventoryBalance.product_id == Product.id)
            & (InventoryBalance.tenant_id == tenant_id),
        )
        .where(
            Product.tenant_id == tenant_id,
            Product.is_active.is_(True),
            InventoryBalance.current_qty <= Product.low_stock_threshold_units,
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
            (InventoryBalance.product_id == Product.id)
            & (InventoryBalance.tenant_id == tenant_id),
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
