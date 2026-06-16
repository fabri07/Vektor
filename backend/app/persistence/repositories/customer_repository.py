"""Repository for Customer queries. Always filters by tenant_id."""

from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.customer import Customer
from app.persistence.models.transaction import SaleEntry


class CustomerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, customer_id: UUID, tenant_id: UUID) -> Customer | None:
        result = await self._session.execute(
            select(Customer).where(
                Customer.id == customer_id,
                Customer.tenant_id == tenant_id,
                Customer.deactivated_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def list_by_tenant(
        self,
        tenant_id: UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Customer]:
        q = (
            select(Customer)
            .where(
                Customer.tenant_id == tenant_id,
                Customer.deactivated_at.is_(None),
            )
            .order_by(Customer.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(q)
        return list(result.scalars().all())

    async def get_inactive_customers(
        self,
        tenant_id: UUID,
        days: int = 60,
    ) -> list[dict[str, Any]]:
        """Clientes activos sin ventas (no anuladas) en los últimos `days` días.

        Subquery: última venta no anulada por cliente. Un cliente activo es inactivo
        si su última venta es anterior al corte, o si nunca tuvo ventas. Solo clientes
        no desactivados. Ordenados por última venta más antigua primero (None primero:
        los que nunca compraron). El ranking lo arma `shared/analytics.py`.

        Returns list[dict]: customer_id, customer_name, last_sale_date (date | None).
        """
        cutoff = date.today() - timedelta(days=days)
        last_sale_sq = (
            select(
                SaleEntry.customer_id.label("customer_id"),
                func.max(SaleEntry.transaction_date).label("last_sale"),
            )
            .where(
                SaleEntry.tenant_id == tenant_id,
                SaleEntry.voided_at.is_(None),
                SaleEntry.customer_id.isnot(None),
            )
            .group_by(SaleEntry.customer_id)
            .subquery()
        )
        q = (
            select(
                Customer.id,
                Customer.name,
                last_sale_sq.c.last_sale,
            )
            .outerjoin(last_sale_sq, last_sale_sq.c.customer_id == Customer.id)
            .where(
                Customer.tenant_id == tenant_id,
                Customer.deactivated_at.is_(None),
            )
        )
        result = await self._session.execute(q)
        out: list[dict[str, Any]] = []
        for row in result.all():
            last_sale = row.last_sale
            last_date: date | None = (
                last_sale.date() if isinstance(last_sale, datetime) else last_sale
            )
            if last_date is not None and last_date >= cutoff:
                continue  # compró recientemente → activo, no inactivo
            out.append(
                {
                    "customer_id": str(row.id),
                    "customer_name": row.name,
                    "last_sale_date": last_date,
                }
            )
        # None (nunca compró) primero, luego por fecha ascendente (más viejo arriba).
        out.sort(key=lambda c: (c["last_sale_date"] is not None, c["last_sale_date"] or date.min))
        return out

    async def count_active(self, tenant_id: UUID) -> int:
        """Cantidad de clientes activos (no desactivados) del tenant."""
        q = select(func.count(Customer.id)).where(
            Customer.tenant_id == tenant_id,
            Customer.deactivated_at.is_(None),
        )
        result = await self._session.execute(q)
        return int(result.scalar_one() or 0)

    async def save(self, customer: Customer) -> Customer:
        self._session.add(customer)
        await self._session.flush()
        return customer

    async def soft_delete(self, customer: Customer) -> Customer:
        """Marca el cliente como desactivado (soft-delete). No borra la fila."""
        customer.deactivated_at = datetime.now(UTC)
        self._session.add(customer)
        await self._session.flush()
        return customer
