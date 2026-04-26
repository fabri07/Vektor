"""AgentSupplier — proveedor via MCP Gmail (borrador, clasificación) o registro manual."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import anthropic

from app.application.agents.base import BaseAgent
from app.application.agents.shared.schemas import ActionType, AgentRequest, AgentResponse, Confidence, RiskLevel
from app.application.security.prompt_defense import wrap_user_input
from app.integrations.anthropic_client import get_anthropic_async_client
from app.observability.logger import get_logger

if TYPE_CHECKING:
    from app.application.ports.mcp_gateway import McpToolGateway

logger = get_logger(__name__)


class AgentSupplier(BaseAgent):
    agent_name = "agent_supplier"

    def __init__(self, session=None, gateway: "McpToolGateway | None" = None) -> None:
        self._session = session
        self._gateway = gateway
        self._client: Any | None = None

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = get_anthropic_async_client(anthropic.AsyncAnthropic)
        return self._client

    async def process(self, request: AgentRequest) -> AgentResponse:
        message = request.message.lower()
        intent = self._classify_intent(message)

        if intent == "create_draft":
            return await self._handle_create_draft(request)

        if intent == "classify_inbox":
            return self._handle_classify_inbox(request)

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

    async def _handle_create_draft(self, request: AgentRequest) -> AgentResponse:
        mode = "mcp" if self._gateway else "informational"
        draft = await self._generate_email_draft(request.message)

        if not draft.get("has_enough_info"):
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.HIGH,
                requires_approval=False,
                question="Para armar el email necesito: ¿A qué proveedor querés enviarlo y qué necesitás pedirle o comunicarle?",
                result={
                    "action_type": ActionType.CREATE_SUPPLIER_DRAFT,
                    "summary": "Necesito más datos para generar el email.",
                    "mode": mode,
                    "payload": {"message": request.message},
                },
            )

        to_name = draft.get("to_name") or "proveedor"
        subject = draft.get("subject") or "Consulta"
        body = draft.get("body") or request.message

        summary_preview = body[:200] + "..." if len(body) > 200 else body
        summary = f"Email para {to_name}\nAsunto: {subject}\n\n{summary_preview}"

        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="requires_approval",
            risk_level=RiskLevel.MEDIUM,
            confidence=Confidence.HIGH,
            requires_approval=True,
            result={
                "action_type": ActionType.CREATE_SUPPLIER_DRAFT,
                "summary": summary,
                "mode": mode,
                "payload": {
                    "to": draft.get("to_email") or "",
                    "to_name": to_name,
                    "subject": subject,
                    "body": body,
                    "email_mode": "draft",
                    "mode": mode,
                },
            },
        )

    def _handle_classify_inbox(self, request: AgentRequest) -> AgentResponse:
        mode = "mcp" if self._gateway else "informational"
        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="requires_approval",
            risk_level=RiskLevel.LOW,
            confidence=Confidence.HIGH,
            requires_approval=True,
            result={
                "action_type": ActionType.CLASSIFY_GMAIL_MESSAGE,
                "summary": "Revisar mensajes recibidos de proveedores en Gmail.",
                "mode": mode,
                "payload": {
                    "message": request.message,
                    "message_id": "",
                    "mode": mode,
                },
            },
        )

    async def _generate_email_draft(self, message: str) -> dict:
        system = (
            "Sos el asistente de Véktor. Analizá el mensaje del usuario y generá un email "
            "formal en español rioplatense para un proveedor.\n\n"
            "Retorná SOLO un JSON:\n"
            "{\n"
            '  "has_enough_info": true|false,\n'
            '  "to_name": "nombre del proveedor o null",\n'
            '  "to_email": "email si se menciona explícitamente, o null",\n'
            '  "subject": "asunto del email",\n'
            '  "body": "cuerpo completo del email, profesional y directo"\n'
            "}\n\n"
            "has_enough_info=true solo si el mensaje tiene suficiente contexto para saber "
            "QUÉ comunicarle al proveedor (qué pedir, reclamar o consultar).\n"
            "Si falta el destinatario, igual generá el email con to_name=null.\n"
            "Si falta el contenido → has_enough_info=false."
        )
        try:
            response = await self.client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=600,
                system=system,
                messages=[{"role": "user", "content": wrap_user_input(message)}],
            )
            raw = response.content[0].text.strip() if response.content else ""
            return json.loads(raw)
        except Exception as exc:
            logger.warning("agent_supplier.draft_generation_failed", error=str(exc))
            return {"has_enough_info": False}

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
