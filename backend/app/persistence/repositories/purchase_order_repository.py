"""Repository para PurchaseOrder. Siempre filtra por tenant_id."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.purchase_order import PurchaseOrder


class PurchaseOrderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, po: PurchaseOrder) -> PurchaseOrder:
        """Persiste un PurchaseOrder nuevo y devuelve la instancia con id asignado."""
        self._session.add(po)
        await self._session.flush()
        return po

    async def list_by_tenant(self, tenant_id: UUID) -> list[PurchaseOrder]:
        """Lista todos los PurchaseOrders del tenant, ordenados por created_at desc."""
        result = await self._session.execute(
            select(PurchaseOrder)
            .where(PurchaseOrder.tenant_id == tenant_id)
            .order_by(PurchaseOrder.created_at.desc())
        )
        return list(result.scalars().all())

    async def list_by_supplier(
        self,
        supplier_id: UUID,
        tenant_id: UUID,
    ) -> list[PurchaseOrder]:
        """Lista PurchaseOrders de un proveedor específico del tenant."""
        result = await self._session.execute(
            select(PurchaseOrder)
            .where(
                PurchaseOrder.supplier_id == supplier_id,
                PurchaseOrder.tenant_id == tenant_id,
            )
            .order_by(PurchaseOrder.created_at.desc())
        )
        return list(result.scalars().all())
