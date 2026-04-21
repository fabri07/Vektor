"""AgentCash — registra y analiza movimientos monetarios."""

import json
from datetime import date
from decimal import Decimal
from typing import Any, Optional

import anthropic
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.base import BaseAgent
from app.application.agents.shared.event_bus import EventBus
from app.application.agents.shared.heuristic_engine import HeuristicEngine
from app.application.agents.shared.schemas import (
    ActionType,
    AgentRequest,
    AgentResponse,
    Confidence,
    RiskLevel,
)
from app.integrations.anthropic_client import get_anthropic_async_client


class SaleEntity(BaseModel):
    amount: Decimal
    transaction_date: date = Field(default_factory=date.today)
    payment_status: str
    payment_method: Optional[str] = None
    product_description: Optional[str] = None
    notes: Optional[str] = None


class AgentCash(BaseAgent):
    agent_name = "agent_cash"

    def __init__(self, db: Optional[AsyncSession] = None, redis: Optional[Redis] = None) -> None:
        self._client: Any | None = None
        self._db = db
        self._redis = redis

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = get_anthropic_async_client(anthropic.AsyncAnthropic)
        return self._client

    async def _extract_sale_entities(self, message: str, business_context: dict[str, Any]) -> dict[str, Any]:
        heuristics = HeuristicEngine.get(business_context.get("type", "kiosco_almacen"))
        system = (
            "Extraé del mensaje los datos de una venta y devolvé SOLO JSON con amount, date, "
            "payment_status, payment_method, product_description y confidence."
        )
        response = await self.client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=250,
            system=f"{system}\n\n{heuristics.to_prompt_fragment()}",
            messages=[{"role": "user", "content": self.wrap_user_input(message)}],
        )
        raw = response.content[0].text.strip() if response.content else ""
        try:
            return json.loads(raw)
        except Exception:
            return {"error": "No pude interpretar la venta. Intentá con monto y forma de pago."}

    async def _load_business_context(self, business_id: str) -> dict[str, Any]:
        return {"name": "el negocio", "type": "kiosco_almacen"}

    async def process(self, request: AgentRequest) -> AgentResponse:
        business_context = await self._load_business_context(request.business_id)
        entities = await self._extract_sale_entities(request.message, business_context)

        if "error" in entities:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                question=entities["error"],
            )

        if entities.get("payment_status") == "unknown":
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                question="¿La venta fue al contado o en cuenta corriente?",
            )

        amount = entities.get("amount", 0)
        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="requires_approval",
            risk_level=RiskLevel.MEDIUM,
            requires_approval=True,
            confidence=Confidence.HIGH,
            result={
                "summary": f"Registrar venta por ${amount}",
                "action_type": ActionType.REGISTER_SALE,
                "structured_data": entities,
                "alerts": [],
            },
        )

    async def on_confirmed_sale(self, sale_id: str, business_id: str) -> None:
        EventBus.emit("SALE_RECORDED", {"sale_id": sale_id, "business_id": business_id})
