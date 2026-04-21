"""AgentCash — registra y analiza movimientos monetarios."""

import json
import re
from calendar import monthrange
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

    def __init__(
        self,
        db: Optional[AsyncSession] = None,
        redis: Optional[Redis] = None,
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

    async def _maybe_build_google_sheets_import(self, message: str) -> AgentResponse | None:
        if self._gateway is None or "docs.google.com/spreadsheets/d/" not in message.lower():
            return None

        match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", message)
        if match is None or not hasattr(self._gateway, "read_values"):
            return None

        spreadsheet_id = match.group(1)
        result = await self._gateway.read_values(spreadsheet_id, "Sheet1")
        values = getattr(result, "values", []) or []
        headers = values[0] if values else []
        rows = values[1:] if len(values) > 1 else []
        parsed_records = [
            dict(zip(headers, row, strict=False))
            for row in rows
        ]

        return AgentResponse(
            request_id="",
            agent_name=self.agent_name,
            status="requires_approval",
            risk_level=RiskLevel.MEDIUM,
            requires_approval=True,
            confidence=Confidence.HIGH,
            result={
                "summary": "Importar ventas desde Google Sheets.",
                "action_type": ActionType.IMPORT_TABULAR_FILE,
                "structured_data": {
                    "source": "google_sheets",
                    "record_type": "sales",
                    "spreadsheet_id": spreadsheet_id,
                    "parsed_records": parsed_records,
                },
                "alerts": [],
            },
        )

    def _looks_like_expense(self, message: str) -> bool:
        message_lower = message.lower()
        expense_keywords = (
            "pagué",
            "pague",
            "gasto",
            "egreso",
            "alquiler",
            "servicio",
            "luz",
            "gas",
            "internet",
            "agua",
            "sueldo",
            "mercadería",
            "mercaderia",
            "stock",
            "publicidad",
            "marketing",
            "proveedor",
        )
        has_amount = self._extract_amount(message) is not None
        return has_amount and any(keyword in message_lower for keyword in expense_keywords)

    def _extract_amount(self, message: str) -> Decimal | None:
        match = re.search(r"(?<!\d)(?:\$?\s*)?(\d{1,3}(?:[.\s]\d{3})+|\d+)(?:,\d{1,2})?", message)
        if match is None:
            return None
        normalized = match.group(1).replace(".", "").replace(" ", "")
        try:
            return Decimal(normalized)
        except Exception:
            return None

    def _extract_payment_method(self, message: str) -> str:
        message_lower = message.lower()
        if "transfer" in message_lower:
            return "transfer"
        if "debito" in message_lower:
            return "debit_card"
        if "credito" in message_lower:
            return "credit_card"
        if "qr" in message_lower:
            return "qr"
        if "efectivo" in message_lower:
            return "cash"
        return "other"

    def _extract_expense_category(self, message: str) -> tuple[str, str]:
        message_lower = message.lower()
        category_rules = (
            ("RENT", ("alquiler", "renta"), "Alquiler"),
            ("UTILITIES", ("luz", "gas", "internet", "agua", "servicio"), "Servicios"),
            ("PAYROLL", ("sueldo", "sueldos", "empleado", "personal", "nomina", "nómina"), "Sueldos"),
            ("INVENTORY", ("mercadería", "mercaderia", "stock", "proveedor", "compra"), "Mercadería / stock"),
            ("MARKETING", ("marketing", "publicidad", "anuncio", "ads"), "Marketing"),
        )
        for canonical, keywords, label in category_rules:
            if any(keyword in message_lower for keyword in keywords):
                return canonical, label
        return "OTHER", "Gasto"

    def _extract_transaction_date(self, message: str) -> str:
        message_lower = message.lower()
        today = date.today()

        iso_match = re.search(r"\b(20\d{2})-(\d{2})-(\d{2})\b", message_lower)
        if iso_match is not None:
            return iso_match.group(0)

        day_month_year = re.search(
            r"\b(\d{1,2})\s+de\s+"
            r"(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre)"
            r"\s+de\s+(20\d{2})\b",
            message_lower,
        )
        if day_month_year is not None:
            day = int(day_month_year.group(1))
            month = self._month_from_spanish(day_month_year.group(2))
            year = int(day_month_year.group(3))
            return date(year, month, day).isoformat()

        month_year = re.search(
            r"\b(enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre)"
            r"\s+de\s+(20\d{2})\b",
            message_lower,
        )
        if month_year is not None:
            month = self._month_from_spanish(month_year.group(1))
            year = int(month_year.group(2))
            last_day = monthrange(year, month)[1]
            return date(year, month, min(today.day, last_day)).isoformat()

        if "este mes" in message_lower:
            return today.isoformat()

        return today.isoformat()

    def _month_from_spanish(self, value: str) -> int:
        month_map = {
            "enero": 1,
            "febrero": 2,
            "marzo": 3,
            "abril": 4,
            "mayo": 5,
            "junio": 6,
            "julio": 7,
            "agosto": 8,
            "septiembre": 9,
            "setiembre": 9,
            "octubre": 10,
            "noviembre": 11,
            "diciembre": 12,
        }
        return month_map[value]

    def _extract_expense_entities(self, message: str) -> dict[str, Any]:
        amount = self._extract_amount(message)
        if amount is None:
            return {"error": "No pude identificar el monto del gasto. Probá con algo como 'Pagué alquiler $450.000'."}

        category, label = self._extract_expense_category(message)
        return {
            "amount": str(amount),
            "category": category,
            "description": label,
            "transaction_date": self._extract_transaction_date(message),
            "payment_method": self._extract_payment_method(message),
            "is_recurring": any(token in message.lower() for token in ("mensual", "cada mes", "alquiler")),
            "notes": message.strip(),
            "confidence": "HIGH",
        }

    async def process(self, request: AgentRequest) -> AgentResponse:
        google_import = await self._maybe_build_google_sheets_import(request.message)
        if google_import is not None:
            google_import.request_id = request.request_id
            return google_import

        if self._looks_like_expense(request.message):
            entities = self._extract_expense_entities(request.message)
            if "error" in entities:
                return AgentResponse(
                    request_id=request.request_id,
                    agent_name=self.agent_name,
                    status="requires_clarification",
                    risk_level=RiskLevel.LOW,
                    confidence=Confidence.LOW,
                    question=entities["error"],
                    result={"summary": "Faltan datos para registrar el gasto."},
                )

            amount = entities["amount"]
            description = entities.get("description", "gasto")
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="success",
                risk_level=RiskLevel.MEDIUM,
                requires_approval=False,
                confidence=Confidence.HIGH,
                result={
                    "summary": f"Gasto registrado: {description} por ${amount}",
                    "action_type": ActionType.REGISTER_EXPENSE,
                    "structured_data": entities,
                    "alerts": [],
                    "auto_execute": True,
                },
            )

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
