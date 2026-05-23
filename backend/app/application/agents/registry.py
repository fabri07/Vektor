"""Fábrica de sub-agentes. Usa lazy imports para evitar circulares.

Aliases deprecados (Stage 2a → cleanup en Stage 5d):
  agent_cash      → AgentIncome  (protege PendingActions en vuelo con target_agent viejo)
  agent_calendar  → AgentGoogle
  agent_sync      → AgentGoogle
"""

from __future__ import annotations

from app.application.agents.base import BaseAgent
from app.observability.logger import get_logger

logger = get_logger(__name__)


def _make_gateway(settings, user_id):
    """Helper para construir HttpMcpGateway condicionalmente."""
    from app.integrations.mcp.http_gateway import HttpMcpGateway  # noqa: PLC0415

    return (
        HttpMcpGateway(settings=settings, user_id=str(user_id) if user_id else None)
        if settings.ENABLE_GOOGLE_MCP_TOOLS and settings.MCP_SERVER_URL
        else None
    )


def get_sub_agent(
    name: str,
    db=None,
    redis=None,
    user_id=None,
    tenant_id=None,
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

    # ── Aliases deprecados (Stage 2a → cleanup Stage 5d) ─────────────────────
    if name == "agent_cash":
        logger.warning(
            "registry_deprecated_alias",
            alias="agent_cash",
            redirect="agent_income",
        )
        from app.application.agents.income.agent import AgentIncome  # noqa: PLC0415

        return AgentIncome(db=db, redis=redis)

    if name == "agent_calendar":
        logger.warning(
            "registry_deprecated_alias",
            alias="agent_calendar",
            redirect="agent_google",
        )
        from app.application.agents.google.agent import AgentGoogle  # noqa: PLC0415
        from app.config.settings import get_settings  # noqa: PLC0415

        settings = get_settings()
        gateway = _make_gateway(settings, user_id)
        return AgentGoogle(gateway=gateway, tenant_id=str(tenant_id) if tenant_id else None)

    if name == "agent_sync":
        logger.warning(
            "registry_deprecated_alias",
            alias="agent_sync",
            redirect="agent_google",
        )
        from app.application.agents.google.agent import AgentGoogle  # noqa: PLC0415
        from app.config.settings import get_settings  # noqa: PLC0415

        settings = get_settings()
        gateway = _make_gateway(settings, user_id)
        return AgentGoogle(gateway=gateway, tenant_id=str(tenant_id) if tenant_id else None)

    return None
