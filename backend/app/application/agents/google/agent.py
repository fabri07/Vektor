"""AgentGoogle — broker de herramientas Google (Stage 2a: merge de AgentCalendar + AgentSync).

Responsabilidades:
- Ejecutar acciones Google Calendar (CREATE_CALENDAR_EVENT) via AgentCalendar (LLM reasoning)
- Ejecutar acciones Google Sheets/Drive/Docs (SYNC_TO_GOOGLE) via AgentSync (LLM reasoning)
- Stage 4: ejecutar UPLOAD_TO_DRIVE / CREATE_GOOGLE_DOC / APPEND_TO_SHEET emitiendo
  PendingActions de aprobación (la ejecución real va por PendingActionService → GoogleToolBroker)
- No toma decisiones de negocio — solo ejecuta acciones externas

AgentGoogle es el ÚNICO agente que importa GoogleToolBroker directamente.
Los agentes de dominio (income, expense, stock, health, supplier) NO importan el broker.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

from app.application.agents.base import BaseAgent
from app.application.agents.google.tool_broker import GoogleToolBroker
from app.application.agents.shared.schemas import (
    ActionType,
    AgentRequest,
    AgentResponse,
    Confidence,
    RiskLevel,
)

if TYPE_CHECKING:
    from app.application.ports.mcp_gateway import McpToolGateway

# ActionTypes que este agente despacha via PendingAction → PendingActionService → broker
_BROKER_ACTION_TYPES = frozenset({
    ActionType.UPLOAD_TO_DRIVE,
    ActionType.CREATE_GOOGLE_DOC,
    ActionType.APPEND_TO_SHEET,
})

_BROKER_ACTION_LABELS: dict[ActionType, str] = {
    ActionType.UPLOAD_TO_DRIVE: "Subir archivo a Google Drive",
    ActionType.CREATE_GOOGLE_DOC: "Crear documento en Google Docs",
    ActionType.APPEND_TO_SHEET: "Agregar datos a Google Sheets",
}


class AgentGoogle(BaseAgent):
    agent_name = "agent_google"

    def __init__(
        self,
        gateway: McpToolGateway | None = None,
        tenant_id: str | None = None,
        user_id: str | None = None,
    ) -> None:
        self._gateway = gateway
        self._tenant_id = tenant_id
        self._user_id = user_id

    def make_broker(self, settings: Any) -> GoogleToolBroker:
        """Crea un GoogleToolBroker para uso directo de este agente."""
        return GoogleToolBroker(
            user_id=self._user_id or "",
            tenant_id=self._tenant_id or "",
            settings=settings,
        )

    async def process(  # type: ignore[override]
        self,
        request: AgentRequest,
        task: Any | None = None,
    ) -> AgentResponse:
        action_type = getattr(task, "action_type", None) if task else None

        # Stage 4: nuevos ActionTypes → PendingAction de aprobación (ejecución via broker)
        if action_type in _BROKER_ACTION_TYPES:
            return self._pending_for_broker_action(request, action_type, task)

        if action_type == ActionType.CREATE_CALENDAR_EVENT:
            return await self._dispatch_calendar(request)
        # Default: sync / sheets / docs / drive
        return await self._dispatch_sync(request)

    def _pending_for_broker_action(
        self,
        request: AgentRequest,
        action_type: ActionType,
        task: Any | None,
    ) -> AgentResponse:
        """Emite requires_approval para que PendingActionService ejecute via broker.

        Para UPLOAD_TO_DRIVE: lee upstream_outputs del DAG para obtener el contenido
        real del informe de salud (generado por AgentHealth en el task previo).
        """
        entities = dict(task.entities) if task else {}

        if action_type == ActionType.UPLOAD_TO_DRIVE and not entities.get("content_base64"):
            upstream_outputs = request.context.get("upstream_outputs", {})
            if upstream_outputs:
                # Toma el primer resultado upstream (esperamos el health report de AgentHealth).
                # Stage 4: export como texto plano con narrativa + score.
                # Stage 5a: AgentHealth generará PDF con reportlab y lo persistirá en R2;
                #           el payload incluirá blob_id en lugar de content_base64 inline.
                upstream_result = next(iter(upstream_outputs.values()), {})
                narrative: str = upstream_result.get("summary", "")
                health_score = upstream_result.get("health_score")
                if narrative:
                    lines: list[str] = []
                    if health_score is not None:
                        lines.append(f"Score de salud: {health_score}/100\n")
                    lines.append(narrative)
                    report_text = "\n".join(lines)
                    entities["content_base64"] = base64.b64encode(
                        report_text.encode()
                    ).decode()
                    entities.setdefault("filename", "informe_salud_vektor.txt")
                    entities.setdefault("mime_type", "text/plain")

        payload = {**entities, "mode": "mcp", "raw_message": request.message}
        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="requires_approval",
            risk_level=RiskLevel.MEDIUM,
            confidence=Confidence.HIGH,
            requires_approval=True,
            result={
                "action_type": action_type,
                "summary": _BROKER_ACTION_LABELS.get(action_type, "Acción Google"),
                "mode": "mcp",
                "payload": payload,
            },
        )

    async def _dispatch_calendar(self, request: AgentRequest) -> AgentResponse:
        from app.application.agents.calendar.agent import AgentCalendar  # noqa: PLC0415

        delegate = AgentCalendar(gateway=self._gateway, tenant_id=self._tenant_id)
        response = await delegate.process(request)
        response.agent_name = self.agent_name
        return response

    async def _dispatch_sync(self, request: AgentRequest) -> AgentResponse:
        from app.application.agents.sync.agent import AgentSync  # noqa: PLC0415
        from app.config.settings import get_settings  # noqa: PLC0415

        # Inyectar broker para que AgentSync use el broker en lugar de GoogleMcpService directo
        broker = GoogleToolBroker(
            user_id=request.user_id,
            tenant_id=self._tenant_id or "",
            settings=get_settings(),
        ) if self._tenant_id else None

        delegate = AgentSync(gateway=self._gateway, tenant_id=self._tenant_id, broker=broker)
        response = await delegate.process(request)
        response.agent_name = self.agent_name
        return response
