"""Fábrica de sub-agentes. Usa lazy imports para evitar circulares."""

from __future__ import annotations

import uuid

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.base import BaseAgent


def get_sub_agent(
    name: str,
    db: AsyncSession | None = None,
    redis: Redis | None = None,
    user_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
) -> BaseAgent | None:
    if name == "agent_cash":
        from app.application.agents.cash.agent import AgentCash  # noqa: PLC0415

        gateway = None
        if db is not None and redis is not None and user_id is not None and tenant_id is not None:
            from app.integrations.google_workspace.gateway import GoogleWorkspaceGateway  # noqa: PLC0415

            gateway = GoogleWorkspaceGateway(db, redis, user_id, tenant_id)
        return AgentCash(db=db, redis=redis, gateway=gateway)
    if name == "agent_stock":
        from app.application.agents.stock.agent import AgentStock  # noqa: PLC0415

        return AgentStock()
    if name == "agent_supplier":
        from app.application.agents.supplier.agent import AgentSupplier  # noqa: PLC0415

        gateway = None
        if db is not None and redis is not None and user_id is not None and tenant_id is not None:
            from app.integrations.google_workspace.gateway import GoogleWorkspaceGateway  # noqa: PLC0415

            gateway = GoogleWorkspaceGateway(db, redis, user_id, tenant_id)
        return AgentSupplier(session=db, gateway=gateway)
    if name == "agent_health":
        from app.application.agents.health.agent import AgentHealth  # noqa: PLC0415

        return AgentHealth(db=db)
    if name == "agent_helper":
        from app.application.agents.helper.agent import AgentHelper  # noqa: PLC0415

        return AgentHelper()
    return None
