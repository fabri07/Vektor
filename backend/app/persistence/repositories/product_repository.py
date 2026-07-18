"""Repository for Product catalog queries."""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import maintenance_lock_service
from app.persistence.models.product import Product


class ProductRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, product_id: UUID, tenant_id: UUID) -> Product | None:
        result = await self._session.execute(
            select(Product).where(
                Product.id == product_id,
                Product.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_by_tenant(
        self,
        tenant_id: UUID,
        is_active: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Product]:
        q = select(Product).where(Product.tenant_id == tenant_id)
        if is_active is not None:
            q = q.where(Product.is_active == is_active)
        q = q.limit(limit).offset(offset)
        result = await self._session.execute(q)
        return list(result.scalars().all())

    async def get_products_with_margin(self, tenant_id: UUID) -> list[dict[str, Any]]:
        """Catálogo activo con margen bruto calculado por producto (Sprint 17).

        Solo calcula margen donde `unit_cost_ars` y `sale_price_ars` están presentes
        y `sale_price_ars > 0`. Los productos sin costo se devuelven con
        `margin_pct=None` (NO se inventa un margen — política no-invention).

        Returns list[dict] con: product_id, name, sku, category, sale_price,
        unit_cost, stock_units, margin_pct (float|None), margin_abs (float|None).
        """
        result = await self._session.execute(
            select(Product).where(
                Product.tenant_id == tenant_id,
                Product.is_active.is_(True),
            )
        )
        products = list(result.scalars().all())
        out: list[dict[str, Any]] = []
        for p in products:
            sale_price = float(p.sale_price_ars or 0)
            unit_cost = float(p.unit_cost_ars) if p.unit_cost_ars is not None else None
            margin_pct: float | None = None
            margin_abs: float | None = None
            if unit_cost is not None and sale_price > 0:
                margin_abs = round(sale_price - unit_cost, 2)
                margin_pct = round((sale_price - unit_cost) / sale_price * 100, 1)
            out.append(
                {
                    "product_id": str(p.id),
                    "name": p.name,
                    "sku": p.sku,
                    "category": p.category,
                    "sale_price": sale_price,
                    "unit_cost": unit_cost,
                    "stock_units": p.stock_units,
                    "margin_pct": margin_pct,
                    "margin_abs": margin_abs,
                }
            )
        return out

    async def save(self, product: Product) -> Product:
        # F3-T3: shared lock ANTES de crear/actualizar el producto — barrera de
        # exclusión mutua real contra el dedup (que toma el exclusive). No-op en SQLite.
        await maintenance_lock_service.acquire_write_lock_shared(
            self._session, product.tenant_id
        )
        self._session.add(product)
        await self._session.flush()
        return product
