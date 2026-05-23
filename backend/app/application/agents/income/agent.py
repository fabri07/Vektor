"""AgentIncome — motor de ingresos.

Responsabilidades:
- Registrar ventas (REGISTER_SALE)
- Registrar cobros (REGISTER_CASH_INFLOW)
- Importar archivos de ventas (IMPORT_TABULAR_FILE)

Stage 2b: lógica real de ingresos extraída de AgentCash.
AgentCash queda como shim delegante para PendingActions históricas.
"""

from __future__ import annotations

import json
import re
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Optional

import anthropic
from pydantic import BaseModel, Field

from app.application.agents.base import BaseAgent
from app.application.agents.shared.event_bus import EventBus
from app.application.agents.shared.heuristic_engine import HeuristicEngine
from app.application.agents.shared.schemas import (
    ActionType,
    AgentRequest,
    AgentResponse,
    Confidence,
    LLMCall,
    RiskLevel,
    UsageSummary,
)
from app.integrations.anthropic_client import get_anthropic_async_client

if TYPE_CHECKING:
    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import AsyncSession

_MONETARY_RE = re.compile(
    r"\$\s*[\d.,]+"
    r"|\b\d{1,3}(?:[.\s]\d{3})+"
    r"|\b\d+(?:,\d+)+"
    r"|\b\d+\s*(?:pesos?|ars)\b"
    r"|\b[1-9]\d{2,}\b",
    re.IGNORECASE,
)


class SaleEntity(BaseModel):
    amount: Decimal
    transaction_date: str = Field(default_factory=lambda: __import__("datetime").date.today().isoformat())
    payment_status: str
    payment_method: Optional[str] = None
    product_description: Optional[str] = None
    notes: Optional[str] = None


class AgentIncome(BaseAgent):
    agent_name = "agent_income"

    def __init__(
        self,
        db: Optional["AsyncSession"] = None,
        redis: Optional["Redis"] = None,
        gateway: Any | None = None,
    ) -> None:
        self._client: Any | None = None
        self._db = db
        self._redis = redis
        self._gateway = gateway

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = get_anthropic_async_client(anthropic.AsyncAnthropic)
        return self._client

    @client.setter
    def client(self, value: Any) -> None:
        self._client = value

    def _has_explicit_amount(self, message: str) -> bool:
        for match in _MONETARY_RE.finditer(message):
            token = match.group(0).strip()
            if re.fullmatch(r"\d{3,}", token):
                value = int(token)
                if 1900 <= value <= 2099:
                    continue
            return True
        return False

    def _safe_decimal(self, value: object) -> Decimal | None:
        if value is None or value == "" or str(value).lower() == "null":
            return None
        try:
            d = Decimal(str(value))
            return d if d > 0 else None
        except Exception:
            return None

    def _safe_int(self, value: object) -> int:
        if value is None:
            return 1
        try:
            return max(1, int(float(str(value))))
        except (ValueError, TypeError):
            return 1

    async def _extract_sale_entities(
        self, message: str, business_context: dict[str, Any]
    ) -> tuple[dict[str, Any], LLMCall]:
        heuristics = HeuristicEngine.get(business_context.get("type", "kiosco_almacen"))
        system = (
            "Extraé del mensaje los datos de una venta y devolvé SOLO JSON con: "
            "amount (número o null si no se menciona en el mensaje), "
            "quantity (entero, default 1), date, "
            "payment_status, "
            "payment_method (null si el usuario NO lo menciona explícitamente — "
            "NUNCA asumir ni inferir efectivo u otro método), "
            "product_description y confidence."
        )
        response = await self.client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=300,
            system=f"{system}\n\n{heuristics.to_prompt_fragment()}",
            messages=[{"role": "user", "content": self.wrap_user_input(message)}],
        )
        llm_call = LLMCall(
            source=self.agent_name,
            model="claude-haiku-4-5",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        raw = response.content[0].text.strip() if response.content else ""
        try:
            return json.loads(raw), llm_call
        except Exception:
            return {"error": "No pude interpretar la venta. Intentá con monto y forma de pago."}, llm_call

    async def _lookup_product_price(
        self, product_description: str, tenant_id: str
    ) -> tuple[Decimal | None, str | None, str | None, list[str]]:
        if self._db is None:
            return None, None, None, []

        from sqlalchemy import func, select  # noqa: PLC0415

        from app.persistence.models.product import Product  # noqa: PLC0415

        try:
            import uuid as uuid_mod
            tenant_uuid = uuid_mod.UUID(str(tenant_id))
        except ValueError:
            return None, None, None, []

        normalized_desc = product_description.strip().lower()
        base_filters = [
            Product.tenant_id == tenant_uuid,
            Product.is_active.is_(True),
            Product.sale_price_ars > 0,
        ]

        result = await self._db.execute(
            select(Product).where(*base_filters, func.lower(Product.name) == normalized_desc).limit(1)
        )
        product = result.scalar_one_or_none()

        if product is None:
            result = await self._db.execute(
                select(Product).where(
                    *base_filters, Product.sku.isnot(None), func.lower(Product.sku) == normalized_desc
                ).limit(1)
            )
            product = result.scalar_one_or_none()

        if product is None:
            tokens = sorted(
                (w for w in product_description.split() if len(w) >= 3), key=len, reverse=True
            )
            if not tokens:
                tokens = [product_description]
            for keyword in tokens:
                matches_result = await self._db.execute(
                    select(Product).where(
                        *base_filters, func.lower(Product.name).contains(keyword.lower())
                    ).limit(5)
                )
                matches = list(matches_result.scalars().all())
                if len(matches) == 1:
                    product = matches[0]
                    break
                if len(matches) > 1:
                    return None, None, None, [p.name for p in matches]

        if product is None:
            return None, None, None, []
        return product.sale_price_ars, product.name, str(product.id), []

    async def _maybe_build_uploaded_file_import(
        self, request: AgentRequest
    ) -> AgentResponse | None:
        if self._db is None or not request.attachments:
            return None

        from sqlalchemy import select  # noqa: PLC0415

        from app.application.services.data_intent_extractor import DataIntentExtractor  # noqa: PLC0415
        from app.persistence.models.file import UploadedFile  # noqa: PLC0415

        for attachment in request.attachments:
            file_id = attachment.get("file_id") if isinstance(attachment, dict) else None
            if not file_id:
                continue
            try:
                file_uuid = uuid.UUID(str(file_id))
                tenant_uuid = uuid.UUID(str(request.business_id))
            except ValueError:
                continue
            result = await self._db.execute(
                select(UploadedFile).where(
                    UploadedFile.id == file_uuid,
                    UploadedFile.tenant_id == tenant_uuid,
                )
            )
            uploaded_file = result.scalar_one_or_none()
            if uploaded_file is None or not isinstance(uploaded_file.parsed_summary_json, dict):
                continue
            pre_check = DataIntentExtractor().check_file_summary(uploaded_file.parsed_summary_json)
            if not pre_check.has_data_intent:
                continue
            rows = uploaded_file.parsed_summary_json.get("rows_processed", 0)
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_approval",
                risk_level=RiskLevel.MEDIUM,
                requires_approval=True,
                confidence=Confidence(pre_check.confidence),
                result={
                    "summary": f"Importar {rows} registros desde {uploaded_file.original_filename}.",
                    "action_type": ActionType.IMPORT_TABULAR_FILE,
                    "structured_data": {
                        "source": "uploaded_file",
                        "file_id": str(uploaded_file.id),
                        "confirmed_fields": {
                            "ventas": pre_check.intent_type in ("sale", "mixed"),
                            "gastos": pre_check.intent_type in ("expense", "mixed"),
                            "productos": pre_check.intent_type == "product",
                        },
                    },
                    "alerts": [],
                },
            )
        return None

    async def process(  # type: ignore[override]
        self,
        request: AgentRequest,
        task: Any | None = None,
    ) -> AgentResponse:
        file_import = await self._maybe_build_uploaded_file_import(request)
        if file_import is not None:
            return file_import

        action_type = getattr(task, "action_type", None)

        # Branch: cobro / inflow (REGISTER_CASH_INFLOW)
        if action_type == ActionType.REGISTER_CASH_INFLOW:
            return await self._handle_cash_inflow(request, task)

        # Branch: venta (REGISTER_SALE o default)
        # Usar entities pre-extraídas del CEO si están disponibles y son suficientes
        pre_entities = getattr(task, "entities", {}) or {}
        _pre_entities_used = bool(pre_entities.get("amount") and pre_entities.get("payment_method"))
        if _pre_entities_used:
            entities = pre_entities
            usage = UsageSummary()
        else:
            business_context = {"name": "el negocio", "type": "kiosco_almacen"}
            entities, sale_call = await self._extract_sale_entities(request.message, business_context)
            usage = UsageSummary(calls=[sale_call])

        if "error" in entities:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                question=entities["error"],
                usage=usage,
            )

        if entities.get("payment_status") == "unknown":
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                question="¿La venta fue al contado o en cuenta corriente?",
                usage=usage,
            )

        # Si las entities vienen del CEO, el amount ya está determinado — saltear check del mensaje.
        # El check de `_has_explicit_amount` solo aplica cuando el LLM extrajo del texto.
        if _pre_entities_used and entities.get("amount"):
            amount = self._safe_decimal(entities.get("amount"))
        elif self._has_explicit_amount(request.message):
            amount = self._safe_decimal(entities.get("amount"))
        else:
            amount = None
        quantity = self._safe_int(entities.get("quantity"))
        product_desc = (entities.get("product_description") or "").strip()

        if amount is None and product_desc:
            price, matched_name, product_id, alternatives = await self._lookup_product_price(
                product_desc, request.business_id
            )
            if alternatives:
                opts = ", ".join(alternatives[:5])
                suffix = " (entre otros)" if len(alternatives) >= 5 else ""
                return AgentResponse(
                    request_id=request.request_id,
                    agent_name=self.agent_name,
                    status="requires_clarification",
                    risk_level=RiskLevel.LOW,
                    confidence=Confidence.LOW,
                    question=(
                        f"Encontré varios productos que podrían ser '{product_desc}'{suffix}: "
                        f"{opts}. ¿A cuál te referís?"
                    ),
                    result={
                        "summary": "Producto ambiguo — se necesita especificar.",
                        "partial": {**entities, "quantity": quantity},
                    },
                    usage=usage,
                )
            if price is not None:
                amount = price * Decimal(str(quantity))
                entities["amount"] = str(amount)
                entities["quantity"] = quantity
                entities["unit_price"] = str(price)
                entities["product_description"] = matched_name or product_desc
                entities["product_id"] = product_id
                entities["price_lookup_source"] = "products_db"
            else:
                return AgentResponse(
                    request_id=request.request_id,
                    agent_name=self.agent_name,
                    status="requires_clarification",
                    risk_level=RiskLevel.LOW,
                    confidence=Confidence.LOW,
                    question=(
                        f"No encontré el precio de '{product_desc}' en tu catálogo. "
                        "¿Cuál fue el importe total de la venta?"
                    ),
                    result={"summary": "Falta el importe para registrar la venta."},
                    usage=usage,
                )
        elif amount is None:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.LOW,
                question="No pude identificar el monto de la venta. ¿Cuánto fue el total?",
                result={"summary": "Falta el importe para registrar la venta."},
                usage=usage,
            )
        else:
            entities["amount"] = str(amount)
            entities["quantity"] = quantity

        payment_method_raw = (entities.get("payment_method") or "").strip().lower()
        if not payment_method_raw or payment_method_raw in ("null", "none", "other", "otro"):
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.MEDIUM,
                question="¿Cuál fue el medio de pago? (efectivo, débito, crédito, transferencia, QR)",
                result={"summary": "Falta el medio de pago para completar el registro.", "partial": entities},
                usage=usage,
            )

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
            usage=usage,
        )

    async def _handle_cash_inflow(self, request: AgentRequest, task: Any | None) -> AgentResponse:
        """Maneja cobros de deudas / ingresos extra (REGISTER_CASH_INFLOW).

        No es una venta nueva — es el cobro de algo ya registrado o un ingreso extraordinario.
        Extrae monto, descripción y método de pago sin LLM (extracción de texto determinística).
        """
        pre_entities = getattr(task, "entities", {}) or {}
        amount_raw = pre_entities.get("amount") or self._safe_decimal(
            next((m.group(0).replace("$", "").replace(".", "").replace(" ", "").strip()
                  for m in [__import__("re").search(r"\$?\s*[\d.,]+", request.message)] if m), None)
        )
        amount = self._safe_decimal(amount_raw)
        if amount is None:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.LOW,
                question="¿Cuánto cobró? Indicá el monto del ingreso.",
                result={"summary": "Falta el monto para registrar el cobro."},
            )
        entities: dict[str, Any] = {
            "amount": str(amount),
            "transaction_date": pre_entities.get("transaction_date", __import__("datetime").date.today().isoformat()),
            "description": pre_entities.get("description", "Cobro registrado"),
            "payment_method": pre_entities.get("payment_method"),
            "notes": request.message.strip(),
        }
        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="requires_approval",
            risk_level=RiskLevel.MEDIUM,
            requires_approval=True,
            confidence=Confidence.HIGH,
            result={
                "summary": f"Registrar cobro por ${amount}",
                "action_type": ActionType.REGISTER_CASH_INFLOW,
                "structured_data": entities,
                "alerts": [],
            },
        )

    async def on_confirmed_sale(self, sale_id: str, business_id: str) -> None:
        EventBus.emit("SALE_RECORDED", {"sale_id": sale_id, "business_id": business_id})
