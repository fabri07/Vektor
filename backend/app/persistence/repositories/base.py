"""
Generic async repository base.
Every repository enforces tenant_id on all read/write operations.
"""

from typing import Generic, TypeVar
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.db.base import Base

ModelT = TypeVar("ModelT", bound=Base)


class BaseRepository(Generic[ModelT]):
    """
    Generic CRUD repository. Subclasses must set `model`.

    IMPORTANT: All query methods accept tenant_id to enforce data isolation.
    Never call these methods without a valid tenant_id.
    """

    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, id: UUID, tenant_id: UUID) -> ModelT | None:
        result = await self._session.execute(
            select(self.model).where(
                self.model.id == id,  # type: ignore[attr-defined]
                self.model.tenant_id == tenant_id,  # type: ignore[attr-defined]
            )
        )
        return result.scalar_one_or_none()

    async def list_by_tenant(
        self,
        tenant_id: UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ModelT]:
        result = await self._session.execute(
            select(self.model)
            .where(self.model.tenant_id == tenant_id)  # type: ignore[attr-defined]
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def save(self, entity: ModelT) -> ModelT:
        self._session.add(entity)
        await self._session.flush()
        return entity

    async def delete(self, entity: ModelT) -> None:
        await self._session.delete(entity)
        await self._session.flush()

    async def count_by_tenant(self, tenant_id: UUID) -> int:
        from sqlalchemy import func  # noqa: PLC0415

        result = await self._session.execute(
            select(func.count()).select_from(self.model).where(
                self.model.tenant_id == tenant_id  # type: ignore[attr-defined]
            )
        )
        return result.scalar_one()
