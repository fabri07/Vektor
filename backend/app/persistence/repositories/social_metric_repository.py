"""Repository for SocialMetric queries. Always filters by tenant_id."""

from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.social_metric import SocialMetric


class SocialMetricRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, metric_id: UUID, tenant_id: UUID) -> SocialMetric | None:
        result = await self._session.execute(
            select(SocialMetric).where(
                SocialMetric.id == metric_id,
                SocialMetric.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_by_tenant(
        self,
        tenant_id: UUID,
        *,
        platform: str | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> list[SocialMetric]:
        q = select(SocialMetric).where(SocialMetric.tenant_id == tenant_id)
        if platform is not None:
            q = q.where(SocialMetric.platform == platform)
        if from_date is not None:
            q = q.where(SocialMetric.metric_date >= from_date)
        if to_date is not None:
            q = q.where(SocialMetric.metric_date <= to_date)
        q = q.order_by(SocialMetric.metric_date.desc(), SocialMetric.created_at.desc())
        result = await self._session.execute(q)
        return list(result.scalars().all())

    async def save(self, metric: SocialMetric) -> SocialMetric:
        self._session.add(metric)
        await self._session.flush()
        return metric

    async def delete(self, metric: SocialMetric) -> None:
        await self._session.delete(metric)
        await self._session.flush()
