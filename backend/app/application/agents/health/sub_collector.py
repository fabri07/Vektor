"""AgentHealth — sub_collector.

Reúne el BusinessState necesario para calcular las 5 dimensiones del score v2.
Delega en compute_business_state (misma fuente que el pipeline Celery) para garantizar
que el agente y el scheduler producen exactamente los mismos números.
"""

from __future__ import annotations

from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.state.business_state_service import BusinessState, compute_business_state


async def collect(
    business_id: str,
    db: AsyncSession,
    redis: Redis,
) -> BusinessState:
    """Retorna el BusinessState del tenant. Fail-fast si no hay perfil de negocio."""
    return await compute_business_state(UUID(business_id), db, redis)
