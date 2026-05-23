"""AgentExpense — motor de egresos.

Responsabilidades:
- Registrar gastos operativos (REGISTER_EXPENSE)
- Registrar pagos y salidas de caja (REGISTER_CASH_OUTFLOW)
- Importar archivos de gastos (IMPORT_TABULAR_FILE)

Stage 2b: lógica real de egresos extraída de AgentCash.
AgentCash queda como shim delegante para PendingActions históricas.
"""

from __future__ import annotations

import re
import uuid
from calendar import monthrange
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Optional

from app.application.agents.base import BaseAgent
from app.application.agents.shared.schemas import (
    ActionType,
    AgentRequest,
    AgentResponse,
    Confidence,
    RiskLevel,
)

if TYPE_CHECKING:
    from redis.asyncio import Redis
    from sqlalchemy.ext.asyncio import AsyncSession


class AgentExpense(BaseAgent):
    agent_name = "agent_expense"

    def __init__(
        self,
        db: Optional["AsyncSession"] = None,
        redis: Optional["Redis"] = None,
        gateway: Any | None = None,
    ) -> None:
        self._db = db
        self._redis = redis
        self._gateway = gateway

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

        return today.isoformat()

    def _month_from_spanish(self, value: str) -> int:
        month_map = {
            "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
            "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
            "septiembre": 9, "setiembre": 9, "octubre": 10,
            "noviembre": 11, "diciembre": 12,
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

        # Branch: pago de deuda / salida de caja (REGISTER_CASH_OUTFLOW)
        if action_type == ActionType.REGISTER_CASH_OUTFLOW:
            return await self._handle_cash_outflow(request, task)

        # Branch: gasto operativo (REGISTER_EXPENSE o default)
        # Usar entities pre-extraídas del CEO si están disponibles
        pre_entities = getattr(task, "entities", {}) or {}
        if pre_entities.get("amount"):
            entities: dict[str, Any] = {
                "amount": str(pre_entities["amount"]),
                "category": pre_entities.get("category", "OTHER"),
                "description": pre_entities.get("description", "Gasto"),
                "transaction_date": pre_entities.get("transaction_date", date.today().isoformat()),
                "payment_method": pre_entities.get("payment_method", "other"),
                "is_recurring": pre_entities.get("is_recurring", False),
                "notes": request.message.strip(),
                "confidence": "HIGH",
            }
        else:
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

    async def _handle_cash_outflow(self, request: AgentRequest, task: Any | None) -> AgentResponse:
        """Maneja pagos de deudas / salidas de caja (REGISTER_CASH_OUTFLOW).

        Distinto de REGISTER_EXPENSE (gastos operativos recurrentes).
        Registra una salida de dinero puntual a proveedor, acreedor, etc.
        """
        pre_entities = getattr(task, "entities", {}) or {}
        amount = self._extract_amount(request.message)
        if amount is None and pre_entities.get("amount"):
            try:
                amount = Decimal(str(pre_entities["amount"]))
            except Exception:
                amount = None

        if amount is None:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.LOW,
                question="¿Cuánto fue el pago? Indicá el monto.",
                result={"summary": "Falta el monto para registrar la salida de caja."},
            )
        entities: dict[str, Any] = {
            "amount": str(amount),
            "transaction_date": pre_entities.get("transaction_date", date.today().isoformat()),
            "description": pre_entities.get("description", "Pago / salida de caja"),
            "payment_method": pre_entities.get("payment_method") or self._extract_payment_method(request.message),
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
                "summary": f"Registrar pago / salida de caja por ${amount}",
                "action_type": ActionType.REGISTER_CASH_OUTFLOW,
                "structured_data": entities,
                "alerts": [],
            },
        )
