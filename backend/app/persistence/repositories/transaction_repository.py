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
                SaleEntry.id == sale_id, SaleEntry.tenant_id == tenant_id
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
        q = select(SaleEntry).where(SaleEntry.tenant_id == tenant_id)
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
        q = select(func.sum(SaleEntry.amount)).where(SaleEntry.tenant_id == tenant_id)
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
            SaleEntry.transaction_date >= from_date,
            SaleEntry.transaction_date <= to_date,
        )
        result = await self._session.execute(q)
        return int(result.scalar_one() or 0)

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
                ExpenseEntry.id == expense_id, ExpenseEntry.tenant_id == tenant_id
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
        q = select(ExpenseEntry).where(ExpenseEntry.tenant_id == tenant_id)
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
        q = select(func.sum(ExpenseEntry.amount)).where(ExpenseEntry.tenant_id == tenant_id)
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
            ExpenseEntry.transaction_date >= from_date,
            ExpenseEntry.transaction_date <= to_date,
        )
        result = await self._session.execute(q)
        return int(result.scalar_one() or 0)

    async def save(self, entry: ExpenseEntry) -> ExpenseEntry:
        self._session.add(entry)
        await self._session.flush()
        return entry

    async def delete(self, entry: ExpenseEntry) -> None:
        await self._session.delete(entry)
        await self._session.flush()
