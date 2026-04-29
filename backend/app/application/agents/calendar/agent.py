"""AgentCalendar — creación y consulta de eventos Google Calendar via MCP."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import anthropic

from app.application.agents.base import BaseAgent
from app.application.agents.shared.schemas import ActionType, AgentRequest, AgentResponse, Confidence, LLMCall, RiskLevel, UsageSummary
from app.application.security.prompt_defense import wrap_user_input
from app.integrations.anthropic_client import get_anthropic_async_client
from app.observability.logger import get_logger

if TYPE_CHECKING:
    from app.application.ports.mcp_gateway import McpToolGateway

logger = get_logger(__name__)


class AgentCalendar(BaseAgent):
    agent_name = "agent_calendar"

    def __init__(self, gateway: "McpToolGateway | None" = None) -> None:
        self._gateway = gateway
        self._client: Any | None = None

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = get_anthropic_async_client(anthropic.AsyncAnthropic)
        return self._client

    async def process(self, request: AgentRequest) -> AgentResponse:
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

        message = request.message.lower()

        if self._is_query(message):
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_approval",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.HIGH,
                requires_approval=True,
                result={
                    "action_type": ActionType.CREATE_CALENDAR_EVENT,
                    "summary": "Consultar eventos próximos en Google Calendar.",
                    "mode": "mcp",
                    "payload": {
                        "query_only": True,
                        "mode": "mcp",
                        "raw_message": request.message,
                    },
                },
            )

        extracted, cal_call = await self._extract_event_data(request.message)
        usage = UsageSummary(calls=[cal_call]) if cal_call else None

        if not extracted.get("has_enough_info"):
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
                usage=usage,
            )

        summary_text = extracted.get("summary", "Evento")
        start = extracted.get("start_datetime", "")
        end = extracted.get("end_datetime", "")
        summary_display = f"Crear evento: {summary_text}\nFecha: {start}"
        if end:
            summary_display += f" → {end}"

        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="requires_approval",
            risk_level=RiskLevel.MEDIUM,
            confidence=Confidence.HIGH,
            requires_approval=True,
            result={
                "action_type": ActionType.CREATE_CALENDAR_EVENT,
                "summary": summary_display,
                "mode": "mcp",
                "payload": {
                    "summary": summary_text,
                    "start_datetime": start,
                    "end_datetime": end,
                    "attendees": extracted.get("attendees", []),
                    "description": extracted.get("description", ""),
                    "mode": "mcp",
                    "raw_message": request.message,
                },
            },
            usage=usage,
        )

    def _is_query(self, message: str) -> bool:
        query_keywords = (
            "ver agenda", "mis eventos", "qué tengo", "que tengo", "próximos eventos",
            "proximos eventos", "consultar calendario", "ver calendario", "agenda de",
        )
        return any(kw in message for kw in query_keywords)

    async def _extract_event_data(self, message: str) -> tuple[dict, LLMCall | None]:
        """Usa LLM para extraer datos del evento desde lenguaje natural."""
        from datetime import date  # noqa: PLC0415
        today = date.today().isoformat()

        system = (
            f"Hoy es {today}. Sos el asistente de Véktor.\n"
            "Extraé datos de un evento de Google Calendar desde el mensaje del usuario.\n\n"
            "Retorná SOLO un JSON:\n"
            "{\n"
            '  "has_enough_info": true|false,\n'
            '  "summary": "título del evento",\n'
            '  "start_datetime": "ISO 8601, ej: 2026-04-28T10:00:00",\n'
            '  "end_datetime": "ISO 8601 o null",\n'
            '  "attendees": ["email1@example.com"] o [],\n'
            '  "description": "descripción adicional o null"\n'
            "}\n\n"
            "has_enough_info=true si hay al menos título y fecha/hora.\n"
            "Inferí el año/mes del contexto. Si solo dice 'mañana', calculalo desde hoy.\n"
            "Si falta la hora, usá 09:00:00. Si falta la duración, asumí 1 hora."
        )
        try:
            response = await self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=400,
                system=system,
                messages=[{"role": "user", "content": wrap_user_input(message)}],
            )
            llm_call = LLMCall(
                source="agent_calendar",
                model="claude-haiku-4-5-20251001",
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
            )
            raw = response.content[0].text.strip() if response.content else ""
            return json.loads(raw), llm_call
        except Exception as exc:
            logger.warning("agent_calendar.extract_failed", error=str(exc))
            return {"has_enough_info": False}, None
