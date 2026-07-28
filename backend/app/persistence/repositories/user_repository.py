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
        """El usuario con ese email, mirando todos los tenants (o ``None``).

        ``users.email`` NO es único global: el único unique es
        ``uq_users_tenant_email`` (por tenant), y ``POST /users`` valida la
        unicidad per-tenant, así que un OWNER puede dar de alta una sub-cuenta
        con un email que ya es OWNER de otro tenant. Con ``scalar_one_or_none()``
        ese caso levantaba ``MultipleResultsFound`` → 500. En el formulario
        público de solicitud de acceso eso sería un oráculo de enumeración
        (500 para ese email, 201 para todos los demás), que es exactamente lo
        que ese flujo existe para evitar. ``limit(1).first()``: la pregunta es
        "¿este email ya tiene cuenta?", y con dos filas la respuesta sigue
        siendo sí.
        """
        result = await self._session.execute(
            select(User).where(User.email == email.lower()).limit(1)
        )
        return result.scalars().first()

    async def list_by_tenant(self, tenant_id: UUID) -> list[User]:
        result = await self._session.execute(select(User).where(User.tenant_id == tenant_id))
        return list(result.scalars().all())

    async def save(self, user: User) -> User:
        self._session.add(user)
        await self._session.flush()
        return user
