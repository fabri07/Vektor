"""AgentSupplier — proveedor via MCP Gmail (borrador, clasificación) o registro manual."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.application.agents.base import BaseAgent
from app.application.agents.shared.schemas import ActionType, AgentRequest, AgentResponse, Confidence, RiskLevel
from app.observability.logger import get_logger

if TYPE_CHECKING:
    from app.application.ports.mcp_gateway import McpToolGateway

logger = get_logger(__name__)


class AgentSupplier(BaseAgent):
    agent_name = "agent_supplier"

    def __init__(self, session=None, gateway: "McpToolGateway | None" = None) -> None:
        self._session = session
        self._gateway = gateway

    async def process(self, request: AgentRequest) -> AgentResponse:
        message = request.message.lower()

        intent = self._classify_intent(message)

        if intent == "create_draft":
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.HIGH,
                requires_approval=False,
                question="¿A qué proveedor querés enviarle el email y qué necesitás pedirle?",
                result={
                    "action_type": ActionType.CREATE_SUPPLIER_DRAFT,
                    "summary": "Listo para preparar el borrador de email al proveedor.",
                    "mode": "mcp" if self._gateway else "informational",
                    "payload": {"message": request.message},
                },
            )

        if intent == "classify_inbox":
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="success",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.HIGH,
                requires_approval=False,
                result={
                    "action_type": ActionType.CLASSIFY_GMAIL_MESSAGE,
                    "summary": "Revisando mensajes de proveedores en Gmail.",
                    "mode": "mcp" if self._gateway else "informational",
                    "payload": {"message": request.message},
                },
            )

        if intent == "record_purchase":
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_approval",
                risk_level=RiskLevel.MEDIUM,
                confidence=Confidence.HIGH,
                requires_approval=True,
                result={
                    "action_type": ActionType.REGISTER_PURCHASE,
                    "summary": "Registrar compra a proveedor.",
                    "payload": {"message": request.message},
                },
            )

        # intent == "query" — respuesta informativa
        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="requires_clarification",
            risk_level=RiskLevel.LOW,
            confidence=Confidence.MEDIUM,
            question=(
                "¿Qué necesitás hacer con el proveedor?\n"
                "- Redactar un email de compra\n"
                "- Revisar mensajes recibidos\n"
                "- Registrar una compra manualmente"
            ),
            result={"summary": "Aclará qué acción de proveedor necesitás."},
        )

    def _classify_intent(self, message: str) -> str:
        inbox_keywords = (
            "clasificar", "revisar inbox", "revisar gmail", "mensajes recibidos", "bandeja",
            "llegó mail", "llegó email", "llegó un mail", "llegó correo",
            "recibí mail", "recibí email", "recibí un correo", "recibí un mail",
        )
        draft_keywords = (
            "borrador", "redact", "escribi", "enviá",
            "enviar mail", "enviar email", "enviar un mail", "enviar un email",
            "mandar correo", "mandar mail", "mandar un mail", "mandar un email",
            "quiero enviar", "quiero mandar",
            "escribir mail", "escribir email", "redactar",
            "email a ", "mail a ", "un mail a", "un email a",
            "podés enviar", "puedes enviar", "podés mandar", "puedes mandar",
        )
        purchase_keywords = ("compra", "compré", "compramos", "registrar compra", "factura", "proveedor cobró")

        if any(kw in message for kw in inbox_keywords):
            return "classify_inbox"
        if any(kw in message for kw in draft_keywords):
            return "create_draft"
        if any(kw in message for kw in purchase_keywords):
            return "record_purchase"
        return "query"
