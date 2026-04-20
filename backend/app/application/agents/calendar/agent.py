"""AgentCalendar — gestión de agenda del dueño del negocio vía Google Calendar."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from app.application.agents.base import BaseAgent
from app.application.agents.shared.schemas import (
    ActionType,
    AgentRequest,
    AgentResponse,
    Confidence,
    RiskLevel,
)
from app.application.security.prompt_defense import wrap_user_input
from app.integrations.anthropic_client import get_anthropic_async_client
from app.observability.logger import get_logger

if TYPE_CHECKING:
    from app.integrations.google_workspace.gateway import GoogleWorkspaceGateway

logger = get_logger(__name__)


class AgentCalendar(BaseAgent):
    """Agente para consultar y crear eventos en Google Calendar.

    Flujo:
      1. Si Google Workspace no está conectado → retorna requires_google_auth con URL.
      2. Clasifica el intent del mensaje (LLM Haiku): schedule_event vs review_calendar.
      3. schedule_event → requires_approval (MEDIUM) con datos estructurados del evento.
      4. review_calendar → llama calendar_get_agenda y retorna lista de eventos.
    """

    agent_name = "agent_calendar"

    def __init__(self, gateway: GoogleWorkspaceGateway | None = None) -> None:
        self._gateway = gateway
        self._client = get_anthropic_async_client()

    async def process(self, request: AgentRequest) -> AgentResponse:
        # Verificar conexión Google antes de cualquier operación
        if self._gateway is None or not await self._gateway.is_connected():
            auth_url = ""
            if self._gateway is not None:
                try:
                    auth_url = await self._gateway.get_oauth_url(
                        scopes=["https://www.googleapis.com/auth/calendar"]
                    )
                except Exception:
                    pass
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_google_auth",
                risk_level=RiskLevel.LOW,
                requires_approval=False,
                confidence=Confidence.HIGH,
                result={
                    "auth_url": auth_url,
                    "message": (
                        "Para gestionar tu agenda necesito acceso a tu Google Calendar. "
                        "¿Lo conectamos?"
                    ),
                    "scopes_requested": ["https://www.googleapis.com/auth/calendar"],
                },
            )

        classified = await self._classify_intent(request.message)
        intent = classified.get("intent", "review_calendar")
        entities = classified.get("entities", {})

        if intent == "schedule_event":
            return await self._handle_create_event(request, entities)
        # review_calendar y find_free_time se resuelven con get_agenda
        return await self._handle_get_agenda(request)

    # ── Clasificación de intent ───────────────────────────────────────────────

    async def _classify_intent(self, message: str) -> dict:
        """Clasifica el mensaje en schedule_event, review_calendar o find_free_time."""
        system = (
            "Clasificá el mensaje en uno de estos intents:\n"
            "  schedule_event  — el usuario quiere crear, agendar o programar un evento, "
            "recordatorio o tarea.\n"
            "  review_calendar — el usuario quiere ver su agenda o eventos próximos.\n"
            "  find_free_time  — el usuario quiere saber cuándo tiene tiempo libre.\n\n"
            "Retorná SOLO un JSON (sin texto adicional):\n"
            '{"intent": "<intent>", "entities": {'
            '"title": "<str o null>", '
            '"date": "<YYYY-MM-DD o null>", '
            '"time": "<HH:MM o null>", '
            '"duration_minutes": <int o 60>'
            "}}"
        )
        try:
            resp = await self._client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=150,
                system=system,
                messages=[{"role": "user", "content": wrap_user_input(message)}],
            )
            text = (resp.content[0].text if resp.content else "").strip()
            return json.loads(text)
        except Exception:
            logger.warning("agent_calendar.classify_intent_failed", message_snippet=message[:60])
            return {"intent": "review_calendar", "entities": {}}

    # ── Handlers ──────────────────────────────────────────────────────────────

    async def _handle_create_event(self, request: AgentRequest, entities: dict) -> AgentResponse:
        """Retorna requires_approval con datos estructurados del evento a crear."""
        title = entities.get("title") or "Evento"
        date_str: str | None = entities.get("date")
        time_str: str = entities.get("time") or "09:00"
        duration = int(entities.get("duration_minutes") or 60)

        if date_str:
            try:
                start_dt = datetime.fromisoformat(f"{date_str}T{time_str}:00").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                # Fallback: mañana a las 9
                start_dt = (
                    datetime.now(timezone.utc)
                    .replace(hour=9, minute=0, second=0, microsecond=0)
                    + timedelta(days=1)
                )
        else:
            start_dt = (
                datetime.now(timezone.utc).replace(hour=9, minute=0, second=0, microsecond=0)
                + timedelta(days=1)
            )

        end_dt = start_dt + timedelta(minutes=duration)
        summary_msg = (
            f"Crear evento '{title}' el {start_dt.strftime('%d/%m/%Y a las %H:%M')} "
            f"({duration} min)"
        )

        # Confidence depende de si el LLM extrajo el título
        confidence = Confidence.HIGH if entities.get("title") else Confidence.MEDIUM

        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="requires_approval",
            risk_level=RiskLevel.MEDIUM,
            requires_approval=True,
            confidence=confidence,
            result={
                "summary": summary_msg,
                "action_type": str(ActionType.SYNC_TO_GOOGLE),
                "structured_data": {
                    "event_type": "calendar_create",
                    "title": title,
                    "start_datetime": start_dt.isoformat(),
                    "end_datetime": end_dt.isoformat(),
                    "duration_minutes": duration,
                },
            },
        )

    async def _handle_get_agenda(self, request: AgentRequest) -> AgentResponse:
        """Consulta los próximos 7 días de eventos y los retorna."""
        try:
            assert self._gateway is not None
            events = await self._gateway.calendar_get_agenda(days_ahead=7)
        except Exception:
            logger.warning("agent_calendar.get_agenda_failed")
            events = []

        if not events:
            summary_msg = "No tenés eventos en los próximos 7 días."
        else:
            lines = [f"- {e['title']} ({e['start']})" for e in events[:5]]
            summary_msg = "Próximos eventos:\n" + "\n".join(lines)
            if len(events) > 5:
                summary_msg += f"\n...y {len(events) - 5} más."

        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="success",
            risk_level=RiskLevel.LOW,
            requires_approval=False,
            confidence=Confidence.HIGH,
            result={
                "summary": summary_msg,
                "action_type": str(ActionType.GENERATE_HEALTH_REPORT),
                "events": events,
            },
        )
