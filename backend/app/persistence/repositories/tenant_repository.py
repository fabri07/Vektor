"""Repository for Tenant and Subscription models."""

from uuid import UUID

from sqlalchemy import case, select
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
        """La suscripción que rige al tenant: la viva, o si no la última cancelada.

        Reemplaza al viejo `get_active_subscription` (que solo reconocía
        `status == "ACTIVE"`). El índice único parcial
        `uq_subscriptions_one_live_per_tenant` garantiza que hay como mucho
        UNA fila no cancelada por tenant, y esa gana siempre.

        Una `CANCELLED` NO se excluye: excluirla devolvía `None`, y los
        controles leían ese `None` como "sin restricción" — cancelar dejaba
        la cuenta sin gate y sin cupo. `effective_access()` ya sabe qué hacer
        con ella (vale hasta el fin del período pago, después bloquea).
        `None` queda para lo único que significa: el tenant no tiene ninguna.
        """
        es_cancelada = case(
            (Subscription.status == SubscriptionStatus.CANCELLED.value, 1), else_=0
        )
        result = await self._session.execute(
            select(Subscription)
            .where(Subscription.tenant_id == tenant_id)
            .order_by(es_cancelada, Subscription.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()
