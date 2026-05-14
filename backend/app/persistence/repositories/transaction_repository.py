"""Repository for SaleEntry and ExpenseEntry."""

from datetime import date
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.transaction import ExpenseEntry, SaleEntry


class SaleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, sale_id: UUID, tenant_id: UUID) -> SaleEntry | None:
        result = await self._session.execute(
            select(SaleEntry).where(
                SaleEntry.id == sale_id,
                SaleEntry.tenant_id == tenant_id,
                SaleEntry.voided_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def list_by_tenant(
        self,
        tenant_id: UUID,
        from_date: date | None = None,
        to_date: date | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[SaleEntry]:
        q = select(SaleEntry).where(
            SaleEntry.tenant_id == tenant_id,
            SaleEntry.voided_at.is_(None),
        )
        if from_date:
            q = q.where(SaleEntry.transaction_date >= from_date)
        if to_date:
            q = q.where(SaleEntry.transaction_date <= to_date)
        q = q.order_by(SaleEntry.transaction_date.desc()).limit(limit).offset(offset)
        result = await self._session.execute(q)
        return list(result.scalars().all())

    async def total_revenue(
        self,
        tenant_id: UUID,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> float:
        q = select(func.sum(SaleEntry.amount)).where(
            SaleEntry.tenant_id == tenant_id,
            SaleEntry.voided_at.is_(None),
        )
        if from_date:
            q = q.where(SaleEntry.transaction_date >= from_date)
        if to_date:
            q = q.where(SaleEntry.transaction_date <= to_date)
        result = await self._session.execute(q)
        return float(result.scalar_one() or 0)

    async def count_by_date_range(
        self,
        tenant_id: UUID,
        from_date: date,
        to_date: date,
    ) -> int:
        q = select(func.count(SaleEntry.id)).where(
            SaleEntry.tenant_id == tenant_id,
            SaleEntry.voided_at.is_(None),
            SaleEntry.transaction_date >= from_date,
            SaleEntry.transaction_date <= to_date,
        )
        result = await self._session.execute(q)
        return int(result.scalar_one() or 0)

    async def cash_breakdown_by_method(
        self,
        tenant_id: UUID,
        from_date: date,
        to_date: date,
    ) -> list[dict]:
        """Ventas diarias agrupadas por método de pago."""
        q = (
            select(
                SaleEntry.transaction_date,
                SaleEntry.payment_method,
                func.sum(SaleEntry.amount).label("total"),
            )
            .where(
                SaleEntry.tenant_id == tenant_id,
                SaleEntry.voided_at.is_(None),
                SaleEntry.transaction_date >= from_date,
                SaleEntry.transaction_date <= to_date,
            )
            .group_by(SaleEntry.transaction_date, SaleEntry.payment_method)
            .order_by(SaleEntry.transaction_date)
        )
        result = await self._session.execute(q)
        return [
            {
                "date": str(row.transaction_date),
                "payment_method": row.payment_method or "OTROS",
                "total": float(row.total or 0),
            }
            for row in result.all()
        ]

    async def save(self, entry: SaleEntry) -> SaleEntry:
        self._session.add(entry)
        await self._session.flush()
        return entry

    async def bulk_save(self, entries: list[SaleEntry]) -> list[SaleEntry]:
        self._session.add_all(entries)
        await self._session.flush()
        return entries

    async def delete(self, entry: SaleEntry) -> None:
        await self._session.delete(entry)
        await self._session.flush()


class ExpenseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, expense_id: UUID, tenant_id: UUID) -> ExpenseEntry | None:
        result = await self._session.execute(
            select(ExpenseEntry).where(
                ExpenseEntry.id == expense_id,
                ExpenseEntry.tenant_id == tenant_id,
                ExpenseEntry.voided_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def list_by_tenant(
        self,
        tenant_id: UUID,
        from_date: date | None = None,
        to_date: date | None = None,
        category: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ExpenseEntry]:
        q = select(ExpenseEntry).where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.voided_at.is_(None),
        )
        if from_date:
            q = q.where(ExpenseEntry.transaction_date >= from_date)
        if to_date:
            q = q.where(ExpenseEntry.transaction_date <= to_date)
        if category:
            q = q.where(ExpenseEntry.category == category)
        q = q.order_by(ExpenseEntry.transaction_date.desc()).limit(limit).offset(offset)
        result = await self._session.execute(q)
        return list(result.scalars().all())

    async def total_expenses(
        self,
        tenant_id: UUID,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> float:
        q = select(func.sum(ExpenseEntry.amount)).where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.voided_at.is_(None),
        )
        if from_date:
            q = q.where(ExpenseEntry.transaction_date >= from_date)
        if to_date:
            q = q.where(ExpenseEntry.transaction_date <= to_date)
        result = await self._session.execute(q)
        return float(result.scalar_one() or 0)

    async def count_by_date_range(
        self,
        tenant_id: UUID,
        from_date: date,
        to_date: date,
    ) -> int:
        q = select(func.count(ExpenseEntry.id)).where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.voided_at.is_(None),
            ExpenseEntry.transaction_date >= from_date,
            ExpenseEntry.transaction_date <= to_date,
        )
        result = await self._session.execute(q)
        return int(result.scalar_one() or 0)

    async def expenses_by_category(
        self,
        tenant_id: UUID,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> list[dict]:
        """Gastos agrupados por categoría con totales y porcentaje."""
        q = (
            select(ExpenseEntry.category, func.sum(ExpenseEntry.amount).label("total"))
            .where(
                ExpenseEntry.tenant_id == tenant_id,
                ExpenseEntry.voided_at.is_(None),
            )
        )
        if from_date:
            q = q.where(ExpenseEntry.transaction_date >= from_date)
        if to_date:
            q = q.where(ExpenseEntry.transaction_date <= to_date)
        q = q.group_by(ExpenseEntry.category).order_by(func.sum(ExpenseEntry.amount).desc())
        result = await self._session.execute(q)
        rows = result.all()
        grand_total = sum(float(r.total or 0) for r in rows)
        return [
            {
                "category": r.category or "OTHER",
                "total": float(r.total or 0),
                "pct": (
                    round(float(r.total or 0) / grand_total * 100, 1)
                    if grand_total > 0
                    else 0.0
                ),
            }
            for r in rows
        ]

    async def top_suppliers(
        self,
        tenant_id: UUID,
        from_date: date | None = None,
        to_date: date | None = None,
        limit: int = 5,
    ) -> list[dict]:
        """Top proveedores por gasto total."""
        q = (
            select(ExpenseEntry.supplier_name, func.sum(ExpenseEntry.amount).label("total"))
            .where(
                ExpenseEntry.tenant_id == tenant_id,
                ExpenseEntry.voided_at.is_(None),
                ExpenseEntry.supplier_name.isnot(None),
                ExpenseEntry.supplier_name != "",
            )
        )
        if from_date:
            q = q.where(ExpenseEntry.transaction_date >= from_date)
        if to_date:
            q = q.where(ExpenseEntry.transaction_date <= to_date)
        q = (
            q.group_by(ExpenseEntry.supplier_name)
            .order_by(func.sum(ExpenseEntry.amount).desc())
            .limit(limit)
        )
        result = await self._session.execute(q)
        rows = result.all()

        total_q = select(func.sum(ExpenseEntry.amount)).where(
            ExpenseEntry.tenant_id == tenant_id,
            ExpenseEntry.voided_at.is_(None),
            ExpenseEntry.supplier_name.isnot(None),
            ExpenseEntry.supplier_name != "",
        )
        if from_date:
            total_q = total_q.where(ExpenseEntry.transaction_date >= from_date)
        if to_date:
            total_q = total_q.where(ExpenseEntry.transaction_date <= to_date)
        total_result = await self._session.execute(total_q)
        grand_total = float(total_result.scalar_one() or 0)

        return [
            {
                "supplier_name": r.supplier_name,
                "total": float(r.total or 0),
                "pct": (
                    round(float(r.total or 0) / grand_total * 100, 1)
                    if grand_total > 0
                    else 0.0
                ),
            }
            for r in rows
        ]

    async def cash_breakdown_by_method(
        self,
        tenant_id: UUID,
        from_date: date,
        to_date: date,
    ) -> list[dict]:
        """Gastos diarios agrupados por método de pago."""
        q = (
            select(
                ExpenseEntry.transaction_date,
                ExpenseEntry.payment_method,
                func.sum(ExpenseEntry.amount).label("total"),
            )
            .where(
                ExpenseEntry.tenant_id == tenant_id,
                ExpenseEntry.voided_at.is_(None),
                ExpenseEntry.transaction_date >= from_date,
                ExpenseEntry.transaction_date <= to_date,
            )
            .group_by(ExpenseEntry.transaction_date, ExpenseEntry.payment_method)
            .order_by(ExpenseEntry.transaction_date)
        )
        result = await self._session.execute(q)
        return [
            {
                "date": str(row.transaction_date),
                "payment_method": row.payment_method or "OTROS",
                "total": float(row.total or 0),
            }
            for row in result.all()
        ]

    async def save(self, entry: ExpenseEntry) -> ExpenseEntry:
        self._session.add(entry)
        await self._session.flush()
        return entry

    async def delete(self, entry: ExpenseEntry) -> None:
        await self._session.delete(entry)
        await self._session.flush()
