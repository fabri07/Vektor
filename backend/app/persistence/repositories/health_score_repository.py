"""Repository for HealthScoreSnapshot and WeeklyScoreHistory."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.score import HealthScoreSnapshot, WeeklyScoreHistory


class HealthScoreRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_latest(self, tenant_id: UUID) -> HealthScoreSnapshot | None:
        result = await self._session.execute(
            select(HealthScoreSnapshot)
            .where(HealthScoreSnapshot.tenant_id == tenant_id)
            .order_by(HealthScoreSnapshot.snapshot_date.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_by_tenant(
        self,
        tenant_id: UUID,
        since: datetime | None = None,
        limit: int = 30,
    ) -> list[HealthScoreSnapshot]:
        q = select(HealthScoreSnapshot).where(
            HealthScoreSnapshot.tenant_id == tenant_id
        )
        if since:
            q = q.where(HealthScoreSnapshot.snapshot_date >= since)
        q = q.order_by(HealthScoreSnapshot.snapshot_date.desc()).limit(limit)
        result = await self._session.execute(q)
        return list(result.scalars().all())

    async def save(self, snapshot: HealthScoreSnapshot) -> HealthScoreSnapshot:
        self._session.add(snapshot)
        await self._session.flush()
        return snapshot

    async def get_weekly_history(
        self, tenant_id: UUID, limit: int = 12
    ) -> list[WeeklyScoreHistory]:
        result = await self._session.execute(
            select(WeeklyScoreHistory)
            .where(WeeklyScoreHistory.tenant_id == tenant_id)
            .order_by(WeeklyScoreHistory.week_start.desc())
            .limit(limit)
        )
        return list(result.scalars().all())
