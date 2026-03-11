"""Repository for User model."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.user import User


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, user_id: UUID, tenant_id: UUID) -> User | None:
        result = await self._session.execute(
            select(User).where(User.user_id == user_id, User.tenant_id == tenant_id)
        )
        return result.scalar_one_or_none()

    async def get_by_email(self, email: str, tenant_id: UUID) -> User | None:
        result = await self._session.execute(
            select(User).where(
                User.email == email.lower(),
                User.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_email_any_tenant(self, email: str) -> User | None:
        """Used during login — email must be unique globally per tenant."""
        result = await self._session.execute(
            select(User).where(User.email == email.lower())
        )
        return result.scalar_one_or_none()

    async def list_by_tenant(self, tenant_id: UUID) -> list[User]:
        result = await self._session.execute(
            select(User).where(User.tenant_id == tenant_id)
        )
        return list(result.scalars().all())

    async def save(self, user: User) -> User:
        self._session.add(user)
        await self._session.flush()
        return user
