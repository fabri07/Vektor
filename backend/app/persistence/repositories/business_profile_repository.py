"""Repository for BusinessProfile and BusinessSnapshot."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.business import BusinessProfile, BusinessSnapshot


class BusinessProfileRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_tenant_id(self, tenant_id: UUID) -> BusinessProfile | None:
        result = await self.session.execute(
            select(BusinessProfile).where(BusinessProfile.tenant_id == tenant_id)
        )
        return result.scalar_one_or_none()

    async def save(self, bp: BusinessProfile) -> BusinessProfile:
        self.session.add(bp)
        await self.session.flush()
        return bp

    async def create_snapshot(self, snapshot: BusinessSnapshot) -> BusinessSnapshot:
        self.session.add(snapshot)
        await self.session.flush()
        return snapshot

    async def get_latest_snapshot(self, tenant_id: UUID) -> BusinessSnapshot | None:
        result = await self.session.execute(
            select(BusinessSnapshot)
            .where(BusinessSnapshot.tenant_id == tenant_id)
            .order_by(BusinessSnapshot.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()
