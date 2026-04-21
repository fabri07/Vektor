"""AgentSync — sincronización bidireccional entre Véktor y Google Workspace via MCP."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.application.agents.base import BaseAgent
from app.application.agents.shared.schemas import ActionType, AgentRequest, AgentResponse, Confidence, RiskLevel
from app.observability.logger import get_logger

if TYPE_CHECKING:
    from app.application.ports.mcp_gateway import McpToolGateway

logger = get_logger(__name__)

_SYNC_TYPES = {
    "export_sales_to_sheets": {
        "keywords": ("exportar ventas", "ventas a sheets", "exportar a google sheets", "subir ventas"),
        "tool": "google.sheets.append_rows",
        "summary": "Exportar ventas a Google Sheets.",
    },
    "export_report_to_docs": {
        "keywords": ("exportar reporte", "reporte a docs", "informe a google docs", "generar doc"),
        "tool": "google.docs.create_document",
        "summary": "Exportar reporte a Google Docs.",
    },
    "import_from_sheets": {
        "keywords": ("importar desde sheets", "importar de google", "traer datos de sheets", "leer sheets"),
        "tool": "google.sheets.read_range",
        "summary": "Importar datos desde Google Sheets.",
    },
}


class AgentSync(BaseAgent):
    agent_name = "agent_sync"

    def __init__(self, gateway: "McpToolGateway | None" = None) -> None:
        self._gateway = gateway

    async def process(self, request: AgentRequest) -> AgentResponse:
        message_lower = request.message.lower()

        if self._gateway is None:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_google_auth",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.HIGH,
                result={
                    "action_type": ActionType.SYNC_TO_GOOGLE,
                    "mode": "informational",
                    "message": "Necesito que la integración de Google esté disponible para sincronizar datos.",
                },
            )

        sync_type = self._classify_sync_type(message_lower)
        if sync_type is None:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.LOW,
                question=(
                    "¿Qué tipo de sincronización con Google querés hacer?\n"
                    "- Exportar ventas a Google Sheets\n"
                    "- Exportar reporte a Google Docs\n"
                    "- Importar datos desde Google Sheets"
                ),
                result={"summary": "Tipo de sincronización no identificado."},
            )

        cfg = _SYNC_TYPES[sync_type]
        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="requires_approval",
            risk_level=RiskLevel.MEDIUM,
            confidence=Confidence.HIGH,
            requires_approval=True,
            result={
                "action_type": ActionType.SYNC_TO_GOOGLE,
                "sync_type": sync_type,
                "mcp_tool": cfg["tool"],
                "summary": cfg["summary"],
                "mode": "mcp" if self._gateway else "informational",
                "payload": {"sync_type": sync_type, "raw_message": request.message},
            },
        )

    def _classify_sync_type(self, message: str) -> str | None:
        for sync_type, cfg in _SYNC_TYPES.items():
            if any(kw in message for kw in cfg["keywords"]):
                return sync_type
        return None
