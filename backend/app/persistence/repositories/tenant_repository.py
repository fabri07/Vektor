"""Repository for Tenant and Subscription models."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.subscription import SubscriptionStatus
from app.persistence.models.tenant import Subscription, Tenant


class TenantRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, tenant_id: UUID) -> Tenant | None:
        result = await self._session.execute(select(Tenant).where(Tenant.tenant_id == tenant_id))
        return result.scalar_one_or_none()

    async def save(self, tenant: Tenant) -> Tenant:
        self._session.add(tenant)
        await self._session.flush()
        return tenant

    async def get_current_subscription(self, tenant_id: UUID) -> Subscription | None:
        """La suscripción VIVA del tenant — cualquier estado salvo `CANCELLED`.

        Reemplaza al viejo `get_active_subscription` (que solo reconocía
        `status == "ACTIVE"` y por eso nunca encontraba una suscripción en
        `TRIAL`/`GRACE`/`READ_ONLY`). El índice único parcial
        `uq_subscriptions_one_live_per_tenant` garantiza que hay como mucho
        UNA fila así por tenant — `limit(1)` es solo defensivo.
        """
        result = await self._session.execute(
            select(Subscription)
            .where(
                Subscription.tenant_id == tenant_id,
                Subscription.status != SubscriptionStatus.CANCELLED.value,
            )
            .order_by(Subscription.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()
