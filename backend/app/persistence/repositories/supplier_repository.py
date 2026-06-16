"""Repository for Supplier queries. Always filters by tenant_id."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.supplier import Supplier


class SupplierRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, supplier_id: UUID, tenant_id: UUID) -> Supplier | None:
        result = await self._session.execute(
            select(Supplier).where(
                Supplier.id == supplier_id,
                Supplier.tenant_id == tenant_id,
                Supplier.deactivated_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def list_by_tenant(
        self,
        tenant_id: UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Supplier]:
        q = (
            select(Supplier)
            .where(
                Supplier.tenant_id == tenant_id,
                Supplier.deactivated_at.is_(None),
            )
            .order_by(Supplier.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(q)
        return list(result.scalars().all())

    async def count_active(self, tenant_id: UUID) -> int:
        """Cantidad de proveedores activos (no desactivados) del tenant."""
        q = select(func.count(Supplier.id)).where(
            Supplier.tenant_id == tenant_id,
            Supplier.deactivated_at.is_(None),
        )
        result = await self._session.execute(q)
        return int(result.scalar_one() or 0)

    async def save(self, supplier: Supplier) -> Supplier:
        self._session.add(supplier)
        await self._session.flush()
        return supplier

    async def soft_delete(self, supplier: Supplier) -> Supplier:
        """Marca el proveedor como desactivado (soft-delete). No borra la fila."""
        supplier.deactivated_at = datetime.now(UTC)
        self._session.add(supplier)
        await self._session.flush()
        return supplier
