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
from typing import TYPE_CHECKING, Any

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
    transaction_date: str = Field(
        default_factory=lambda: __import__("datetime").date.today().isoformat()
    )
    payment_status: str
    payment_method: str | None = None
    product_description: str | None = None
    notes: str | None = None


class AgentIncome(BaseAgent):
    agent_name = "agent_income"

    def __init__(
        self,
        db: AsyncSession | None = None,
        redis: Redis | None = None,
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
            model="claude-sonnet-4-6",
            max_tokens=800,
            system=f"{system}\n\n{heuristics.to_prompt_fragment()}",
            messages=[{"role": "user", "content": self.wrap_user_input(message)}],
        )
        llm_call = LLMCall(
            source=self.agent_name,
            model="claude-sonnet-4-6",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        raw = response.content[0].text.strip() if response.content else ""
        try:
            return json.loads(raw), llm_call
        except Exception:
            return {
                "error": "No pude interpretar la venta. Intentá con monto y forma de pago."
            }, llm_call

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
            select(Product)
            .where(*base_filters, func.lower(Product.name) == normalized_desc)
            .limit(1)
        )
        product = result.scalar_one_or_none()

        if product is None:
            result = await self._db.execute(
                select(Product)
                .where(
                    *base_filters,
                    Product.sku.isnot(None),
                    func.lower(Product.sku) == normalized_desc,
                )
                .limit(1)
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
                    select(Product)
                    .where(*base_filters, func.lower(Product.name).contains(keyword.lower()))
                    .limit(5)
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

            summary_json = uploaded_file.parsed_summary_json
            filename = uploaded_file.original_filename

            # Detectar qué tipos de datos hay en el archivo directamente desde los arrays
            ventas_rows = summary_json.get("ventas_detectadas") or []
            gastos_rows = summary_json.get("gastos_detectados") or []
            stock_rows = summary_json.get("stock_detectado") or []
            is_multisheet = bool(summary_json.get("multi_sheet"))

            has_ventas = bool(ventas_rows)
            has_gastos = bool(gastos_rows)
            has_stock = bool(stock_rows)

            # Si ningún tipo de dato es reconocible → pedir aclaración al usuario
            if not has_ventas and not has_gastos and not has_stock:
                return AgentResponse(
                    request_id=request.request_id,
                    agent_name=self.agent_name,
                    status="requires_clarification",
                    risk_level=RiskLevel.LOW,
                    requires_approval=False,
                    confidence="LOW",
                    question=(
                        f"Adjuntaste «{filename}», pero no pude detectar el tipo de datos. "
                        "¿Qué contiene? Por ejemplo: ventas, gastos, productos u otro tipo."
                    ),
                    result={"summary": "Archivo sin tipo de datos detectado", "alerts": []},
                )

            # Construir confirmed_fields incluyendo TODO lo que hay en el archivo
            confirmed_fields = {
                "ventas": has_ventas,
                "gastos": has_gastos,
                "productos": has_stock,
            }

            # Construir resumen descriptivo con conteos reales por tipo
            parts: list[str] = []
            if has_ventas:
                parts.append(f"{len(ventas_rows):,} venta(s)".replace(",", "."))
            if has_gastos:
                parts.append(f"{len(gastos_rows):,} gasto(s)".replace(",", "."))
            if has_stock:
                parts.append(f"{len(stock_rows):,} producto(s)".replace(",", "."))

            if is_multisheet and summary_json.get("sheet_names"):
                sheets = ", ".join(f"«{s}»" for s in summary_json["sheet_names"])
                summary_text = (
                    f"Importar desde {filename} ({sheets}):\n"
                    + " · ".join(parts)
                )
            else:
                total = summary_json.get("rows_processed", len(ventas_rows) + len(gastos_rows))
                summary_text = f"Importar {total:,} registros desde {filename}.".replace(",", ".")

            inferred = summary_json.get("inferred_type", "general")
            confidence_val = summary_json.get("confidence", "MEDIUM")

            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_approval",
                risk_level=RiskLevel.MEDIUM,
                requires_approval=True,
                confidence=Confidence(confidence_val),
                result={
                    "summary": summary_text,
                    "action_type": ActionType.IMPORT_TABULAR_FILE,
                    "structured_data": {
                        "source": "uploaded_file",
                        "file_id": str(uploaded_file.id),
                        "confirmed_fields": confirmed_fields,
                        "inferred_type": inferred,
                    },
                    "alerts": [],
                },
            )
        return None

    async def process(
        self,
        request: AgentRequest,
        task: Any | None = None,
    ) -> AgentResponse:
        action_type = getattr(task, "action_type", None)
        analysis_intent = (getattr(task, "entities", {}) or {}).get("_intent")

        # ── Sprint 17: análisis read-only (NO importar — el usuario pidió analizar) ──
        if action_type == ActionType.ANALYZE_FILE:
            if analysis_intent == "limpiar_normalizar_archivo":
                return await self._handle_file_normalization(request)
            return await self._handle_file_analysis(request, analysis_intent)
        if action_type == ActionType.ANALYZE_SALES_DATA:
            return await self._handle_sales_analysis(request, analysis_intent)

        file_import = await self._maybe_build_uploaded_file_import(request)
        if file_import is not None:
            return file_import

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
            entities, sale_call = await self._extract_sale_entities(
                request.message, business_context
            )
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
        if (
            _pre_entities_used
            and entities.get("amount")
            or self._has_explicit_amount(request.message)
        ):
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
                question=(
                    "¿Cuál fue el medio de pago? (efectivo, débito, crédito, transferencia, " "QR)"
                ),
                result={
                    "summary": "Falta el medio de pago para completar el registro.",
                    "partial": entities,
                },
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
            next(
                (
                    m.group(0).replace("$", "").replace(".", "").replace(" ", "").strip()
                    for m in [__import__("re").search(r"\$?\s*[\d.,]+", request.message)]
                    if m
                ),
                None,
            )
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
            "transaction_date": pre_entities.get(
                "transaction_date", __import__("datetime").date.today().isoformat()
            ),
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

    # ── Sprint 17: handlers analíticos read-only ──────────────────────────────

    def _analysis_response(
        self,
        request: AgentRequest,
        summary: str,
        message: str,
        structured: dict[str, Any] | None = None,
        confidence: Confidence = Confidence.HIGH,
    ) -> AgentResponse:
        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="success",
            risk_level=RiskLevel.LOW,
            requires_approval=False,
            confidence=confidence,
            message=message,
            result={"summary": summary, "structured_data": structured or {}, "analysis": True},
        )

    async def _tenant_uuid(self, request: AgentRequest) -> uuid.UUID | None:
        try:
            return uuid.UUID(request.business_id)
        except (ValueError, TypeError):
            return None

    async def _load_attachment_summaries(
        self, request: AgentRequest
    ) -> list[tuple[str, dict[str, Any]]]:
        if self._db is None or not request.attachments:
            return []
        from sqlalchemy import select  # noqa: PLC0415

        from app.persistence.models.file import UploadedFile  # noqa: PLC0415

        tenant_id = await self._tenant_uuid(request)
        if tenant_id is None:
            return []
        out: list[tuple[str, dict[str, Any]]] = []
        for attachment in request.attachments:
            file_id = attachment.get("file_id") if isinstance(attachment, dict) else None
            if not file_id:
                continue
            try:
                file_uuid = uuid.UUID(str(file_id))
            except (ValueError, TypeError):
                continue
            result = await self._db.execute(
                select(UploadedFile).where(
                    UploadedFile.id == file_uuid,
                    UploadedFile.tenant_id == tenant_id,
                )
            )
            f = result.scalar_one_or_none()
            if f is not None and isinstance(f.parsed_summary_json, dict):
                out.append((f.original_filename, f.parsed_summary_json))
        return out

    async def _handle_file_analysis(
        self, request: AgentRequest, intent: str | None
    ) -> AgentResponse:
        """Describe el archivo adjunto: tipo, columnas, calidad y acción sugerida.

        Cubre analizar_archivo_cargado, detectar_tipo_archivo, resumen_ejecutivo_archivo.
        (limpiar_normalizar_archivo tiene su propio handler: _handle_file_normalization.)
        """
        from app.application.services.data_intent_extractor import (  # noqa: PLC0415
            DataIntentExtractor,
        )
        from app.application.services.file_parsing import (  # noqa: PLC0415
            summary_columns,
            summary_row_count,
        )

        summaries = await self._load_attachment_summaries(request)
        if not summaries:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.MEDIUM,
                question=(
                    "No veo ningún archivo adjunto para analizar. "
                    "Subí un Excel, CSV o PDF y te digo qué contiene y qué puedo hacer con él."
                ),
                result={"summary": "Sin archivo para analizar."},
            )

        filename, summary = summaries[0]
        columns = summary_columns(summary)
        row_count = summary_row_count(summary)
        inferred = summary.get("inferred_type") or summary.get("file_type") or "general"
        columns_at_risk = summary.get("columns_at_risk") or []
        pre_check = DataIntentExtractor().check_file_summary(summary)

        _type_label = {
            "ventas": "ventas",
            "gastos": "gastos",
            "stock": "productos/stock",
            "spreadsheet": "planilla",
            "general": "datos generales",
        }
        type_label = _type_label.get(str(inferred), str(inferred))

        lines = [f"Analicé «{filename}»: parece un archivo de {type_label}."]
        if isinstance(row_count, int):
            lines.append(f"Tiene {row_count} filas y {len(columns)} columnas.")
        if columns:
            lines.append("Columnas: " + ", ".join(columns[:8]) + ("…" if len(columns) > 8 else ""))

        # Calidad / limpieza
        if columns_at_risk:
            names = ", ".join(
                c.get("column", "?") if isinstance(c, dict) else str(c) for c in columns_at_risk[:5]
            )
            lines.append(
                f"⚠ Columnas con muchos vacíos: {names}. Conviene limpiarlas antes de importar."
            )
        # Acción sugerida
        if pre_check.has_data_intent:
            destino = {
                "sale": "ventas",
                "expense": "gastos",
                "product": "productos",
                "mixed": "ventas y gastos",
            }.get(pre_check.intent_type or "", "datos")
            lines.append(
                f"Puedo importarlo como {destino}. Decime 'importá este archivo' y lo cargo "
                "con tu confirmación."
            )
        else:
            lines.append(
                "No reconocí datos importables claros (ventas, gastos o productos). "
                "Si me decís qué representa, te ayudo a usarlo."
            )

        return self._analysis_response(
            request,
            "analizar_archivo_cargado",
            "\n".join(lines),
            {
                "filename": filename,
                "inferred_type": inferred,
                "row_count": row_count,
                "columns": columns,
                "columns_at_risk": columns_at_risk,
                "importable": pre_check.has_data_intent,
                "data_intent_type": pre_check.intent_type,
            },
        )

    async def _handle_file_normalization(self, request: AgentRequest) -> AgentResponse:
        """limpiar_normalizar_archivo: reporta columnas riesgosas y la política de imputación.

        Es honesto sobre lo que Véktor hace: no muta el archivo en el chat, pero
        describe concretamente qué columnas conviene limpiar (umbral 35% de nulos,
        de `file_parsing.flag_columns_at_risk`) y la estrategia de imputación que se
        aplica en la ingesta (`impute_column`: cantidad→0, numéricos→mediana,
        categóricos→None), y ofrece las acciones de limpieza disponibles.
        """
        from app.application.services.file_parsing import (  # noqa: PLC0415
            NULL_COLUMN_WARN_THRESHOLD,
        )

        summaries = await self._load_attachment_summaries(request)
        if not summaries:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.MEDIUM,
                question=(
                    "No veo un archivo adjunto para limpiar. Subí el Excel o CSV y te digo "
                    "qué columnas conviene normalizar antes de importar."
                ),
                result={"summary": "Sin archivo para normalizar."},
            )

        filename, summary = summaries[0]
        columns_at_risk = summary.get("columns_at_risk") or []
        umbral = int(NULL_COLUMN_WARN_THRESHOLD * 100)

        if not columns_at_risk:
            return self._analysis_response(
                request,
                "normalizar_archivo_limpio",
                f"«{filename}» está prolijo: ninguna columna supera el {umbral}% de datos "
                "vacíos. Podés importarlo directamente — al cargar, Véktor completa los "
                "huecos menores automáticamente (cantidades en 0, valores numéricos por la "
                "mediana de la columna y categorías vacías quedan sin asignar).",
            )

        lines = [
            f"Revisé «{filename}» y encontré {len(columns_at_risk)} columna(s) con más del "
            f"{umbral}% de datos vacíos:"
        ]
        for c in columns_at_risk[:8]:
            if isinstance(c, dict):
                col = c.get("column", "?")
                pct = c.get("null_pct")
                pct_str = (
                    f"{pct * 100:.0f}% vacío" if isinstance(pct, int | float) else "muchos vacíos"
                )
                lines.append(f"- {col}: {pct_str}")
            else:
                lines.append(f"- {c}")
        lines.append(
            "Al importar, Véktor normaliza los huecos automáticamente: cantidades faltantes "
            "→ 0, montos faltantes → mediana de la columna, categorías vacías → sin asignar. "
            "Si una columna casi no tiene datos, conviene eliminarla ('eliminá la columna X') "
            "o completar los datos antes de cargar."
        )
        return self._analysis_response(
            request,
            "limpiar_normalizar_archivo",
            "\n".join(lines),
            {"filename": filename, "columns_at_risk": columns_at_risk, "threshold_pct": umbral},
        )

    async def _handle_sales_analysis(
        self, request: AgentRequest, intent: str | None
    ) -> AgentResponse:
        """Análisis de ventas: rentabilidad, productos estrella/problemáticos, ticket.

        Cubre analizar_rentabilidad_ventas, detectar_productos_estrella,
        detectar_productos_problematicos, analizar_ticket_promedio, analizar_descuentos
        y la familia Clientes (stub).
        """
        # ── Familia Clientes — stub (diferido a Sprint 18) ────────────────────
        if intent in (
            "analizar_clientes",
            "detectar_clientes_inactivos",
            "analizar_cuentas_por_cobrar",
            "priorizar_cobranza",
        ):
            return self._analysis_response(
                request,
                "clientes_stub",
                "Para analizar clientes necesito que tus ventas tengan identificado el "
                "cliente. Hoy Véktor todavía no registra ese dato. Apenas habilitemos el "
                "campo 'cliente' en las ventas, voy a poder mostrarte quiénes son tus "
                "mejores clientes, cuentas por cobrar y a quién priorizar en la cobranza.",
                confidence=Confidence.MEDIUM,
            )

        tenant_id = await self._tenant_uuid(request)
        if self._db is None or tenant_id is None:
            return self._analysis_response(
                request,
                "ventas_sin_datos",
                "Necesito acceso a tus ventas para analizarlas. Cargá ventas y vuelvo a intentar.",
            )

        # ── analizar_ticket_promedio → DeterministicFinance ───────────────────
        if intent == "analizar_ticket_promedio":
            from app.application.services.deterministic_finance import (  # noqa: PLC0415
                calcular_ticket_promedio,
            )

            ticket = await calcular_ticket_promedio(tenant_id, self._db)
            if ticket.get("sin_datos"):
                return self._analysis_response(
                    request,
                    "ticket_sin_datos",
                    "Todavía no tengo ventas registradas en los últimos 30 días para "
                    "calcular tu ticket promedio.",
                )
            return self._analysis_response(
                request,
                "analizar_ticket_promedio",
                f"Tu ticket promedio de los últimos 30 días es ${ticket['ticket_promedio']} "
                f"sobre {ticket['n_transacciones']} ventas.",
                {"ticket": str(ticket["ticket_promedio"]), "n": ticket["n_transacciones"]},
            )

        # ── analizar_descuentos → requiere custom_fields ──────────────────────
        if intent == "analizar_descuentos":
            return self._analysis_response(
                request,
                "descuentos_sin_datos",
                "Para analizar descuentos necesito que registres el descuento aplicado en "
                "cada venta (por ejemplo, con un campo personalizado 'descuento'). "
                "Configuralo en Ajustes → Campos personalizados y después analizo su impacto "
                "en tu margen.",
                confidence=Confidence.MEDIUM,
            )

        # ── rentabilidad / estrella / problemáticos → JOIN ventas × productos ─
        from datetime import date as _date  # noqa: PLC0415
        from datetime import timedelta as _timedelta

        from app.application.agents.shared import analytics  # noqa: PLC0415
        from app.persistence.repositories.product_repository import (  # noqa: PLC0415
            ProductRepository,
        )
        from app.persistence.repositories.transaction_repository import (  # noqa: PLC0415
            SaleRepository,
        )

        to_date = _date.today()
        from_date = to_date - _timedelta(days=30)
        sales = await SaleRepository(self._db).get_sales_by_product(tenant_id, from_date, to_date)
        if not sales:
            return self._analysis_response(
                request,
                "ventas_sin_productos",
                "No tengo ventas con producto identificado en los últimos 30 días. "
                "Cuando registres ventas asociadas a productos, te muestro cuáles son los "
                "más rentables.",
            )
        products = await ProductRepository(self._db).get_products_with_margin(tenant_id)
        joined = analytics.join_sales_with_products(sales, products)

        _, warning_pct = await self._get_margin_config(tenant_id)
        classed = analytics.classify_star_vs_problem(joined, warning_pct)

        if intent == "detectar_productos_estrella":
            stars = classed["stars"]
            if not stars:
                return self._analysis_response(
                    request,
                    "estrella_ninguno",
                    "Todavía no identifico productos estrella claros (alto volumen y buen "
                    "margen). Puede faltar costo cargado en tus productos más vendidos.",
                )
            lines = ["Tus productos estrella (más vendidos y con buen margen):"]
            for r in stars[:8]:
                mp = f"{r['margin_pct']:.0f}%" if r["margin_pct"] is not None else "s/margen"
                lines.append(f"- {r['name']}: ${r['revenue']:.0f} facturados, margen {mp}")
            return self._analysis_response(
                request,
                "detectar_productos_estrella",
                "\n".join(lines),
                {"stars": stars},
            )

        if intent == "detectar_productos_problematicos":
            problems = classed["problems"]
            if not problems:
                return self._analysis_response(
                    request,
                    "problematicos_ninguno",
                    "No detecté productos problemáticos: los de mayor volumen tienen un "
                    "margen aceptable. Bien ahí.",
                )
            lines = [f"Productos de alto volumen pero margen bajo (< {warning_pct:.0f}%):"]
            for r in problems[:8]:
                lines.append(
                    f"- {r['name']}: ${r['revenue']:.0f} facturados, margen {r['margin_pct']:.0f}%"
                )
            lines.append("Conviene revisar su precio o costo: venden mucho pero dejan poco.")
            return self._analysis_response(
                request,
                "detectar_productos_problematicos",
                "\n".join(lines),
                {"problems": problems},
            )

        # ── analizar_rentabilidad_ventas (default) ────────────────────────────
        rated = [r for r in joined if r["estimated_profit"] is not None]
        lines = [f"Analicé {len(joined)} productos vendidos en los últimos 30 días."]
        top_rev = joined[:5]
        lines.append(
            "Más facturación: "
            + ", ".join(f"{r['name']} (${r['revenue']:.0f})" for r in top_rev if r["name"])
        )
        if rated:
            top_profit = sorted(rated, key=lambda r: r["estimated_profit"], reverse=True)[:5]
            lines.append(
                "Más ganancia estimada: "
                + ", ".join(
                    f"{r['name']} (${r['estimated_profit']:.0f})" for r in top_profit if r["name"]
                )
            )
            no_cost = len(joined) - len(rated)
            if no_cost:
                lines.append(
                    f"{no_cost} producto(s) sin costo cargado — no puedo estimar su ganancia."
                )
        else:
            lines.append(
                "No puedo estimar ganancias porque tus productos no tienen costo cargado. "
                "Cargá el costo unitario y calculo la rentabilidad real."
            )
        return self._analysis_response(
            request,
            "analizar_rentabilidad_ventas",
            "\n".join(lines),
            {"by_product": joined[:30]},
        )

    async def _get_margin_config(self, tenant_id: uuid.UUID) -> tuple[float, float]:
        from app.application.services.health_config_service import (  # noqa: PLC0415
            HealthConfigService,
        )

        assert self._db is not None  # garantizado por el handler que lo invoca
        try:
            cfg = await HealthConfigService(self._db).get(tenant_id)
            return cfg.target_margin_pct, cfg.warning_margin_pct
        except Exception:
            return 40.0, 25.0
