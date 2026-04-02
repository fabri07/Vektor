"""AgentCash — registra y analiza movimientos monetarios."""

import json
from datetime import date
from decimal import Decimal
from typing import Optional

import anthropic
from pydantic import BaseModel, Field

from app.application.agents.base import BaseAgent
from app.application.agents.shared.event_bus import EventBus
from app.application.agents.shared.schemas import (
    ActionType,
    AgentRequest,
    AgentResponse,
    Confidence,
    RiskLevel,
)


class SaleEntity(BaseModel):
    amount: Decimal
    transaction_date: date = Field(default_factory=date.today)
    payment_status: str  # "paid" | "credit" | "unknown"
    payment_method: Optional[str] = None
    product_description: Optional[str] = None
    notes: Optional[str] = None


class CashInflowEntity(BaseModel):
    amount: Decimal
    transaction_date: date = Field(default_factory=date.today)
    linked_sale_id: Optional[str] = None
    notes: Optional[str] = None


class ExpenseEntity(BaseModel):
    amount: Decimal
    transaction_date: date = Field(default_factory=date.today)
    category: str  # alquiler, sueldos, servicios, insumos, otros
    description: Optional[str] = None


class AgentCash(BaseAgent):
    agent_name = "agent_cash"

    def __init__(self) -> None:
        self.client = anthropic.AsyncAnthropic()

    async def _extract_sale_entities(self, message: str, business_context: dict) -> dict:
        system = (
            f"Sos el asistente de carga de ventas de Véktor.\n"
            f"Negocio: {business_context.get('name', 'el negocio')} "
            f"({business_context.get('type', 'kiosco_almacen')})\n\n"
            "<instruccion>\n"
            "Extraé del mensaje la información de la venta. Retorná SOLO un JSON con:\n"
            "{\n"
            '  "amount": <número>,\n'
            '  "date": "<YYYY-MM-DD o hoy>",\n'
            '  "payment_status": "<paid si mencionó efectivo/tarjeta/transferencia, '
            'credit si mencionó cuenta corriente/fiado, unknown si no especificó>",\n'
            '  "payment_method": "<efectivo|tarjeta|transferencia|null>",\n'
            '  "product_description": "<descripción breve o null>",\n'
            '  "confidence": "<HIGH|MEDIUM|LOW>"\n'
            "}\n"
            'Si no podés extraer el monto → {"error": "No pude identificar el monto de la venta."}\n'
            "</instruccion>"
        )
        response = await self.client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=system,
            messages=[{"role": "user", "content": self.wrap_user_input(message)}],
        )
        return json.loads(response.content[0].text.strip())

    async def process(self, request: AgentRequest) -> AgentResponse:
        business_context = {"name": "el negocio", "type": "kiosco_almacen"}
        entities = await self._extract_sale_entities(request.message, business_context)

        if "error" in entities:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                question=entities["error"],
            )

        # REGLA CRÍTICA 2: payment_status unknown → pedir aclaración ANTES de crear pending_action
        if entities.get("payment_status") == "unknown":
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                question="¿La venta fue al contado o en cuenta corriente?",
            )

        amount = entities.get("amount", 0)
        summary = (
            f"Venta por ${amount:,.0f} "
            f"{'al contado' if entities.get('payment_status') == 'paid' else 'en cuenta corriente'}"
        )
        if entities.get("product_description"):
            summary += f" — {entities['product_description']}"

        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="requires_approval",
            risk_level=RiskLevel.MEDIUM,
            requires_approval=True,
            confidence=Confidence.HIGH if entities.get("confidence") == "HIGH" else Confidence.MEDIUM,
            result={
                "summary": summary,
                "action_type": ActionType.REGISTER_SALE,
                "structured_data": entities,
                "alerts": [],
            },
        )

    async def on_confirmed_sale(self, sale_id: str, business_id: str) -> None:
        """Llamado después de confirmar y persistir una venta."""
        EventBus.emit("SALE_RECORDED", {"sale_id": sale_id, "business_id": business_id})
        await self.recalculate_cash_health(business_id)

    async def recalculate_cash_health(self, business_id: str) -> None:
        """Recalcular cobertura de caja y emitir alerta si corresponde."""
        EventBus.emit("CASH_HEALTH_UPDATED", {"business_id": business_id})
