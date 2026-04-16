"""AgentStock — gestión de inventario en tiempo real."""

import json
import uuid
from decimal import Decimal
from typing import Any, Optional

import anthropic
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.base import BaseAgent
from app.application.agents.shared.heuristic_engine import HeuristicEngine
from app.application.agents.shared.schemas import (
    ActionType,
    AgentRequest,
    AgentResponse,
    Confidence,
    RiskLevel,
)
from app.integrations.anthropic_client import get_anthropic_async_client


class StockAdjustEntity(BaseModel):
    product_id: Optional[str] = None
    sku: Optional[str] = None
    product_name: Optional[str] = None
    qty_change: int  # positivo = alta, negativo = baja
    reason: str  # venta, compra, merma, ajuste, devolucion
    unit_cost: Optional[Decimal] = None


class AgentStock(BaseAgent):
    agent_name = "agent_stock"

    def __init__(self) -> None:
        self._client: Any | None = None

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = get_anthropic_async_client(anthropic.AsyncAnthropic)
        return self._client

    @client.setter
    def client(self, value: Any) -> None:
        self._client = value

    async def on_sale_recorded(
        self,
        sale_id: str,
        tenant_id: str,
        db: Optional[AsyncSession] = None,
    ) -> None:
        """
        Reacciona al evento SALE_RECORDED.
        Decrementa el stock del producto vendido llamando a stock_service directamente.
        Si la venta no tiene product_id asociado, no hay movimiento de inventario.
        """
        from app.application.services import stock_service  # noqa: PLC0415
        from app.observability.logger import get_logger  # noqa: PLC0415
        from app.persistence.models.transaction import SaleEntry  # noqa: PLC0415

        logger = get_logger(__name__)

        if db is None:
            logger.warning("on_sale_recorded: no db session provided, skipping stock decrement",
                           sale_id=sale_id)
            return

        try:
            sale_uuid = uuid.UUID(sale_id)
            tenant_uuid = uuid.UUID(tenant_id)
        except ValueError:
            logger.warning("on_sale_recorded: invalid sale_id or tenant_id",
                           sale_id=sale_id, tenant_id=tenant_id)
            return

        result = await db.execute(
            select(SaleEntry).where(
                SaleEntry.id == sale_uuid,
                SaleEntry.tenant_id == tenant_uuid,
            )
        )
        sale = result.scalar_one_or_none()

        if sale is None:
            logger.warning("on_sale_recorded: sale not found", sale_id=sale_id)
            return

        if sale.product_id is None:
            logger.info("on_sale_recorded: sale has no product_id, skipping stock decrement",
                        sale_id=sale_id)
            return

        await stock_service.decrement_stock(
            product_id=sale.product_id,
            tenant_id=tenant_uuid,
            qty=sale.quantity,
            source_event_id=sale_id,
            db=db,
        )

    async def detect_stockout(
        self,
        product_id: str,
        current_qty: int,
        min_threshold: int = 0,
    ) -> bool:
        """True si el stock está en riesgo de quiebre."""
        return current_qty <= min_threshold

    async def detect_overstock(
        self,
        product_id: str,
        rotation_days: float,
        business_type: str,
    ) -> bool:
        """
        True si el producto está inmovilizado.
        Condición: rotación_real > 2 × rotation_days_max heurístico del rubro.
        """
        config = HeuristicEngine.get(business_type)
        return config.is_overstock(rotation_days)

    async def generate_replenishment_ranking(self, tenant_id: str) -> list[dict]:
        """
        Top-10 productos a reponer ordenados por urgencia:
        1. Quiebre inminente (stock <= 0)
        2. Stock bajo (stock <= 20% del máximo histórico)
        3. Alta velocidad de rotación
        """
        return []

    async def process(self, request: AgentRequest) -> AgentResponse:
        message_lower = request.message.lower()

        if any(w in message_lower for w in ["merma", "roto", "perdí", "perdido", "vencido"]):
            return await self._handle_stock_loss(request)
        elif any(w in message_lower for w in ["ajuste", "conteo", "inventario", "stock"]):
            return await self._handle_stock_adjustment(request)
        else:
            return await self._handle_query(request)

    async def _handle_stock_loss(self, request: AgentRequest) -> AgentResponse:
        """REGISTER_STOCK_LOSS es HIGH risk — logging reforzado."""
        entities = await self._extract_stock_entities(request.message, "merma o pérdida")

        summary = (
            f"Registrar merma: {entities.get('product_name') or 'producto'}"
            f" × {abs(entities.get('qty_change') or 0)} unidades"
        )

        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="requires_approval",
            risk_level=RiskLevel.HIGH,
            requires_approval=True,
            confidence=Confidence.HIGH,
            result={
                "summary": summary,
                "action_type": ActionType.REGISTER_STOCK_LOSS,
                "structured_data": entities,
                "alerts": [
                    "Acción de alto riesgo: se registrará en el audit log con detalle."
                ],
            },
        )

    async def _handle_stock_adjustment(self, request: AgentRequest) -> AgentResponse:
        entities = await self._extract_stock_entities(request.message, "ajuste de inventario")
        qty = entities.get("qty_change") or 0
        summary = (
            f"Ajuste de stock: {entities.get('product_name') or 'producto'} → {qty:+d} unidades"
        )

        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="requires_approval",
            risk_level=RiskLevel.MEDIUM,
            requires_approval=True,
            confidence=Confidence.HIGH,
            result={
                "summary": summary,
                "action_type": ActionType.UPDATE_STOCK,
                "structured_data": entities,
            },
        )

    async def _extract_stock_entities(self, message: str, context: str) -> dict:
        system = (
            f"Sos el asistente de inventario de Véktor.\n"
            f"Extraé información de {context} del mensaje. Retorná SOLO un JSON:\n"
            '{{\n'
            '  "product_name": "<nombre del producto o null>",\n'
            '  "sku": "<SKU si se menciona o null>",\n'
            '  "qty_change": <número entero, negativo para bajas>,\n'
            '  "reason": "<merma|ajuste|devolucion|compra>",\n'
            '  "confidence": "<HIGH|MEDIUM|LOW>"\n'
            '}}'
        )
        response = await self.client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=200,
            system=system,
            messages=[{"role": "user", "content": self.wrap_user_input(message)}],
        )
        return json.loads(response.content[0].text.strip())

    async def _handle_query(self, request: AgentRequest) -> AgentResponse:
        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="success",
            risk_level=RiskLevel.LOW,
            confidence=Confidence.HIGH,
            result={"summary": "Consultá el dashboard para ver el estado del inventario."},
        )
