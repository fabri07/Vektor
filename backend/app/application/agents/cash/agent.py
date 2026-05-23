"""AgentCash — shim de compatibilidad (Stage 2b).

Delega toda la lógica de negocio a AgentIncome (ingresos) o AgentExpense (egresos).
Retiene el check de Google Sheets ya que depende del gateway inyectado en el shim.
Solo existe para proteger PendingActions históricas con target_agent='agent_cash'.

Stage 5d: eliminar junto con el alias en registry.py.
"""

import re
from decimal import Decimal
from typing import Any, Optional

import anthropic  # noqa: F401  # compatibility: legacy tests patch cash.agent.anthropic
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.base import BaseAgent
from app.application.agents.shared.event_bus import EventBus
from app.application.agents.shared.schemas import ActionType, AgentRequest, AgentResponse, Confidence, RiskLevel


class AgentCash(BaseAgent):
    agent_name = "agent_cash"

    def __init__(
        self,
        db: Optional[AsyncSession] = None,
        redis: Optional[Redis] = None,
        gateway: Any | None = None,
    ) -> None:
        self._db = db
        self._redis = redis
        self._gateway = gateway

    # ── Helpers de dispatch legacy ─────────────────────────────────────────────

    def _extract_amount(self, message: str) -> Decimal | None:
        match = re.search(r"(?<!\d)(?:\$?\s*)?(\d{1,3}(?:[.\s]\d{3})+|\d+)(?:,\d{1,2})?", message)
        if match is None:
            return None
        normalized = match.group(1).replace(".", "").replace(" ", "")
        try:
            return Decimal(normalized)
        except Exception:
            return None

    def _looks_like_expense(self, message: str) -> bool:
        message_lower = message.lower()
        expense_keywords = (
            "pagué", "pague", "gasto", "egreso", "alquiler", "servicio",
            "luz", "gas", "internet", "agua", "sueldo", "mercadería",
            "mercaderia", "stock", "publicidad", "marketing", "proveedor",
        )
        has_amount = self._extract_amount(message) is not None
        return has_amount and any(
            re.search(r"(?<!\w)" + re.escape(kw) + r"(?!\w)", message_lower)
            for kw in expense_keywords
        )

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
        parsed_records = [dict(zip(headers, row, strict=False)) for row in rows]
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

    # ── Shim: process delega a Income o Expense ───────────────────────────────

    async def process(  # type: ignore[override]
        self,
        request: AgentRequest,
        task: Any | None = None,
    ) -> AgentResponse:
        # 1. Google Sheets import (depende del gateway inyectado en el shim)
        google_import = await self._maybe_build_google_sheets_import(request.message)
        if google_import is not None:
            google_import.request_id = request.request_id
            return google_import

        # 2. Dispatch por tipo de operación
        _expense_types = {ActionType.REGISTER_EXPENSE, ActionType.REGISTER_CASH_OUTFLOW}
        is_expense = (
            task is not None and getattr(task, "action_type", None) in _expense_types
        ) or (task is None and self._looks_like_expense(request.message))

        if is_expense:
            from app.application.agents.expense.agent import AgentExpense  # noqa: PLC0415
            delegate: BaseAgent = AgentExpense(db=self._db, redis=self._redis, gateway=self._gateway)
        else:
            from app.application.agents.income.agent import AgentIncome  # noqa: PLC0415
            delegate = AgentIncome(db=self._db, redis=self._redis, gateway=self._gateway)

        response = await delegate.process(request, task=task)
        response.agent_name = self.agent_name  # preservar "agent_cash" para audit log legacy
        return response

    async def on_confirmed_sale(self, sale_id: str, business_id: str) -> None:
        EventBus.emit("SALE_RECORDED", {"sale_id": sale_id, "business_id": business_id})
