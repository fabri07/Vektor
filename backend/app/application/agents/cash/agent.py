"""AgentCash — registra y analiza movimientos monetarios."""

import json
import re
import uuid
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
    LLMCall,
    RiskLevel,
    UsageSummary,
)
from app.integrations.anthropic_client import get_anthropic_async_client

# Patrón determinístico para detectar montos explícitos en el mensaje del usuario.
# Matchea expresiones monetarias inequívocas. Los números sueltos de 3+ dígitos se
# aceptan como monto, pero _has_explicit_amount descarta años 19xx/20xx para no
# confundir "el 20 de abril de 2026" con un importe.
# NO matchea "3" en "3 cocas" (1-2 dígitos); SÍ matchea "5000" en "vendí 5000 al contado".
_MONETARY_RE = re.compile(
    r"\$\s*[\d.,]+"                    # $ seguido de número: $500, $30.000
    r"|\b\d{1,3}(?:[.\s]\d{3})+"      # miles con punto/espacio: 3.000, 30 000
    r"|\b\d+(?:,\d+)+"                # decimal con coma: 500,50
    r"|\b\d+\s*(?:pesos?|ars)\b"      # número + "pesos" / "ARS"
    r"|\b[1-9]\d{2,}\b",              # número ≥ 100 sin símbolo (3+ dígitos)
    re.IGNORECASE,
)


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

    async def _extract_sale_entities(self, message: str, business_context: dict[str, Any]) -> tuple[dict[str, Any], LLMCall]:
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
            source="agent_cash",
            model="claude-haiku-4-5",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        raw = response.content[0].text.strip() if response.content else ""
        try:
            return json.loads(raw), llm_call
        except Exception:
            return {"error": "No pude interpretar la venta. Intentá con monto y forma de pago."}, llm_call

    def _has_explicit_amount(self, message: str) -> bool:
        """True si el mensaje contiene una expresión monetaria inequívoca (no una cantidad de productos)."""
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
        """Parseo defensivo de quantity: acepta enteros, floats como '3.0', y strings numéricos."""
        if value is None:
            return 1
        try:
            return max(1, int(float(str(value))))
        except (ValueError, TypeError):
            return 1

    async def _lookup_product_price(
        self, product_description: str, tenant_id: str
    ) -> tuple[Decimal | None, str | None, str | None, list[str]]:
        """Retorna (sale_price_ars, nombre_exacto, product_id, alternativas).

        alternativas es una lista de nombres cuando hay múltiples coincidencias (producto ambiguo).
        En ese caso los tres primeros valores son None y el llamador debe pedir al usuario que especifique.

        Búsqueda en tres pasos: exacto normalizado → SKU → ilike por cada token significativo.
        Solo productos activos, con precio > 0 y provenance REAL.
        """
        if self._db is None:
            return None, None, None, []

        from sqlalchemy import select, func  # noqa: PLC0415

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
            Product.provenance == "REAL",
        ]

        # 1. Match exacto normalizado
        result = await self._db.execute(
            select(Product).where(
                *base_filters,
                func.lower(Product.name) == normalized_desc,
            ).limit(1)
        )
        product = result.scalar_one_or_none()

        # 2. Match por SKU
        if product is None:
            result = await self._db.execute(
                select(Product).where(
                    *base_filters,
                    Product.sku.isnot(None),
                    func.lower(Product.sku) == normalized_desc,
                ).limit(1)
            )
            product = result.scalar_one_or_none()

        # 3. ILIKE por cada token significativo (≥ 3 chars), en orden de longitud desc.
        #    Si hay múltiples coincidencias para un token → producto ambiguo → pedir clarificación.
        if product is None:
            tokens = sorted(
                (w for w in product_description.split() if len(w) >= 3),
                key=len,
                reverse=True,
            )
            if not tokens:
                tokens = [product_description]
            for keyword in tokens:
                matches_result = await self._db.execute(
                    select(Product).where(
                        *base_filters,
                        func.lower(Product.name).contains(keyword.lower()),
                    ).limit(5)
                )
                matches = list(matches_result.scalars().all())
                if len(matches) == 1:
                    product = matches[0]
                    break
                if len(matches) > 1:
                    # Múltiples coincidencias → ambiguo, devolver opciones
                    alternatives = [p.name for p in matches]
                    return None, None, None, alternatives

        if product is None:
            return None, None, None, []
        return product.sale_price_ars, product.name, str(product.id), []

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

    async def _maybe_build_uploaded_file_import(self, request: AgentRequest) -> AgentResponse | None:
        if self._db is None or not request.attachments:
            return None

        from sqlalchemy import select  # noqa: PLC0415

        from app.application.services.data_intent_extractor import (  # noqa: PLC0415
            DataIntentExtractor,
        )
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

            confirmed_fields = {
                "ventas": pre_check.intent_type in ("sale", "mixed"),
                "gastos": pre_check.intent_type in ("expense", "mixed"),
                "productos": pre_check.intent_type == "product",
            }
            rows = uploaded_file.parsed_summary_json.get("rows_processed", 0)
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_approval",
                risk_level=RiskLevel.MEDIUM,
                requires_approval=True,
                confidence=Confidence(pre_check.confidence),
                result={
                    "summary": (
                        f"Importar {rows} registros desde {uploaded_file.original_filename}."
                    ),
                    "action_type": ActionType.IMPORT_TABULAR_FILE,
                    "structured_data": {
                        "source": "uploaded_file",
                        "file_id": str(uploaded_file.id),
                        "confirmed_fields": confirmed_fields,
                    },
                    "alerts": [],
                },
            )
        return None

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
        # Usar word-boundary para evitar falsos positivos por substring
        # (ej: "gas" dentro de "gaseosa" no debe activar la detección de gasto)
        return has_amount and any(
            re.search(r"(?<!\w)" + re.escape(kw) + r"(?!\w)", message_lower)
            for kw in expense_keywords
        )

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
        file_import = await self._maybe_build_uploaded_file_import(request)
        if file_import is not None:
            return file_import

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

        # Resolver amount: solo confiar en el LLM si el mensaje contiene monto explícito.
        # Si el usuario no mencionó ningún número monetario, forzar None para evitar
        # que el LLM alucinando un importe puentee la consulta al catálogo de productos.
        if self._has_explicit_amount(request.message):
            amount = self._safe_decimal(entities.get("amount"))
        else:
            amount = None  # ignorar lo que devolvió el LLM; buscar en catálogo
        quantity = self._safe_int(entities.get("quantity"))
        product_desc = (entities.get("product_description") or "").strip()

        if amount is None and product_desc:
            price, matched_name, product_id, alternatives = await self._lookup_product_price(
                product_desc, request.business_id
            )
            if alternatives:
                # Múltiples productos coinciden → pedir que especifique
                # (limit=5 puede no ser exhaustivo; aclararlo si hay exactamente 5)
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

        # Si el medio de pago no fue mencionado, preguntar antes de confirmar
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

    async def on_confirmed_sale(self, sale_id: str, business_id: str) -> None:
        EventBus.emit("SALE_RECORDED", {"sale_id": sale_id, "business_id": business_id})
