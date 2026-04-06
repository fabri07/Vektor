"""AgentSupplier — gestión de proveedores, clasificación de correos y borradores.

Guardrails absolutos:
- NUNCA envía correos directamente.
- NUNCA compromete pagos sin aprobación HIGH.
- CREATE_SUPPLIER_DRAFT SIEMPRE es pending_action MEDIUM — sin excepción.

Flujo completo de email:
  1. preflight_check(metadata) → si falla, GMAIL_SKIPPED
  2. classify_email(metadata) → {classification, confidence, should_open_body}
  3. Si should_open_body=True → get_message(id) → body completo
  4. create_draft(email_data, ...) → texto del borrador (LLM, no pushea a Gmail)
  5. Retorna requires_approval=True + pending_action con draft_text
  6. Post-aprobación: execute_pending_action() pushea draft a Gmail via GmailClient

Modelos LLM:
  - Clasificación / extracción: claude-haiku-4-5-20251001 (AsyncAnthropic)
  - Sin LLM síncrono — usar siempre AsyncAnthropic.

Pre-flight check: obligatorio antes de abrir cualquier correo Gmail.
Context budget: 3.500 tokens.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import anthropic
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.base import BaseAgent
from app.application.agents.shared.schemas import (
    ActionType,
    AgentRequest,
    AgentResponse,
    Confidence,
    RiskLevel,
)
from app.application.agents.supplier.preflight import gmail_preflight_check
from app.application.security.prompt_defense import wrap_user_input
from app.integrations.google_workspace.exceptions import WorkspaceTokenError
from app.integrations.google_workspace.gateway import GoogleWorkspaceGateway
from app.observability.logger import get_logger

logger = get_logger(__name__)

EMAIL_CLASSIFICATIONS = [
    "pedido",
    "factura",
    "lista_precios",
    "consulta",
    "reclamo",
    "confirmacion_entrega",
    "irrelevante",
]

# Aserción de seguridad: CREATE_SUPPLIER_DRAFT DEBE ser MEDIUM según RiskEngine.
# Si se rompe, falla en import (no en runtime).
from app.application.agents.shared.risk_engine import RiskEngine as _RE
assert _RE.evaluate(ActionType.CREATE_SUPPLIER_DRAFT) == RiskLevel.MEDIUM, (
    "CREATE_SUPPLIER_DRAFT debe ser RiskLevel.MEDIUM en RiskEngine — "
    "si cambiás el risk map, revisá el contrato de aprobación de AgentSupplier."
)
assert _RE.evaluate(ActionType.CLASSIFY_GMAIL_MESSAGE) == RiskLevel.LOW, (
    "CLASSIFY_GMAIL_MESSAGE debe ser RiskLevel.LOW en RiskEngine."
)


class AgentSupplier(BaseAgent):
    agent_name = "agent_supplier"

    def __init__(
        self,
        session: AsyncSession | None = None,
        gateway: GoogleWorkspaceGateway | None = None,
    ) -> None:
        """
        Args:
            session: AsyncSession necesaria para workspace queries.
            gateway: GoogleWorkspaceGateway inyectado en tests o pasado desde el router.
                     Si es None, las operaciones Gmail devuelven requires_clarification
                     con reason="not_connected".
        """
        self._session = session
        self._gateway = gateway
        self._llm = anthropic.AsyncAnthropic()

    # ── LLM helpers ───────────────────────────────────────────────────────────

    async def classify_email(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """Clasifica el correo usando solo metadata y snippet (Nivel 1).

        Retorna {classification, confidence, should_open_body}.
        should_open_body=True solo para: pedido, factura, lista_precios.
        """
        system = (
            f"Clasificá este correo de proveedor en una de estas categorías:\n"
            f"{', '.join(EMAIL_CLASSIFICATIONS)}\n\n"
            'Retorná SOLO un JSON:\n'
            '{"classification": "<categoria>", "confidence": "<HIGH|MEDIUM|LOW>", "should_open_body": <true|false>}\n\n'
            "should_open_body=true solo para: pedido, factura, lista_precios\n"
            "NO retornes nada más que el JSON."
        )
        snippet = (
            f"De: {metadata.get('from')}\n"
            f"Asunto: {metadata.get('subject')}\n"
            f"Snippet: {metadata.get('snippet', '')}"
        )
        response = await self._llm.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system=system,
            messages=[{"role": "user", "content": wrap_user_input(snippet)}],
        )
        raw = response.content[0].text.strip()
        try:
            result: dict[str, Any] = json.loads(raw)
        except (json.JSONDecodeError, IndexError):
            result = {"classification": "irrelevante", "confidence": "LOW", "should_open_body": False}

        if result.get("classification") not in EMAIL_CLASSIFICATIONS:
            result["classification"] = "irrelevante"
        return result

    async def create_draft(
        self,
        email_data: dict[str, Any],
        supplier_name: str,
        business_context: dict[str, Any],
    ) -> str:
        """Genera texto de borrador con LLM. NUNCA envía ni llama a Gmail aquí.

        El texto se incluye en el pending_action. Post-aprobación,
        execute_pending_action() lo pushea a Gmail via GmailClient.create_draft().
        """
        system = (
            f"Generá un borrador de email profesional en español para responder a un proveedor.\n"
            f"Negocio: {business_context.get('name', 'el negocio')}\n"
            f"Proveedor: {supplier_name}\n\n"
            "El borrador debe ser claro, profesional y conciso."
        )
        context = json.dumps(email_data, ensure_ascii=False)
        response = await self._llm.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            system=system,
            messages=[
                {
                    "role": "user",
                    "content": wrap_user_input(f"Generá borrador para: {context}"),
                }
            ],
        )
        return response.content[0].text.strip()

    # ── process ───────────────────────────────────────────────────────────────

    async def process(self, request: AgentRequest) -> AgentResponse:
        message = request.message.lower()

        if any(kw in message for kw in ("mail", "correo", "email", "gmail")):
            return await self._handle_email_request(request)
        elif "proveedor" in message:
            return await self._handle_supplier_query(request)
        else:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="success",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.HIGH,
                result={"summary": "No identifiqué una acción concreta para proveedores."},
            )

    # ── Handlers ──────────────────────────────────────────────────────────────

    async def _handle_email_request(self, request: AgentRequest) -> AgentResponse:
        """Flujo completo: preflight → fetch metadata → classify → (draft si relevante).

        Si no hay gateway (workspace no conectado) → requires_clarification.
        Si el scope es insuficiente → requires_clarification + reconnect_required.
        Si el correo no pasa preflight → GMAIL_SKIPPED sin error al usuario.
        Si la clasificación indica borrador → requires_approval (MEDIUM).
        """
        if self._gateway is None:
            return self._workspace_not_connected(request)

        # Verificar conexión activa antes de gastar tokens LLM
        try:
            connected = await self._gateway.is_connected()
        except Exception:
            connected = False

        if not connected:
            return self._workspace_not_connected(request)

        # Obtener metadata de correos relevantes desde Gmail
        try:
            gmail = await self._gateway.gmail()
            messages = await self._gateway.run_gmail(
                gmail.list_messages(query="from:proveedor label:INBOX", max_results=5)
            )
        except WorkspaceTokenError as exc:
            if exc.reason == "insufficient_scope":
                return self._insufficient_scope(request)
            return self._workspace_error(request, exc.reason)

        if not messages:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="success",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.HIGH,
                result={"summary": "No encontré correos de proveedores recientes."},
            )

        # Procesar primer mensaje que pase preflight
        for msg_summary in messages:
            metadata = {
                "from": msg_summary.from_,
                "subject": msg_summary.subject,
                "snippet": msg_summary.snippet,
                "labels": msg_summary.labels,
            }

            passes = await gmail_preflight_check(
                metadata=metadata,
                business_id=request.business_id,
                db=self._session,
                user_requested=True,  # el usuario pidió explícitamente revisar correos
            )
            if not passes:
                logger.info(
                    "agent_supplier.preflight_skipped",
                    message_id=msg_summary.message_id,
                    from_=msg_summary.from_,
                )
                continue

            # Clasificar (Nivel 1 — solo metadata)
            classification = await self.classify_email(metadata)
            action = ActionType.CLASSIFY_GMAIL_MESSAGE

            if not classification.get("should_open_body", False):
                # Clasificación sin borrador — LOW risk, sin pending_action
                return AgentResponse(
                    request_id=request.request_id,
                    agent_name=self.agent_name,
                    status="success",
                    risk_level=RiskLevel.LOW,
                    confidence=Confidence(classification.get("confidence", "MEDIUM")),
                    result={
                        "summary": f"Correo clasificado como '{classification['classification']}'.",
                        "action_type": action,
                        "classification": classification,
                        "message_id": msg_summary.message_id,
                    },
                )

            # Abrir body para generar borrador
            try:
                full_msg = await self._gateway.run_gmail(
                    gmail.get_message(msg_summary.message_id)
                )
            except WorkspaceTokenError as exc:
                if exc.reason == "insufficient_scope":
                    return self._insufficient_scope(request)
                return self._workspace_error(request, exc.reason)

            email_data = {
                "from": full_msg.from_,
                "subject": full_msg.subject,
                "body": (full_msg.body_text or full_msg.snippet or ""),
            }
            draft_text = await self.create_draft(
                email_data=email_data,
                supplier_name=full_msg.from_ or "Proveedor",
                business_context={"name": request.business_id},
            )

            # CREATE_SUPPLIER_DRAFT → MEDIUM → requires_approval → pending_action
            pending_action_id = str(uuid.uuid4())
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_approval",
                risk_level=RiskLevel.MEDIUM,
                requires_approval=True,
                confidence=Confidence(classification.get("confidence", "MEDIUM")),
                pending_action_id=pending_action_id,
                result={
                    "summary": "Borrador de respuesta generado. Revisalo antes de enviarlo.",
                    "action_type": ActionType.CREATE_SUPPLIER_DRAFT,
                    "structured_data": {
                        "draft_text": draft_text,
                        "draft_to": full_msg.from_,
                        "draft_subject": f"Re: {full_msg.subject or ''}",
                        "source_message_id": msg_summary.message_id,
                    },
                    "note": "Este borrador requiere tu aprobación antes de guardarse en Gmail.",
                },
            )

        # Todos los mensajes fueron filtrados por preflight
        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="success",
            risk_level=RiskLevel.LOW,
            confidence=Confidence.HIGH,
            result={"summary": "Los correos encontrados no pasaron el filtro de proveedores autorizados."},
        )

    async def _handle_supplier_query(self, request: AgentRequest) -> AgentResponse:
        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="success",
            risk_level=RiskLevel.LOW,
            confidence=Confidence.HIGH,
            result={"summary": "Consultá el panel de proveedores para ver el estado actual."},
        )

    # ── Respuestas de error tipadas ────────────────────────────────────────────

    def _workspace_not_connected(self, request: AgentRequest) -> AgentResponse:
        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="requires_clarification",
            risk_level=RiskLevel.LOW,
            confidence=Confidence.HIGH,
            result={"reconnect_required": True, "reason": "not_connected"},
            question="Tu cuenta de Gmail no está conectada. ¿Querés conectarla ahora?",
        )

    def _insufficient_scope(self, request: AgentRequest) -> AgentResponse:
        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="requires_clarification",
            risk_level=RiskLevel.LOW,
            confidence=Confidence.HIGH,
            result={"reconnect_required": True, "reason": "insufficient_scope"},
            question="Tu cuenta de Google necesita reconectarse para otorgar los permisos necesarios. ¿Querés hacerlo ahora?",
        )

    def _workspace_error(self, request: AgentRequest, reason: str) -> AgentResponse:
        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="error",
            risk_level=RiskLevel.LOW,
            confidence=Confidence.LOW,
            result={"error": reason},
        )
