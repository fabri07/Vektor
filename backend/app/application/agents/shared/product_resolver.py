"""Resolución de product_id desde nombre/SKU en la DB del tenant."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.product import Product


async def resolve_product_id(
    db: AsyncSession,
    tenant_id: str,
    product_name: str | None,
    sku: str | None,
) -> tuple[str | None, list[str]]:
    """Busca product_id por SKU (exacto) o nombre (ILIKE).

    Returns:
        (product_id, alternatives) — si hay 1 match exacto devuelve el id;
        si hay múltiples devuelve (None, [nombres]); si no hay ninguno (None, []).
    """
    tid = uuid.UUID(tenant_id)

    if sku:
        result = await db.execute(
            select(Product).where(Product.tenant_id == tid, Product.sku == sku)
        )
        product = result.scalar_one_or_none()
        if product:
            return str(product.id), []

    if product_name:
        result = await db.execute(
            select(Product).where(
                Product.tenant_id == tid,
                Product.name.ilike(f"%{product_name}%"),
            )
        )
        matches = list(result.scalars().all())
        if len(matches) == 1:
            return str(matches[0].id), []
        if len(matches) > 1:
            return None, [m.name for m in matches[:5]]

    return None, []
