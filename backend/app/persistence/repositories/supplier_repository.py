"""Repository for Supplier queries. Always filters by tenant_id."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.supplier import BRAND_COLLAPSED_FLAG_KEY, Supplier
from app.persistence.models.transaction import ExpenseEntry
from app.persistence.repositories._jsonb_flags import flag_not_true_sql


def _not_brand_collapsed_or_active() -> ColumnElement[bool]:
    """Filtro: excluir del listado las marcas colapsadas (baja por error de
    clasificación, flag ``_brand_collapsed``) aun con ``include_inactive=True``.

    Un desactivado solo entra si NO tiene el flag; un activo entra siempre
    (si un colapsado se restaura vía script de revert, este le limpia el flag).
    """
    return or_(
        Supplier.deactivated_at.is_(None),
        flag_not_true_sql(Supplier.custom_fields, BRAND_COLLAPSED_FLAG_KEY),
    )


class SupplierRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(
        self,
        supplier_id: UUID,
        tenant_id: UUID,
        include_deactivated: bool = False,
    ) -> Supplier | None:
        conditions: list[Any] = [
            Supplier.id == supplier_id,
            Supplier.tenant_id == tenant_id,
        ]
        if not include_deactivated:
            conditions.append(Supplier.deactivated_at.is_(None))
        result = await self._session.execute(select(Supplier).where(*conditions))
        return result.scalar_one_or_none()

    async def find_by_name(self, name_normalized: str, tenant_id: UUID) -> Supplier | None:
        """Busca un proveedor activo del tenant por nombre normalizado (lower+trim)."""
        result = await self._session.execute(
            select(Supplier)
            .where(
                func.lower(func.trim(Supplier.name)) == name_normalized,
                Supplier.tenant_id == tenant_id,
                Supplier.deactivated_at.is_(None),
            )
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_by_tenant(
        self,
        tenant_id: UUID,
        limit: int = 50,
        offset: int = 0,
        include_inactive: bool = False,
    ) -> list[Supplier]:
        """Lista proveedores del tenant.

        ``include_inactive=True`` incluye los dados de baja REALES
        (``deactivated_at``) para mostrarlos en rojo en la UI. Las marcas
        colapsadas por error (flag ``_brand_collapsed``) nunca se listan.
        """
        conditions: list[Any] = [Supplier.tenant_id == tenant_id]
        if not include_inactive:
            conditions.append(Supplier.deactivated_at.is_(None))
        else:
            conditions.append(_not_brand_collapsed_or_active())
        q = (
            select(Supplier)
            .where(*conditions)
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

    async def count_history(self, supplier_id: UUID, tenant_id: UUID) -> int:
        """Cantidad de operaciones del proveedor: gastos no anulados + movimientos
        de inventario (compras). >0 ⇒ tiene historial."""
        expenses = await self._session.execute(
            select(func.count(ExpenseEntry.id)).where(
                ExpenseEntry.supplier_id == supplier_id,
                ExpenseEntry.tenant_id == tenant_id,
                ExpenseEntry.voided_at.is_(None),
            )
        )
        movements = await self._session.execute(
            select(func.count(InventoryMovement.id)).where(
                InventoryMovement.supplier_id == supplier_id,
                InventoryMovement.tenant_id == tenant_id,
                InventoryMovement.voided_at.is_(None),
            )
        )
        return int(expenses.scalar_one() or 0) + int(movements.scalar_one() or 0)

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

    async def reactivate(self, supplier: Supplier) -> Supplier:
        """Revierte la baja: limpia ``deactivated_at``."""
        supplier.deactivated_at = None
        self._session.add(supplier)
        await self._session.flush()
        return supplier
