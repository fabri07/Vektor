"""AgentStock — gestión de inventario en tiempo real."""

import json
import uuid
from decimal import Decimal
from typing import Any

import anthropic
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.base import BaseAgent
from app.application.agents.shared.heuristic_engine import HeuristicEngine
from app.application.agents.shared.product_resolver import resolve_product_id
from app.application.agents.shared.schemas import (
    ActionType,
    AgentRequest,
    AgentResponse,
    Confidence,
    LLMCall,
    RiskLevel,
    UsageSummary,
)
from app.application.security.prompt_defense import wrap_user_input
from app.integrations.anthropic_client import get_anthropic_async_client


class StockAdjustEntity(BaseModel):
    product_id: str | None = None
    sku: str | None = None
    product_name: str | None = None
    qty_change: int
    reason: str
    unit_cost: Decimal | None = None


class AgentStock(BaseAgent):
    agent_name = "agent_stock"

    def __init__(self, db: AsyncSession | None = None) -> None:
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
        if self._db is None:
            return None, []
        return await resolve_product_id(self._db, tenant_id, product_name, sku)

    async def on_sale_recorded(
        self,
        sale_id: str,
        tenant_id: str,
        db: AsyncSession | None = None,
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
                SaleEntry.voided_at.is_(None),
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

    async def detect_stockout(
        self,
        product_id: str,
        current_qty: int,
        min_threshold: int = 0,
    ) -> bool:
        return current_qty <= min_threshold

    async def detect_overstock(
        self,
        product_id: str,
        rotation_days: float,
        business_type: str,
    ) -> bool:
        config = HeuristicEngine.get(business_type)
        return config.is_overstock(rotation_days)

    async def generate_replenishment_ranking(self, tenant_id: str) -> list[dict]:
        return []

    async def _classify_stock_intent(self, message: str) -> tuple[str, LLMCall | None]:
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
            classify_call = LLMCall(
                source="agent_stock",
                model="claude-haiku-4-5",
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
            )
            result = json.loads(resp.content[0].text.strip())
            intent = result.get("intent", "STOCK_QUERY")
            if intent not in ("STOCK_LOSS", "STOCK_ADJUSTMENT", "STOCK_QUERY"):
                return "STOCK_QUERY", classify_call
            return intent, classify_call
        except Exception:
            msg = message.lower()
            if any(
                w in msg
                for w in ["merma", "roto", "perdí", "vencido", "se rompió", "caducó", "dañado"]
            ):
                return "STOCK_LOSS", None
            if any(w in msg for w in ["ajuste", "conteo", "inventario", "corrección"]):
                return "STOCK_ADJUSTMENT", None
            return "STOCK_QUERY", None

    async def process(self, request: AgentRequest) -> AgentResponse:
        intent, classify_call = await self._classify_stock_intent(request.message)
        all_calls: list[LLMCall] = [classify_call] if classify_call else []

        if intent == "STOCK_LOSS":
            response, extract_call = await self._handle_stock_loss(request)
            if extract_call:
                all_calls.append(extract_call)
        elif intent == "STOCK_ADJUSTMENT":
            response, extract_call = await self._handle_stock_adjustment(request)
            if extract_call:
                all_calls.append(extract_call)
        else:
            response = await self._handle_query(request)

        response.usage = UsageSummary(calls=all_calls) if all_calls else None
        return response

    async def _handle_stock_loss(self, request: AgentRequest) -> tuple[AgentResponse, LLMCall | None]:
        entities, extract_call = await self._extract_stock_entities(request.message, "merma o pérdida")

        product_id, alternatives = await self._resolve_product_id(
            entities.get("product_name"),
            entities.get("sku"),
            request.business_id,
        )

        if product_id is None:
            return self._product_not_found_response(
                request, entities.get("product_name"), alternatives, ActionType.REGISTER_STOCK_LOSS
            ), extract_call

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
        ), extract_call

    async def _handle_stock_adjustment(self, request: AgentRequest) -> tuple[AgentResponse, LLMCall | None]:
        entities, extract_call = await self._extract_stock_entities(request.message, "ajuste de inventario")

        product_id, alternatives = await self._resolve_product_id(
            entities.get("product_name"),
            entities.get("sku"),
            request.business_id,
        )

        if product_id is None:
            return self._product_not_found_response(
                request, entities.get("product_name"), alternatives, ActionType.UPDATE_STOCK
            ), extract_call

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
        ), extract_call

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

    async def _extract_stock_entities(self, message: str, context: str) -> tuple[dict, LLMCall]:
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
        extract_call = LLMCall(
            source="agent_stock",
            model="claude-haiku-4-5",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        raw = response.content[0].text.strip() if response.content else ""
        try:
            return json.loads(raw), extract_call
        except (json.JSONDecodeError, ValueError):
            return {
                "product_name": None,
                "sku": None,
                "qty_change": 0,
                "reason": "ajuste",
                "confidence": "LOW",
            }, extract_call

    async def _handle_query(self, request: AgentRequest) -> AgentResponse:
        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="success",
            risk_level=RiskLevel.LOW,
            confidence=Confidence.HIGH,
            result={"summary": "Consultá el dashboard para ver el estado del inventario."},
        )
