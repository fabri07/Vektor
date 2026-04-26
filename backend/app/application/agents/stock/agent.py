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
from app.application.security.prompt_defense import wrap_user_input
from app.integrations.anthropic_client import get_anthropic_async_client
from app.persistence.models.product import Product


class StockAdjustEntity(BaseModel):
    product_id: Optional[str] = None
    sku: Optional[str] = None
    product_name: Optional[str] = None
    qty_change: int
    reason: str
    unit_cost: Optional[Decimal] = None


class AgentStock(BaseAgent):
    agent_name = "agent_stock"

    def __init__(self, db: Optional[AsyncSession] = None) -> None:
        self._client: Any | None = None
        self._db = db

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = get_anthropic_async_client(anthropic.AsyncAnthropic)
        return self._client

    @client.setter
    def client(self, value: Any) -> None:
        self._client = value

    async def _resolve_product_id(
        self,
        product_name: str | None,
        sku: str | None,
        tenant_id: str,
    ) -> tuple[str | None, list[str]]:
        """Busca el product_id en la DB. Devuelve (id, [nombres_alternativos])."""
        if self._db is None:
            return None, []

        tid = uuid.UUID(tenant_id)

        # 1. Búsqueda exacta por SKU
        if sku:
            result = await self._db.execute(
                select(Product).where(
                    Product.tenant_id == tid,
                    Product.sku == sku,
                )
            )
            product = result.scalar_one_or_none()
            if product:
                return str(product.id), []

        # 2. Búsqueda por nombre (ILIKE)
        if product_name:
            result = await self._db.execute(
                select(Product).where(
                    Product.tenant_id == tid,
                    Product.name.ilike(f"%{product_name}%"),
                )
            )
            matches = list(result.scalars().all())
            if len(matches) == 1:
                return str(matches[0].id), []
            if len(matches) > 1:
                return None, [m.name for m in matches[:5]]

        return None, []

    async def on_sale_recorded(
        self,
        sale_id: str,
        tenant_id: str,
        db: Optional[AsyncSession] = None,
    ) -> None:
        from app.application.services import stock_service  # noqa: PLC0415
        from app.observability.logger import get_logger  # noqa: PLC0415
        from app.persistence.models.transaction import SaleEntry  # noqa: PLC0415

        logger = get_logger(__name__)
        effective_db = db or self._db

        if effective_db is None:
            logger.warning("on_sale_recorded: no db session", sale_id=sale_id)
            return

        try:
            sale_uuid = uuid.UUID(sale_id)
            tenant_uuid = uuid.UUID(tenant_id)
        except ValueError:
            logger.warning("on_sale_recorded: invalid ids", sale_id=sale_id)
            return

        result = await effective_db.execute(
            select(SaleEntry).where(
                SaleEntry.id == sale_uuid,
                SaleEntry.tenant_id == tenant_uuid,
            )
        )
        sale = result.scalar_one_or_none()
        if sale is None or sale.product_id is None:
            return

        await stock_service.decrement_stock(
            product_id=sale.product_id,
            tenant_id=tenant_uuid,
            qty=sale.quantity,
            source_event_id=sale_id,
            db=effective_db,
        )

    async def detect_stockout(self, product_id: str, current_qty: int, min_threshold: int = 0) -> bool:
        return current_qty <= min_threshold

    async def detect_overstock(self, product_id: str, rotation_days: float, business_type: str) -> bool:
        config = HeuristicEngine.get(business_type)
        return config.is_overstock(rotation_days)

    async def generate_replenishment_ranking(self, tenant_id: str) -> list[dict]:
        return []

    async def _classify_stock_intent(self, message: str) -> str:
        system = (
            "Clasificá el mensaje en exactamente uno de estos intents de inventario:\n"
            "STOCK_LOSS: merma, pérdida, rotura, vencimiento, daño, desaparición de producto.\n"
            "STOCK_ADJUSTMENT: ajuste de inventario, conteo, corrección de stock.\n"
            "STOCK_QUERY: consulta sobre stock, disponibilidad, qué hay en inventario.\n\n"
            'Retorná SOLO un JSON: {"intent": "<STOCK_LOSS|STOCK_ADJUSTMENT|STOCK_QUERY>"}'
        )
        try:
            resp = await self.client.messages.create(
                model="claude-haiku-4-5",
                max_tokens=50,
                system=system,
                messages=[{"role": "user", "content": wrap_user_input(message)}],
            )
            result = json.loads(resp.content[0].text.strip())
            intent = result.get("intent", "STOCK_QUERY")
            if intent not in ("STOCK_LOSS", "STOCK_ADJUSTMENT", "STOCK_QUERY"):
                return "STOCK_QUERY"
            return intent
        except Exception:
            msg = message.lower()
            if any(w in msg for w in ["merma", "roto", "perdí", "vencido", "se rompió", "caducó", "dañado"]):
                return "STOCK_LOSS"
            if any(w in msg for w in ["ajuste", "conteo", "inventario", "corrección"]):
                return "STOCK_ADJUSTMENT"
            return "STOCK_QUERY"

    async def process(self, request: AgentRequest) -> AgentResponse:
        intent = await self._classify_stock_intent(request.message)

        if intent == "STOCK_LOSS":
            return await self._handle_stock_loss(request)
        elif intent == "STOCK_ADJUSTMENT":
            return await self._handle_stock_adjustment(request)
        else:
            return await self._handle_query(request)

    async def _handle_stock_loss(self, request: AgentRequest) -> AgentResponse:
        entities = await self._extract_stock_entities(request.message, "merma o pérdida")

        product_id, alternatives = await self._resolve_product_id(
            entities.get("product_name"),
            entities.get("sku"),
            request.business_id,
        )

        if product_id is None:
            return self._product_not_found_response(
                request, entities.get("product_name"), alternatives, ActionType.REGISTER_STOCK_LOSS
            )

        entities["product_id"] = product_id
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
                "alerts": ["Acción de alto riesgo: se registrará en el audit log con detalle."],
            },
        )

    async def _handle_stock_adjustment(self, request: AgentRequest) -> AgentResponse:
        entities = await self._extract_stock_entities(request.message, "ajuste de inventario")

        product_id, alternatives = await self._resolve_product_id(
            entities.get("product_name"),
            entities.get("sku"),
            request.business_id,
        )

        if product_id is None:
            return self._product_not_found_response(
                request, entities.get("product_name"), alternatives, ActionType.UPDATE_STOCK
            )

        entities["product_id"] = product_id
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

    def _product_not_found_response(
        self,
        request: AgentRequest,
        product_name: str | None,
        alternatives: list[str],
        action_type: ActionType,
    ) -> AgentResponse:
        if alternatives:
            names = ", ".join(f'"{n}"' for n in alternatives)
            question = (
                f"Encontré varios productos que coinciden con '{product_name}': {names}. "
                "¿Podés indicarme el nombre exacto o el SKU del producto?"
            )
        elif product_name:
            question = (
                f"No encontré el producto '{product_name}' en tu catálogo. "
                "Revisá el nombre exacto o el SKU e intentalo de nuevo."
            )
        else:
            question = "¿De qué producto se trata? Indicame el nombre exacto o el SKU."

        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="requires_clarification",
            risk_level=RiskLevel.LOW,
            confidence=Confidence.LOW,
            question=question,
            result={
                "action_type": action_type,
                "summary": "Producto no identificado en el catálogo.",
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
            model="claude-haiku-4-5",
            max_tokens=200,
            system=system,
            messages=[{"role": "user", "content": self.wrap_user_input(message)}],
        )
        raw = response.content[0].text.strip() if response.content else ""
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {"product_name": None, "sku": None, "qty_change": 0, "reason": "ajuste", "confidence": "LOW"}

    async def _handle_query(self, request: AgentRequest) -> AgentResponse:
        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="success",
            risk_level=RiskLevel.LOW,
            confidence=Confidence.HIGH,
            result={"summary": "Consultá el dashboard para ver el estado del inventario."},
        )
