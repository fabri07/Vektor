"""Fábrica de sub-agentes. Usa lazy imports para evitar circulares."""

from __future__ import annotations

from app.application.agents.base import BaseAgent


def get_sub_agent(
    name: str,
    db=None,
    redis=None,
    user_id=None,
    tenant_id=None,
) -> BaseAgent | None:
    if name == "agent_cash":
        from app.application.agents.cash.agent import AgentCash  # noqa: PLC0415

        return AgentCash(db=db, redis=redis)
    if name == "agent_stock":
        from app.application.agents.stock.agent import AgentStock  # noqa: PLC0415

        return AgentStock(db=db)
    if name == "agent_supplier":
        from app.application.agents.supplier.agent import AgentSupplier  # noqa: PLC0415
        from app.config.settings import get_settings  # noqa: PLC0415
        from app.integrations.mcp.http_gateway import HttpMcpGateway  # noqa: PLC0415

        settings = get_settings()
        gateway = (
            HttpMcpGateway(settings=settings, user_id=str(user_id) if user_id else None)
            if settings.ENABLE_GOOGLE_MCP_TOOLS and settings.MCP_SERVER_URL
            else None
        )
        return AgentSupplier(session=db, gateway=gateway)
    if name == "agent_calendar":
        from app.application.agents.calendar.agent import AgentCalendar  # noqa: PLC0415
        from app.config.settings import get_settings  # noqa: PLC0415
        from app.integrations.mcp.http_gateway import HttpMcpGateway  # noqa: PLC0415

        settings = get_settings()
        gateway = (
            HttpMcpGateway(settings=settings, user_id=str(user_id) if user_id else None)
            if settings.ENABLE_GOOGLE_MCP_TOOLS and settings.MCP_SERVER_URL
            else None
        )
        return AgentCalendar(gateway=gateway, tenant_id=str(tenant_id) if tenant_id else None)
    if name == "agent_sync":
        from app.application.agents.sync.agent import AgentSync  # noqa: PLC0415
        from app.config.settings import get_settings  # noqa: PLC0415
        from app.integrations.mcp.http_gateway import HttpMcpGateway  # noqa: PLC0415

        settings = get_settings()
        gateway = (
            HttpMcpGateway(settings=settings, user_id=str(user_id) if user_id else None)
            if settings.ENABLE_GOOGLE_MCP_TOOLS and settings.MCP_SERVER_URL
            else None
        )
        return AgentSync(gateway=gateway, tenant_id=str(tenant_id) if tenant_id else None)
    if name == "agent_health":
        from app.application.agents.health.agent import AgentHealth  # noqa: PLC0415

        return AgentHealth(db=db)
    if name == "agent_helper":
        from app.application.agents.helper.agent import AgentHelper  # noqa: PLC0415

        return AgentHelper()
    return None
