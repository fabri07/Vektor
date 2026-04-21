"""AgentCalendar — creación y consulta de eventos Google Calendar via MCP."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.application.agents.base import BaseAgent
from app.application.agents.shared.schemas import ActionType, AgentRequest, AgentResponse, Confidence, RiskLevel
from app.observability.logger import get_logger

if TYPE_CHECKING:
    from app.application.ports.mcp_gateway import McpToolGateway

logger = get_logger(__name__)


class AgentCalendar(BaseAgent):
    agent_name = "agent_calendar"

    def __init__(self, gateway: "McpToolGateway | None" = None) -> None:
        self._gateway = gateway

    async def process(self, request: AgentRequest) -> AgentResponse:
        message = request.message.lower()

        if self._gateway is None:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_google_auth",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.HIGH,
                result={
                    "action_type": ActionType.CREATE_CALENDAR_EVENT,
                    "mode": "informational",
                    "message": "Necesito que la integración de Google Calendar esté disponible para crear o consultar eventos.",
                },
            )

        if self._is_query(message):
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="success",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.HIGH,
                result={
                    "action_type": ActionType.GENERATE_HEALTH_REPORT,
                    "summary": "Consulta de calendario — sin datos disponibles sin conexión MCP.",
                    "mode": "mcp" if self._gateway else "informational",
                },
            )

        extracted = self._extract_event_data(request.message)
        if not extracted.get("summary"):
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.LOW,
                question=(
                    "Para crear el evento necesito:\n"
                    "- Título del evento\n"
                    "- Fecha y hora de inicio\n"
                    "- Duración o hora de fin\n"
                    "¿Podés darme esos datos?"
                ),
                result={"summary": "Faltan datos del evento de calendario."},
            )

        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="requires_approval",
            risk_level=RiskLevel.MEDIUM,
            confidence=Confidence.HIGH,
            requires_approval=True,
            result={
                "action_type": ActionType.CREATE_CALENDAR_EVENT,
                "summary": f"Crear evento: {extracted.get('summary')}",
                "mode": "mcp" if self._gateway else "informational",
                "payload": extracted,
            },
        )

    def _is_query(self, message: str) -> bool:
        query_keywords = ("ver agenda", "mis eventos", "qué tengo", "próximos eventos", "consultar calendario", "calendar")
        return any(kw in message for kw in query_keywords)

    def _extract_event_data(self, message: str) -> dict:
        """Extracción básica de keywords del mensaje. En producción un Haiku hace esto."""
        data: dict = {}
        msg_lower = message.lower()

        # Título básico
        for kw in ("reunión", "reunion", "meeting", "evento", "cita", "entrega", "llamada"):
            if kw in msg_lower:
                idx = msg_lower.find(kw)
                data["summary"] = message[idx: idx + 60].strip()
                break

        data["raw_message"] = message
        return data
