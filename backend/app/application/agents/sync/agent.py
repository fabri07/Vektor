"""AgentSync — sincronizaciones bajo demanda con Google Workspace."""
from __future__ import annotations

import json
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


class AgentSync(BaseAgent):
    agent_name = "agent_sync"

    def __init__(self, gateway: "GoogleWorkspaceGateway | None" = None) -> None:
        self._gateway = gateway
        self._client = get_anthropic_async_client()

    async def process(self, request: AgentRequest) -> AgentResponse:
        if self._gateway is None or not await self._gateway.is_connected():
            auth_url = ""
            if self._gateway is not None:
                try:
                    auth_url = await self._gateway.get_oauth_url(
                        [
                            "https://www.googleapis.com/auth/drive.file",
                            "https://www.googleapis.com/auth/spreadsheets",
                        ]
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
                    "message": "Para sincronizar con Google necesito acceso a Drive y Sheets.",
                },
            )

        # Clasificar tipo de sync
        sync_type = await self._classify_sync_type(request.message)

        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="requires_approval",
            risk_level=RiskLevel.MEDIUM,
            requires_approval=True,
            confidence=Confidence.HIGH,
            result={
                "summary": f"Sincronizar {sync_type} con Google",
                "action_type": str(ActionType.SYNC_TO_GOOGLE),
                "structured_data": {
                    "sync_type": sync_type,
                    "message": request.message,
                },
            },
        )

    async def _classify_sync_type(self, message: str) -> str:
        system = (
            "Clasificá en uno: export_sales, export_expenses, upload_report, send_summary, sync_inventory.\n"
            'Retorná SOLO: {"sync_type": "<tipo>"}'
        )
        try:
            resp = await self._client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=50,
                system=system,
                messages=[{"role": "user", "content": wrap_user_input(message)}],
            )
            return json.loads(resp.content[0].text.strip()).get("sync_type", "export_sales")
        except Exception:
            return "export_sales"
