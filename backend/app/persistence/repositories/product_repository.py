"""Repository for Product catalog queries."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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

    async def save(self, product: Product) -> Product:
        self._session.add(product)
        await self._session.flush()
        return product
