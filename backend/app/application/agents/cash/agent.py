"""AgentCash — registra y analiza movimientos monetarios."""

import json
import re
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
from app.integrations.google_workspace.exceptions import WorkspaceTokenError
from app.integrations.google_workspace.gateway import GoogleWorkspaceGateway


_SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)|(?:[?&]id=)([a-zA-Z0-9-_]+)")


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

    def __init__(
        self,
        db: Optional[AsyncSession] = None,
        redis: Optional[Redis] = None,
        gateway: GoogleWorkspaceGateway | None = None,
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
            f"Sos el asistente de carga de ventas de Véktor.\n"
            f"Negocio: {business_context.get('name', 'el negocio')} "
            f"({business_context.get('type', 'kiosco_almacen')})\n\n"
            f"{heuristics.to_prompt_fragment()}\n\n"
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
            model="claude-haiku-4-5",
            max_tokens=300,
            system=system,
            messages=[{"role": "user", "content": self.wrap_user_input(message)}],
        )
        raw = response.content[0].text.strip() if response.content else ""
        try:
            result: dict[str, Any] = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            result = {"error": "No pude interpretar la respuesta. Intentá reformular el mensaje."}
        return result

    async def process(self, request: AgentRequest) -> AgentResponse:
        if self._looks_like_google_sheet_request(request.message):
            return await self._handle_google_sheet_import(request)

        # Cargar contexto de conversación si está disponible
        conversation_turns: list[dict[str, Any]] = []
        if request.conversation_id and self._db is not None and self._redis is not None:
            from app.application.services.conversation_service import ConversationService  # noqa: PLC0415
            svc = ConversationService(self._redis, self._db)
            ctx = await svc.get_context(request.conversation_id)
            conversation_turns = ctx.get("turns", [])

        business_context: dict[str, Any] = {"name": "el negocio", "type": "kiosco_almacen"}
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
            # Incluir historial previo en la pregunta de aclaración si hay turnos
            prior_context = ""
            if conversation_turns:
                prior_context = " (basado en tu mensaje anterior)"
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                question=f"¿La venta fue al contado o en cuenta corriente?{prior_context}",
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

    # ── Google Sheets import ─────────────────────────────────────────────────

    @staticmethod
    def _looks_like_google_sheet_request(message: str) -> bool:
        text = message.lower()
        return (
            ("google" in text or "sheet" in text or "hoja" in text)
            and any(word in text for word in ("import", "cargar", "registr", "analiz"))
        )

    @staticmethod
    def _extract_sheet_id(message: str) -> str | None:
        match = _SHEET_ID_RE.search(message)
        if not match:
            return None
        return match.group(1) or match.group(2)

    @staticmethod
    def _infer_record_type(message: str) -> str:
        text = message.lower()
        if any(word in text for word in ("gasto", "gastos", "egreso", "expense")):
            return "expenses"
        return "sales"

    @staticmethod
    def _normalize_header(value: str) -> str:
        return (
            value.lower()
            .strip()
            .replace(" ", "_")
            .replace("á", "a")
            .replace("é", "e")
            .replace("í", "i")
            .replace("ó", "o")
            .replace("ú", "u")
        )

    @classmethod
    def _parse_sheet_records(
        cls,
        values: list[list[str]],
        record_type: str,
    ) -> list[dict[str, Any]]:
        if len(values) < 2:
            return []

        headers = [cls._normalize_header(cell) for cell in values[0]]
        records: list[dict[str, Any]] = []
        amount_keys = {"monto", "amount", "total", "importe", "valor"}
        description_keys = {"descripcion", "description", "detalle", "producto", "concepto"}
        category_keys = {"categoria", "category", "rubro"}
        payment_keys = {"metodo_pago", "medio_pago", "payment_method", "pago"}

        for row in values[1:51]:
            mapped = {
                headers[idx]: cell.strip()
                for idx, cell in enumerate(row)
                if idx < len(headers) and cell.strip()
            }
            amount_raw = next((mapped[key] for key in amount_keys if key in mapped), "")
            amount_text = amount_raw.replace("$", "").replace(".", "").replace(",", ".").strip()
            try:
                amount = float(amount_text)
            except ValueError:
                continue
            if amount <= 0:
                continue

            description = next((mapped[key] for key in description_keys if key in mapped), None)
            if record_type == "expenses":
                category = next((mapped[key] for key in category_keys if key in mapped), "otros")
                records.append(
                    {
                        "amount": amount,
                        "category": category,
                        "description": description or category,
                    }
                )
            else:
                payment_method = next((mapped[key] for key in payment_keys if key in mapped), None)
                records.append(
                    {
                        "amount": amount,
                        "payment_status": "paid",
                        "payment_method": payment_method,
                        "product_description": description,
                    }
                )
        return records

    async def _handle_google_sheet_import(self, request: AgentRequest) -> AgentResponse:
        spreadsheet_id = self._extract_sheet_id(request.message)
        if not spreadsheet_id:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                question="Pasame el link de Google Sheets o el ID de la hoja que querés importar.",
            )

        if self._gateway is None:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                result={"reconnect_required": True, "reason": "not_connected", "app_id": "sheets"},
                question="Google Sheets no está conectado. Conectalo desde Aplicaciones y volvé a intentar.",
            )

        record_type = self._infer_record_type(request.message)
        try:
            sheets = await self._gateway.sheets()
            values = await self._gateway.run_google(
                sheets.read_values(spreadsheet_id=spreadsheet_id, range_name="A1:Z100")
            )
        except WorkspaceTokenError as exc:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                result={"reconnect_required": True, "reason": exc.reason, "app_id": "sheets"},
                question="Necesitás reconectar Google Sheets desde Aplicaciones para leer esa hoja.",
            )

        records = self._parse_sheet_records(values.values, record_type)
        if not records:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                question="No encontré filas con una columna de monto válida. Revisá que la hoja tenga encabezados como monto, total o importe.",
            )

        label = "gastos" if record_type == "expenses" else "ventas"
        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="requires_approval",
            risk_level=RiskLevel.MEDIUM,
            requires_approval=True,
            confidence=Confidence.MEDIUM,
            result={
                "summary": f"Encontré {len(records)} {label} para importar desde Google Sheets.",
                "action_type": ActionType.IMPORT_TABULAR_FILE,
                "structured_data": {
                    "source": "google_sheets",
                    "record_type": record_type,
                    "spreadsheet_id": spreadsheet_id,
                    "range": values.range,
                    "parsed_records": records,
                },
            },
        )
