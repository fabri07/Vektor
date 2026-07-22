"""Helpers compartidos por los tests de repositorios de compras de proveedor.

``_product``/``_purchase`` los usan ``test_supplier_products_by_brand`` y
``test_suppliers_purchase_breakdown``. Variante permisiva de ``marca``: ``None``
→ sin la clave; ``""``/``"   "`` sí se guardan (los tests de derivación de brand
verifican que el repo mapee esos casos a ``None``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product


async def _product(
    session: AsyncSession, tenant_id: uuid.UUID, name: str, marca: str | None = None
) -> Product:
    p = Product(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name=name,
        sale_price_ars=Decimal("100"),
        stock_units=5,
        provenance="REAL",
        custom_fields={"marca": marca} if marca is not None else {},
    )
    session.add(p)
    await session.flush()
    return p


async def _purchase(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    product_id: uuid.UUID,
    supplier_id: uuid.UUID | None,
    qty: int,
    unit_cost: Decimal | None,
    voided: bool = False,
    occurred_at: datetime | None = None,
    created_at: datetime | None = None,
    movement_id: uuid.UUID | None = None,
) -> None:
    kwargs: dict[str, object] = {}
    if occurred_at is not None:
        kwargs["occurred_at"] = occurred_at
    if created_at is not None:
        # Sobrescribe el server_default: sirve para probar que la fecha de negocio
        # (occurred_at) manda sobre la de carga (created_at) — F6-B3.
        kwargs["created_at"] = created_at
    session.add(
        InventoryMovement(
            id=movement_id or uuid.uuid4(),
            tenant_id=tenant_id,
            product_id=product_id,
            supplier_id=supplier_id,
            movement_type="purchase",
            qty=qty,
            unit_cost=unit_cost,
            voided_at=datetime.now(UTC) if voided else None,
            **kwargs,
        )
    )
