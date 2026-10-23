"""Repository para CashClose (cierre de caja diario). Sprint 20."""

from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.cash_close import CashClose


class CashCloseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_date(self, tenant_id: UUID, close_date: date) -> CashClose | None:
        result = await self._session.execute(
            select(CashClose).where(
                CashClose.tenant_id == tenant_id,
                CashClose.close_date == close_date,
            )
        )
        return result.scalar_one_or_none()

    async def list_by_range(
        self, tenant_id: UUID, from_date: date, to_date: date
    ) -> list[CashClose]:
        result = await self._session.execute(
            select(CashClose)
            .where(
                CashClose.tenant_id == tenant_id,
                CashClose.close_date >= from_date,
                CashClose.close_date <= to_date,
            )
            .order_by(CashClose.close_date.desc())
        )
        return list(result.scalars().all())

    async def save(self, entry: CashClose) -> CashClose:
        self._session.add(entry)
        await self._session.flush()
        return entry
