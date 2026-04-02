"""AgentSupplier — gestión de proveedores, clasificación de correos y borradores.

Guardrails absolutos:
- NUNCA envía correos directamente.
- NUNCA compromete pagos sin aprobación HIGH.
- CREATE_SUPPLIER_DRAFT SIEMPRE es pending_action MEDIUM — sin excepción.

Pre-flight check: obligatorio antes de abrir cualquier correo Gmail.
Modelo LLM: claude-haiku-4-5-20251001 para clasificación y extracción.
Context budget: 3.500 tokens.
"""

import anthropic
import json

from app.application.agents.base import BaseAgent
from app.application.agents.shared.schemas import (
    ActionType,
    AgentRequest,
    AgentResponse,
    Confidence,
    RiskLevel,
)
from app.application.security.prompt_defense import wrap_user_input

client = anthropic.Anthropic()

EMAIL_CLASSIFICATIONS = [
    "pedido",
    "factura",
    "lista_precios",
    "consulta",
    "reclamo",
    "confirmacion_entrega",
    "irrelevante",
]


class AgentSupplier(BaseAgent):
    agent_name = "agent_supplier"

    async def classify_email(self, metadata: dict) -> dict:
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
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system=system,
            messages=[{"role": "user", "content": snippet}],
        )
        raw = response.content[0].text.strip()
        try:
            result = json.loads(raw)
        except (json.JSONDecodeError, IndexError):
            result = {"classification": "irrelevante", "confidence": "LOW", "should_open_body": False}

        if result.get("classification") not in EMAIL_CLASSIFICATIONS:
            result["classification"] = "irrelevante"
        return result

    async def create_draft(
        self,
        email_data: dict,
        supplier_name: str,
        business_context: dict,
    ) -> str:
        """Genera un borrador de respuesta. NUNCA envía directamente."""
        system = (
            f"Generá un borrador de email profesional en español para responder a un proveedor.\n"
            f"Negocio: {business_context.get('name', 'el negocio')}\n"
            f"Proveedor: {supplier_name}\n\n"
            "El borrador debe ser claro, profesional y conciso."
        )
        context = json.dumps(email_data, ensure_ascii=False)
        response = client.messages.create(
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

    async def process(self, request: AgentRequest) -> AgentResponse:
        message = request.message.lower()

        if "mail" in message or "correo" in message or "email" in message:
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

    async def _handle_email_request(self, request: AgentRequest) -> AgentResponse:
        """Gestiona solicitudes relacionadas con correos de proveedores.

        Siempre retorna requires_approval=True — CREATE_SUPPLIER_DRAFT es MEDIUM sin excepción.
        En producción: recibir metadata del correo como attachment estructurado.
        """
        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="requires_approval",
            risk_level=RiskLevel.MEDIUM,
            requires_approval=True,
            confidence=Confidence.HIGH,
            result={
                "summary": "Borrador de respuesta al proveedor generado para tu revisión.",
                "action_type": ActionType.CREATE_SUPPLIER_DRAFT,
                "structured_data": {
                    "draft": "Borrador pendiente de generación con datos del correo",
                },
                "note": "RECORDATORIO: Este borrador requiere tu aprobación antes de enviarse.",
            },
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
