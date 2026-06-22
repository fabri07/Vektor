"""Repository for inventory movement queries. Always filters by tenant_id."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product


@dataclass(frozen=True)
class SupplierProductPurchase:
    """Una fila de la tabla "productos comprados a un proveedor"."""

    product_id: UUID
    name: str
    last_purchase_at: datetime | None
    total_qty: float
    unit_price: Decimal


class InventoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def products_purchased_from_supplier(
        self, tenant_id: UUID, supplier_id: UUID
    ) -> list[SupplierProductPurchase]:
        """Productos comprados a un proveedor, agregados por producto.

        Fuente: ``inventory_movements`` con ``movement_type='purchase'`` y
        ``supplier_id`` del proveedor. Por producto:
          - ``total_qty`` = ``SUM(qty)`` (suma de unidades compradas);
          - ``last_purchase_at`` = ``MAX(created_at)`` (última compra);
          - ``unit_price`` = ``unit_cost`` del movimiento MÁS RECIENTE del producto.

        SIEMPRE filtra por ``tenant_id`` (aislamiento multi-tenant). Orden por
        última compra descendente.
        """
        # Agregados por producto (cantidad total + fecha de última compra).
        agg = (
            select(
                InventoryMovement.product_id.label("product_id"),
                func.coalesce(func.sum(InventoryMovement.qty), 0).label("total_qty"),
                func.max(InventoryMovement.created_at).label("last_purchase_at"),
            )
            .where(
                InventoryMovement.tenant_id == tenant_id,
                InventoryMovement.supplier_id == supplier_id,
                InventoryMovement.movement_type == "purchase",
            )
            .group_by(InventoryMovement.product_id)
            .subquery()
        )

        # Costo unitario del movimiento más reciente por producto: se busca el
        # unit_cost del movimiento cuyo created_at == MAX(created_at) del producto.
        # (Si hay empate de timestamp, gana cualquiera — caso borde improbable.)
        latest_cost = (
            select(
                InventoryMovement.product_id.label("product_id"),
                InventoryMovement.unit_cost.label("unit_cost"),
                func.row_number()
                .over(
                    partition_by=InventoryMovement.product_id,
                    order_by=InventoryMovement.created_at.desc(),
                )
                .label("rn"),
            )
            .where(
                InventoryMovement.tenant_id == tenant_id,
                InventoryMovement.supplier_id == supplier_id,
                InventoryMovement.movement_type == "purchase",
            )
            .subquery()
        )

        q = (
            select(
                agg.c.product_id,
                Product.name,
                agg.c.last_purchase_at,
                agg.c.total_qty,
                latest_cost.c.unit_cost,
            )
            .join(Product, Product.id == agg.c.product_id)
            .join(
                latest_cost,
                (latest_cost.c.product_id == agg.c.product_id) & (latest_cost.c.rn == 1),
            )
            .where(Product.tenant_id == tenant_id)
            .order_by(agg.c.last_purchase_at.desc().nullslast())
        )
        result = await self._session.execute(q)
        rows = result.all()
        return [
            SupplierProductPurchase(
                product_id=row.product_id,
                name=row.name,
                last_purchase_at=row.last_purchase_at,
                total_qty=float(row.total_qty or 0),
                unit_price=row.unit_cost if row.unit_cost is not None else Decimal("0"),
            )
            for row in rows
        ]
