"""AgentGoogle — broker de herramientas Google (Stage 2a: merge de AgentCalendar + AgentSync).

Responsabilidades:
- Ejecutar acciones Google Calendar (CREATE_CALENDAR_EVENT)
- Ejecutar acciones Google Sheets/Drive/Docs (SYNC_TO_GOOGLE)
- No toma decisiones de negocio — solo ejecuta acciones externas

Dispatch interno:
- ActionType.CREATE_CALENDAR_EVENT → delega a AgentCalendar
- ActionType.SYNC_TO_GOOGLE y todo lo demás → delega a AgentSync
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from app.application.agents.base import BaseAgent
from app.application.agents.shared.schemas import ActionType, AgentRequest, AgentResponse

if TYPE_CHECKING:
    from app.application.ports.mcp_gateway import McpToolGateway


class AgentGoogle(BaseAgent):
    agent_name = "agent_google"

    def __init__(
        self,
        gateway: Optional["McpToolGateway"] = None,
        tenant_id: str | None = None,
    ) -> None:
        self._gateway = gateway
        self._tenant_id = tenant_id

    async def process(  # type: ignore[override]
        self,
        request: AgentRequest,
        task: Any | None = None,
    ) -> AgentResponse:
        action_type = getattr(task, "action_type", None) if task else None

        if action_type == ActionType.CREATE_CALENDAR_EVENT:
            return await self._dispatch_calendar(request)
        # Default: sync / sheets / docs / drive
        return await self._dispatch_sync(request)

    async def _dispatch_calendar(self, request: AgentRequest) -> AgentResponse:
        from app.application.agents.calendar.agent import AgentCalendar  # noqa: PLC0415

        delegate = AgentCalendar(gateway=self._gateway, tenant_id=self._tenant_id)
        response = await delegate.process(request)
        response.agent_name = self.agent_name
        return response

    async def _dispatch_sync(self, request: AgentRequest) -> AgentResponse:
        from app.application.agents.sync.agent import AgentSync  # noqa: PLC0415

        delegate = AgentSync(gateway=self._gateway, tenant_id=self._tenant_id)
        response = await delegate.process(request)
        response.agent_name = self.agent_name
        return response
