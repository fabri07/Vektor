"""Fábrica de sub-agentes. Usa lazy imports para evitar circulares.

Alias deprecado (Stage 2a):
  agent_cash → AgentIncome  (protege PendingActions en vuelo con target_agent viejo)

Stage 5d completado: AgentCalendar y AgentSync eliminados; su lógica fue absorbida
por AgentGoogle. Los aliases agent_calendar y agent_sync ya no existen.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from app.application.agents.base import BaseAgent
from app.observability.logger import get_logger

if TYPE_CHECKING:
    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.config.settings import Settings
    from app.integrations.mcp.http_gateway import HttpMcpGateway

logger = get_logger(__name__)


def _make_gateway(settings: Settings, user_id: Any | None) -> HttpMcpGateway | None:
    """Helper para construir HttpMcpGateway condicionalmente."""
    from app.integrations.mcp.http_gateway import HttpMcpGateway  # noqa: PLC0415

    return (
        HttpMcpGateway(settings=settings, user_id=str(user_id) if user_id else None)
        if settings.ENABLE_GOOGLE_MCP_TOOLS and settings.MCP_SERVER_URL
        else None
    )


def get_sub_agent(
    name: str,
    db: AsyncSession | None = None,
    redis: Redis | None = None,
    user_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
) -> BaseAgent | None:
    # ── Agentes nuevos (Stage 2) ──────────────────────────────────────────────
    if name == "agent_income":
        from app.application.agents.income.agent import AgentIncome  # noqa: PLC0415

        return AgentIncome(db=db, redis=redis)

    if name == "agent_expense":
        from app.application.agents.expense.agent import AgentExpense  # noqa: PLC0415

        return AgentExpense(db=db, redis=redis)

    if name == "agent_google":
        from app.application.agents.google.agent import AgentGoogle  # noqa: PLC0415
        from app.config.settings import get_settings  # noqa: PLC0415

        settings = get_settings()
        gateway = _make_gateway(settings, user_id)
        return AgentGoogle(gateway=gateway, tenant_id=str(tenant_id) if tenant_id else None)

    # ── Agentes existentes ────────────────────────────────────────────────────
    if name == "agent_stock":
        from app.application.agents.stock.agent import AgentStock  # noqa: PLC0415

        return AgentStock(db=db)

    if name == "agent_supplier":
        from app.application.agents.supplier.agent import AgentSupplier  # noqa: PLC0415
        from app.config.settings import get_settings  # noqa: PLC0415

        settings = get_settings()
        gateway = _make_gateway(settings, user_id)
        return AgentSupplier(session=db, gateway=gateway)

    if name == "agent_health":
        from app.application.agents.health.agent import AgentHealth  # noqa: PLC0415

        return AgentHealth(db=db)

    if name == "agent_helper":
        from app.application.agents.helper.agent import AgentHelper  # noqa: PLC0415

        return AgentHelper()

    if name == "agent_chat":
        from app.application.agents.chat.agent import AgentChat  # noqa: PLC0415

        return AgentChat()

    if name == "agent_client":
        from app.application.agents.client.agent import AgentClient  # noqa: PLC0415

        return AgentClient(db=db)

    # ── Alias deprecado (Stage 2a) ────────────────────────────────────────────
    if name == "agent_cash":
        logger.warning(
            "registry_deprecated_alias",
            alias="agent_cash",
            redirect="agent_income",
        )
        from app.application.agents.income.agent import AgentIncome  # noqa: PLC0415

        return AgentIncome(db=db, redis=redis)

    return None
