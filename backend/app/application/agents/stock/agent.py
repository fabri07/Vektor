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
from app.application.agents.shared.llm_safe import call_llm
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
from app.domain.product import effective_threshold
from app.integrations.anthropic_client import get_anthropic_async_client
from app.observability.logger import get_logger


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

    async def generate_replenishment_ranking(self, tenant_id: str) -> list[dict[str, Any]]:
        return []

    async def _classify_stock_intent(self, message: str) -> tuple[str, LLMCall | None]:
        system = (
            "Clasificá el mensaje en exactamente uno de estos intents de inventario:\n"
            "STOCK_LOSS: merma, pérdida, rotura, vencimiento, daño, desaparición de producto.\n"
            "STOCK_ADJUSTMENT: ajuste de inventario, conteo, corrección de stock.\n"
            "PRODUCT_UPDATE: cambiar precio, costo, umbral de stock bajo, activar/desactivar "
            "un producto, renombrar, editar categoría, modificar descripción, cambiar estado "
            "(aclarar que estado es derivado), ajustar stock a valor específico.\n"
            "STOCK_QUERY: consulta sobre stock, disponibilidad, qué hay en inventario.\n\n"
            'Retorná SOLO un JSON: {"intent": '
            '"<STOCK_LOSS|STOCK_ADJUSTMENT|PRODUCT_UPDATE|STOCK_QUERY>"}'
        )
        raw, classify_call = await call_llm(
            client=self.client,
            source="agent_stock",
            model="claude-sonnet-4-6",
            system=system,
            messages=[{"role": "user", "content": wrap_user_input(message)}],
            max_tokens=300,
        )
        if raw is not None:
            try:
                parsed = json.loads(raw)
                intent = parsed.get("intent", "STOCK_QUERY")
                if intent in ("STOCK_LOSS", "STOCK_ADJUSTMENT", "PRODUCT_UPDATE", "STOCK_QUERY"):
                    return intent, classify_call
            except (json.JSONDecodeError, ValueError):
                pass
        # Fallback determinístico por palabras clave
        msg = message.lower()
        if any(
            w in msg for w in ["merma", "roto", "perdí", "vencido", "se rompió", "caducó", "dañado"]
        ):
            return "STOCK_LOSS", classify_call
        if any(w in msg for w in ["ajuste", "conteo", "inventario", "corrección"]):
            return "STOCK_ADJUSTMENT", classify_call
        if any(
            w in msg
            for w in [
                "precio",
                "costo",
                "umbral",
                "activar",
                "desactivar",
                "renombrar",
                "cambiar nombre",
                "cambiar estado",
                "editar",
                "modificar",
            ]
        ):
            return "PRODUCT_UPDATE", classify_call
        return "STOCK_QUERY", classify_call

    async def process(self, request: AgentRequest, task: Any | None = None) -> AgentResponse:
        # ── Sprint 17: análisis read-only (sin LLM de clasificación) ──────────
        action_type = getattr(task, "action_type", None)
        analysis_intent = (getattr(task, "entities", {}) or {}).get("_intent")
        if action_type == ActionType.ANALYZE_PRICES:
            return await self._handle_price_analysis(request, analysis_intent)
        if action_type == ActionType.ANALYZE_STOCK_DATA:
            return await self._handle_stock_analysis(request, analysis_intent)

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
        elif intent == "PRODUCT_UPDATE":
            response, extract_call = await self._handle_product_update(request)
            if extract_call:
                all_calls.append(extract_call)
        else:
            response = await self._handle_query(request)

        response.usage = UsageSummary(calls=all_calls) if all_calls else None
        return response

    async def _handle_stock_loss(
        self, request: AgentRequest
    ) -> tuple[AgentResponse, LLMCall | None]:
        entities, extract_call = await self._extract_stock_entities(
            request.message, "merma o pérdida"
        )

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

    async def _handle_stock_adjustment(
        self, request: AgentRequest
    ) -> tuple[AgentResponse, LLMCall | None]:
        entities, extract_call = await self._extract_stock_entities(
            request.message, "ajuste de inventario"
        )

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

    async def _extract_product_update_entities(
        self, message: str
    ) -> tuple[dict[str, Any], LLMCall]:
        system = (
            "Sos el asistente de inventario de Véktor.\n"
            "Extraé del mensaje los campos a actualizar en un producto. Retorná SOLO un JSON:\n"
            "{{\n"
            '  "product_name": "<nombre o descripción del producto para identificarlo, o null>",\n'
            '  "sku": "<SKU actual del producto para identificarlo, o null — NO el nuevo SKU>",\n'
            '  "new_sku": "<nuevo SKU si el usuario pide cambiar el SKU, o null>",\n'
            '  "sale_price_ars": <nuevo precio de venta o null>,\n'
            '  "unit_cost_ars": <nuevo costo unitario o null>,\n'
            '  "low_stock_threshold_units": <nuevo umbral de pocas unidades o null>,\n'
            '  "is_active": <true|false|null — solo si se menciona activar/desactivar>,\n'
            '  "name": "<nuevo nombre o null — solo si se pide renombrar>",\n'
            '  "category": "<nueva categoría o null>",\n'
            '  "stock_units": <valor absoluto de stock o null — solo si se pide ajustar a un '
            "número exacto>,\n"
            '  "wants_state_change": <true si solo pide cambiar estado sin especificar cómo>,\n'
            '  "confidence": "<HIGH|MEDIUM|LOW>"\n'
            "}}"
        )
        response = await self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            system=system,
            messages=[{"role": "user", "content": self.wrap_user_input(message)}],
        )
        extract_call = LLMCall(
            source="agent_stock",
            model="claude-sonnet-4-6",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        raw = response.content[0].text.strip() if response.content else ""
        try:
            return json.loads(raw), extract_call
        except (json.JSONDecodeError, ValueError):
            return {"product_name": None, "sku": None, "confidence": "LOW"}, extract_call

    async def _handle_product_update(
        self, request: AgentRequest
    ) -> tuple[AgentResponse, LLMCall | None]:
        entities, extract_call = await self._extract_product_update_entities(request.message)

        # Si el usuario solo quiere "cambiar el estado" sin saber que es derivado → aclarar
        if entities.get("wants_state_change") and not any(
            entities.get(f)
            for f in (
                "sale_price_ars",
                "unit_cost_ars",
                "low_stock_threshold_units",
                "is_active",
                "name",
                "category",
                "stock_units",
            )
        ):
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.MEDIUM,
                question=(
                    "El estado del producto (En stock / Pocas unidades / Sin stock) se calcula "
                    "automáticamente según las unidades disponibles y el umbral configurado. "
                    "¿Querés que te ayude a ajustar el stock o el umbral de pocas unidades?"
                ),
                result={"action_type": ActionType.UPDATE_PRODUCT, "summary": "Estado derivado."},
            ), extract_call

        product_id, alternatives = await self._resolve_product_id(
            entities.get("product_name"),
            entities.get("sku"),  # sku como identificador (no como campo a actualizar)
            request.business_id,
        )

        if product_id is None:
            return self._product_not_found_response(
                request, entities.get("product_name"), alternatives, ActionType.UPDATE_PRODUCT
            ), extract_call

        # Construir payload solo con campos mencionados
        # new_sku se mapea a "sku" para actualizar el campo en el producto
        _updatable = (
            "sale_price_ars",
            "unit_cost_ars",
            "low_stock_threshold_units",
            "is_active",
            "name",
            "category",
            "stock_units",
        )
        update_fields = {k: entities[k] for k in _updatable if entities.get(k) is not None}
        if entities.get("new_sku"):
            update_fields["sku"] = entities["new_sku"]

        if not update_fields:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.LOW,
                question=(
                    "¿Qué campo querés modificar? Por ejemplo: precio, costo, umbral de stock "
                    "bajo, activar o desactivar."
                ),
                result={
                    "action_type": ActionType.UPDATE_PRODUCT,
                    "summary": "Sin campos a actualizar.",
                },
            ), extract_call

        fields_desc = ", ".join(f"{k}={v}" for k, v in update_fields.items())
        summary = f"Actualizar {entities.get('product_name') or 'producto'}: {fields_desc}"
        update_fields["product_id"] = product_id

        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="requires_approval",
            risk_level=RiskLevel.MEDIUM,
            requires_approval=True,
            confidence=Confidence.HIGH,
            result={
                "summary": summary,
                "action_type": ActionType.UPDATE_PRODUCT,
                "structured_data": update_fields,
            },
        ), extract_call

    async def _extract_stock_entities(
        self, message: str, context: str
    ) -> tuple[dict[str, Any], LLMCall | None]:
        system = (
            f"Sos el asistente de inventario de Véktor.\n"
            f"Extraé información de {context} del mensaje. Retorná SOLO un JSON:\n"
            "{{\n"
            '  "product_name": "<nombre del producto o null>",\n'
            '  "sku": "<SKU si se menciona o null>",\n'
            '  "qty_change": <número entero, negativo para bajas>,\n'
            '  "reason": "<merma|ajuste|devolucion|compra>",\n'
            '  "confidence": "<HIGH|MEDIUM|LOW>"\n'
            "}}"
        )
        _fallback: dict[str, Any] = {
            "product_name": None,
            "sku": None,
            "qty_change": 0,
            "reason": "ajuste",
            "confidence": "LOW",
        }
        raw, extract_call = await call_llm(
            client=self.client,
            source="agent_stock",
            model="claude-sonnet-4-6",
            system=system,
            messages=[{"role": "user", "content": self.wrap_user_input(message)}],
            max_tokens=600,
        )
        if raw is None:
            return _fallback, extract_call
        try:
            return json.loads(raw), extract_call
        except (json.JSONDecodeError, ValueError):
            return _fallback, extract_call

    async def _handle_query(self, request: AgentRequest) -> AgentResponse:
        logger = get_logger(__name__)
        if self._db is None:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="success",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.LOW,
                result={"summary": "SIN_DATOS", "message": "Sin acceso a base de datos."},
            )

        try:
            tenant_id = uuid.UUID(request.business_id)
        except ValueError:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="error",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.LOW,
                result={"summary": "SIN_DATOS", "message": "Tenant inválido."},
            )

        from app.persistence.models.product import Product  # noqa: PLC0415

        rows = await self._db.execute(
            select(Product).where(
                Product.tenant_id == tenant_id,
                Product.is_active.is_(True),
            )
        )
        products = list(rows.scalars().all())

        if not products:
            logger.info("agent_stock.query.empty", tenant_id=str(tenant_id))
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="success",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.HIGH,
                result={
                    "summary": "SIN_PRODUCTOS",
                    "message": "No hay productos cargados en el catálogo todavía.",
                    "total_products": 0,
                },
            )

        out_of_stock = [p for p in products if p.stock_units == 0]
        low_stock = [
            p
            for p in products
            if p.stock_units > 0
            and p.stock_units <= effective_threshold(p.low_stock_threshold_units)
        ]
        ok_stock = [
            p for p in products if p.stock_units > effective_threshold(p.low_stock_threshold_units)
        ]

        # Top 5 más críticos (sin stock primero, luego por unidades ascendente)
        critical = sorted(out_of_stock + low_stock, key=lambda p: p.stock_units)[:5]
        critical_list = [
            {
                "name": p.name,
                "stock": p.stock_units,
                "threshold": effective_threshold(p.low_stock_threshold_units),
            }
            for p in critical
        ]

        logger.info(
            "agent_stock.query",
            tenant_id=str(tenant_id),
            total=len(products),
            out_of_stock=len(out_of_stock),
            low_stock=len(low_stock),
            ok=len(ok_stock),
            used_fallback=sum(1 for p in products if p.low_stock_threshold_units is None),
        )

        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="success",
            risk_level=RiskLevel.LOW,
            confidence=Confidence.HIGH,
            result={
                "summary": "stock_query",
                "total_products": len(products),
                "in_stock": len(ok_stock),
                "low_stock": len(low_stock),
                "out_of_stock": len(out_of_stock),
                "critical_products": critical_list,
                "period": "realtime",
            },
        )

    # ── Sprint 17: handlers analíticos read-only (sin LLM, determinísticos) ────

    def _analysis_response(
        self,
        request: AgentRequest,
        summary: str,
        message: str,
        structured: dict[str, Any] | None = None,
        confidence: Confidence = Confidence.HIGH,
    ) -> AgentResponse:
        """Respuesta de análisis LOW-risk: status success, sin aprobación."""
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

    def _empty_catalog_response(self, request: AgentRequest) -> AgentResponse:
        return self._analysis_response(
            request,
            summary="SIN_PRODUCTOS",
            message=(
                "Todavía no tenés productos cargados en el catálogo. "
                "Subí tu lista de precios o cargá productos para que pueda analizar "
                "márgenes, stock y reposición."
            ),
            confidence=Confidence.HIGH,
        )

    async def _tenant_uuid(self, request: AgentRequest) -> uuid.UUID | None:
        try:
            return uuid.UUID(request.business_id)
        except (ValueError, TypeError):
            return None

    async def _business_vertical(self, tenant_id: uuid.UUID) -> str:
        """Devuelve el vertical_code del tenant o fallback a kiosco_almacen."""
        if self._db is None:
            return "kiosco_almacen"
        from app.persistence.models.business import BusinessProfile  # noqa: PLC0415

        result = await self._db.execute(
            select(BusinessProfile.vertical_code).where(BusinessProfile.tenant_id == tenant_id)
        )
        code = result.scalar_one_or_none()
        return code or "kiosco_almacen"

    async def _get_margin_config(self, tenant_id: uuid.UUID) -> tuple[float, float]:
        """Devuelve (target_margin_pct, warning_margin_pct) del tenant o defaults vertical."""
        from app.application.services.health_config_service import (  # noqa: PLC0415
            HealthConfigService,
        )

        assert self._db is not None  # garantizado por el handler que lo invoca
        try:
            cfg = await HealthConfigService(self._db).get(tenant_id)
            return cfg.target_margin_pct, cfg.warning_margin_pct
        except Exception:
            return 40.0, 25.0

    async def _load_attachment_summaries(
        self, request: AgentRequest
    ) -> list[tuple[str, dict[str, Any]]]:
        """Carga (filename, parsed_summary_json) de cada adjunto del mensaje actual."""
        if self._db is None or not request.attachments:
            return []
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

    @staticmethod
    def _summary_product_rows(summary: dict[str, Any]) -> list[dict[str, Any]]:
        """Extrae filas de producto del summary (stock_detectado o ventas_detectadas)."""
        _buckets = ("stock_detectado", "ventas_detectadas", "gastos_detectados", "otros_detectados")
        for key in _buckets:
            rows = summary.get(key)
            if isinstance(rows, list) and rows:
                return [r for r in rows if isinstance(r, dict)]
        return []

    @staticmethod
    def _extract_price_map(
        rows: list[dict[str, Any]], price_keys: tuple[str, ...] | None = None
    ) -> dict[str, float]:
        """Construye {nombre_normalizado: precio} a partir de filas heterogéneas.

        `price_keys` permite priorizar la columna de costo (para detectar aumentos
        de proveedor) vs precio de venta. Usa `parse_money` type-aware para no
        corromper floats ni formato argentino.
        """
        from app.application.agents.shared.analytics import parse_money  # noqa: PLC0415

        _name_keys = ("nombre", "producto", "name", "descripcion", "detalle", "articulo")
        _price_keys = price_keys or (
            "precio",
            "precio_venta",
            "price",
            "pvp",
            "importe",
            "valor",
            "costo",
        )
        out: dict[str, float] = {}
        for r in rows:
            lowered = {str(k).strip().lower(): v for k, v in r.items()}
            name = next((str(lowered[k]) for k in _name_keys if lowered.get(k)), None)
            price_raw = next((lowered[k] for k in _price_keys if lowered.get(k) is not None), None)
            if not name:
                continue
            price = parse_money(price_raw)
            if price is None:
                continue
            out[name.strip().lower()] = price
        return out

    async def _handle_price_list_comparison(self, request: AgentRequest) -> AgentResponse:
        """Compara dos listas de precios adjuntas por nombre de producto."""
        from app.application.agents.shared import analytics  # noqa: PLC0415

        summaries = await self._load_attachment_summaries(request)
        if len(summaries) < 2:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.MEDIUM,
                question=(
                    "Para comparar listas necesito dos archivos adjuntos: la lista "
                    "anterior y la nueva. Subí ambos y los comparo por producto."
                ),
                result={"summary": "Faltan dos listas para comparar."},
            )
        old_map = self._extract_price_map(self._summary_product_rows(summaries[0][1]))
        new_map = self._extract_price_map(self._summary_product_rows(summaries[1][1]))
        diff = analytics.diff_price_lists(old_map, new_map)
        if not diff:
            return self._analysis_response(
                request,
                "comparar_listas_sin_match",
                "No encontré productos en común entre las dos listas para comparar. "
                "Verificá que los nombres de producto coincidan en ambos archivos.",
            )
        subieron = [d for d in diff if d["delta_abs"] > 0]
        subieron.sort(key=lambda d: d["delta_pct"] or 0, reverse=True)
        lines = [f"Comparé {len(diff)} productos en común entre las dos listas."]
        if subieron:
            avg = round(sum(d["delta_pct"] or 0 for d in subieron) / len(subieron), 1)
            lines.append(f"{len(subieron)} subieron de precio (promedio +{avg}%).")
            for d in subieron[:8]:
                lines.append(
                    f"- {d['key']}: ${d['old_price']:.0f} → ${d['new_price']:.0f} "
                    f"(+{d['delta_pct']:.0f}%)"
                )
        return self._analysis_response(
            request,
            "comparar_listas_precios",
            "\n".join(lines),
            {"diff": diff},
        )

    @staticmethod
    def _fuzzy_match(
        name: str, candidates: dict[str, dict[str, Any]], threshold: int = 88
    ) -> dict[str, Any] | None:
        """Matchea `name` contra un dict {nombre_normalizado: producto} con rapidfuzz.

        Devuelve el producto del catálogo si hay match exacto o fuzzy >= threshold,
        o None. Prioriza match exacto.
        """
        from rapidfuzz import fuzz  # noqa: PLC0415

        if name in candidates:
            return candidates[name]
        best: dict[str, Any] | None = None
        best_score = 0.0
        for cand_name, prod in candidates.items():
            score = fuzz.token_sort_ratio(name, cand_name)
            if score > best_score:
                best_score, best = score, prod
        return best if best_score >= threshold else None

    async def _catalog_by_name(self, tenant_id: uuid.UUID) -> dict[str, dict[str, Any]]:
        """Catálogo activo indexado por nombre normalizado, con margen ya calculado."""
        from app.persistence.repositories.product_repository import (  # noqa: PLC0415
            ProductRepository,
        )

        assert self._db is not None  # garantizado por el handler que lo invoca
        products = await ProductRepository(self._db).get_products_with_margin(tenant_id)
        return {str(p["name"]).strip().lower(): p for p in products}

    async def _maybe_handle_price_list_attachment(
        self, request: AgentRequest, tenant_id: uuid.UUID
    ) -> AgentResponse | None:
        """Analiza una lista de precios adjunta vs el catálogo.

        Cruza por fuzzy match (rapidfuzz) contra el catálogo, calcula el margen
        donde hay `unit_cost_ars`, y detecta precios en $0 y nombres duplicados.
        Retorna None si no hay adjunto con filas de producto (el caller cae al
        análisis de márgenes del catálogo).
        """
        summaries = await self._load_attachment_summaries(request)
        rows: list[dict[str, Any]] = []
        for _name, summary in summaries:
            rows = self._summary_product_rows(summary)
            if rows:
                break
        if not rows:
            return None

        price_map = self._extract_price_map(rows)
        if not price_map:
            return self._analysis_response(
                request,
                "lista_precios_sin_precios",
                "Pude leer el archivo pero no identifiqué una columna de precios clara. "
                "Asegurate de que tenga una columna 'precio' (y opcionalmente 'costo').",
            )

        # Precios en 0 y duplicados
        zeros = [name for name, price in price_map.items() if price <= 0]
        seen: dict[str, int] = {}
        for r in rows:
            lowered = {str(k).strip().lower(): v for k, v in r.items()}
            name = next(
                (
                    str(lowered[k])
                    for k in ("nombre", "producto", "name", "descripcion")
                    if lowered.get(k)
                ),
                None,
            )
            if name:
                key = name.strip().lower()
                seen[key] = seen.get(key, 0) + 1
        duplicates = [name for name, c in seen.items() if c > 1]

        # Cruce fuzzy con catálogo + cálculo de margen donde hay costo
        catalog = await self._catalog_by_name(tenant_id)
        matched: list[dict[str, Any]] = []
        new_items: list[str] = []
        low_margin: list[dict[str, Any]] = []
        for name, list_price in price_map.items():
            prod = self._fuzzy_match(name, catalog)
            if prod is None:
                new_items.append(name)
                continue
            unit_cost = prod.get("unit_cost")
            margin_pct = None
            if unit_cost is not None and list_price > 0:
                margin_pct = round((list_price - unit_cost) / list_price * 100, 1)
            entry = {
                "list_name": name,
                "catalog_name": prod.get("name"),
                "list_price": list_price,
                "unit_cost": unit_cost,
                "margin_pct": margin_pct,
            }
            matched.append(entry)
            if margin_pct is not None and margin_pct < 0:
                low_margin.append(entry)

        lines = [f"Leí una lista con {len(price_map)} productos."]
        lines.append(
            f"{len(matched)} coinciden con tu catálogo (incluyendo similares); "
            f"{len(new_items)} serían nuevos."
        )
        con_costo = [m for m in matched if m["margin_pct"] is not None]
        if con_costo:
            avg = round(sum(m["margin_pct"] for m in con_costo) / len(con_costo), 1)
            lines.append(f"Margen promedio con los precios de la lista: {avg:.0f}%.")
        if low_margin:
            ejemplos = ", ".join(m["catalog_name"] for m in low_margin[:4])
            lines.append(
                f"⚠ {len(low_margin)} producto(s) quedarían con margen NEGATIVO "
                f"(precio < costo): {ejemplos}."
            )
        if zeros:
            lines.append(
                f"⚠ {len(zeros)} producto(s) con precio en $0 — revisalos antes de importar."
            )
        if duplicates:
            lines.append(
                f"⚠ {len(duplicates)} nombre(s) duplicado(s): {', '.join(duplicates[:5])}."
            )
        lines.append("Si querés, decime 'importá esta lista' y la cargo al catálogo.")
        return self._analysis_response(
            request,
            "analizar_lista_precios",
            "\n".join(lines),
            {
                "total": len(price_map),
                "matched": len(matched),
                "new_items": len(new_items),
                "zeros": zeros,
                "duplicates": duplicates,
                "negative_margin": low_margin,
            },
        )

    async def _handle_supplier_price_increase(
        self, request: AgentRequest, tenant_id: uuid.UUID
    ) -> AgentResponse | None:
        """Detecta aumentos del proveedor: precios del adjunto vs `unit_cost_ars` del catálogo.

        Los precios del archivo del proveedor se interpretan como COSTOS nuevos y se
        comparan contra el costo unitario actual del catálogo (fuzzy match).
        Retorna None si no hay adjunto con filas de producto.
        """
        summaries = await self._load_attachment_summaries(request)
        rows: list[dict[str, Any]] = []
        for _name, summary in summaries:
            rows = self._summary_product_rows(summary)
            if rows:
                break
        if not rows:
            return None

        # Priorizar columna de costo; si no hay, usar precio como proxy del costo nuevo
        new_costs = self._extract_price_map(
            rows,
            price_keys=("costo", "precio_costo", "cost", "precio", "price", "importe", "valor"),
        )
        if not new_costs:
            return self._analysis_response(
                request,
                "aumentos_sin_precios",
                "Leí el archivo del proveedor pero no encontré una columna de costo/precio "
                "para comparar. Asegurate de que tenga una columna 'costo' o 'precio'.",
            )

        catalog = await self._catalog_by_name(tenant_id)
        if not catalog:
            return self._empty_catalog_response(request)

        increases: list[dict[str, Any]] = []
        for name, new_cost in new_costs.items():
            prod = self._fuzzy_match(name, catalog)
            if prod is None:
                continue
            old_cost = prod.get("unit_cost")
            if old_cost is None or old_cost <= 0 or new_cost <= 0:
                continue
            delta_pct = round((new_cost - old_cost) / old_cost * 100, 1)
            if delta_pct > 0.5:  # umbral mínimo para considerar "aumento"
                increases.append(
                    {
                        "name": prod.get("name"),
                        "old_cost": old_cost,
                        "new_cost": new_cost,
                        "delta_pct": delta_pct,
                    }
                )

        if not increases:
            return self._analysis_response(
                request,
                "aumentos_ninguno",
                "Comparé el archivo del proveedor con los costos de tu catálogo y no detecté "
                "aumentos significativos. Si esperabas cambios, verificá que los nombres "
                "coincidan con tus productos.",
            )
        increases.sort(key=lambda x: x["delta_pct"], reverse=True)
        avg = round(sum(i["delta_pct"] for i in increases) / len(increases), 1)
        lines = [
            f"Detecté aumentos en {len(increases)} producto(s) vs el costo de tu catálogo "
            f"(promedio +{avg}%):"
        ]
        for i in increases[:10]:
            lines.append(
                f"- {i['name']}: ${i['old_cost']:,.0f} → ${i['new_cost']:,.0f} "
                f"(+{i['delta_pct']:.0f}%)"
            )
        return self._analysis_response(
            request,
            "detectar_aumentos_proveedor",
            "\n".join(lines),
            {"increases": increases, "avg_increase_pct": avg},
        )

    async def _handle_price_analysis(
        self, request: AgentRequest, intent: str | None
    ) -> AgentResponse:
        if self._db is None:
            return self._empty_catalog_response(request)
        tenant_id = await self._tenant_uuid(request)
        if tenant_id is None:
            return self._empty_catalog_response(request)

        from app.application.agents.shared import analytics  # noqa: PLC0415
        from app.persistence.repositories.product_repository import (  # noqa: PLC0415
            ProductRepository,
        )

        # ── Intents que dependen de adjuntos (lista de precios / comparación) ──
        if intent in ("comparar_listas_precios", "comparar_precios_proveedores"):
            return await self._handle_price_list_comparison(request)
        if intent == "detectar_aumentos_proveedor":
            attach_result = await self._handle_supplier_price_increase(request, tenant_id)
            if attach_result is not None:
                return attach_result
            # sin adjunto → cae al análisis de márgenes del catálogo (abajo)
        if intent == "analizar_lista_precios":
            attach_result = await self._maybe_handle_price_list_attachment(request, tenant_id)
            if attach_result is not None:
                return attach_result
            # sin adjunto → cae al análisis de márgenes del catálogo (abajo)

        products = await ProductRepository(self._db).get_products_with_margin(tenant_id)
        if not products:
            return self._empty_catalog_response(request)

        target_pct, warning_pct = await self._get_margin_config(tenant_id)

        # ── sugerir_precios_venta ─────────────────────────────────────────────
        if intent == "sugerir_precios_venta":
            suggestions = analytics.suggest_sale_prices(products, target_pct)
            if not suggestions:
                return self._analysis_response(
                    request,
                    "sugerir_precios_sin_costo",
                    "Para sugerirte precios necesito el costo de tus productos. "
                    "Ninguno tiene costo cargado todavía — actualizá el costo unitario "
                    "y vuelvo a calcular el precio para tu margen objetivo.",
                )
            lines = [f"Precios sugeridos para un margen objetivo de {target_pct:.0f}%:"]
            for s in suggestions[:10]:
                lines.append(
                    f"- {s['name']}: actual ${s['current_price']:.0f} → "
                    f"sugerido ${s['suggested_price']:.0f}"
                )
            return self._analysis_response(
                request,
                "sugerir_precios_venta",
                "\n".join(lines),
                {"suggestions": suggestions[:30], "target_margin_pct": target_pct},
            )

        # ── identificar_productos_sensibles ───────────────────────────────────
        if intent == "identificar_productos_sensibles":
            sensitive = analytics.sensitive_products(products, warning_pct, cost_bump_pct=5.0)
            if not sensitive:
                return self._analysis_response(
                    request,
                    "productos_sensibles_ninguno",
                    "Buenas noticias: ningún producto caería por debajo de tu margen "
                    f"de alerta ({warning_pct:.0f}%) ante un aumento de costo del 5%. "
                    "Tus márgenes tienen colchón.",
                )
            lines = [
                f"Productos sensibles: un aumento del 5% en el costo los dejaría por "
                f"debajo de tu margen de alerta ({warning_pct:.0f}%):"
            ]
            for s in sensitive[:10]:
                lines.append(
                    f"- {s['name']}: margen {s['current_margin_pct']:.0f}% → "
                    f"{s['margin_after_bump_pct']:.0f}%"
                )
            return self._analysis_response(
                request,
                "identificar_productos_sensibles",
                "\n".join(lines),
                {"sensitive_products": sensitive},
            )

        # ── simular_actualizacion_precios ─────────────────────────────────────
        if intent == "simular_actualizacion_precios":
            pct = self._extract_pct(request.message)
            if pct is None:
                return AgentResponse(
                    request_id=request.request_id,
                    agent_name=self.agent_name,
                    status="requires_clarification",
                    risk_level=RiskLevel.LOW,
                    confidence=Confidence.MEDIUM,
                    question="¿Qué porcentaje de aumento querés simular? Por ejemplo: 12%.",
                    result={"summary": "Falta el porcentaje de aumento a simular."},
                )
            sim = analytics.simulate_price_increase(products, pct)
            lines = [f"Simulación de aumento del {pct:.0f}% sobre {len(sim['items'])} productos:"]
            if sim["avg_margin_before_pct"] is not None:
                lines.append(
                    f"Margen promedio: {sim['avg_margin_before_pct']:.0f}% → "
                    f"{sim['avg_margin_after_pct']:.0f}%."
                )
            lines.append("Recordá: esto es una simulación, no modifiqué ningún precio.")
            return self._analysis_response(
                request,
                "simular_actualizacion_precios",
                "\n".join(lines),
                {"simulation": sim},
            )

        # ── analizar_margenes_productos (default) ─────────────────────────────
        ranked = analytics.rank_by_margin(products)
        wm = ranked["with_margin"]
        if not wm:
            return self._analysis_response(
                request,
                "margenes_sin_costo",
                "Para analizar márgenes necesito el costo de tus productos. "
                "Todavía no hay costos cargados — actualizá el costo unitario y "
                "calculo el margen de cada producto.",
            )
        lines = [f"Margen promedio del catálogo: {ranked['avg_margin_pct']:.0f}%."]
        best = wm[:3]
        worst = wm[-3:][::-1]
        lines.append(
            "Mejores márgenes: " + ", ".join(f"{p['name']} ({p['margin_pct']:.0f}%)" for p in best)
        )
        if len(wm) > 3:
            lines.append(
                "Márgenes más bajos: "
                + ", ".join(f"{p['name']} ({p['margin_pct']:.0f}%)" for p in worst)
            )
        if ranked["without_cost"]:
            n = len(ranked["without_cost"])
            lines.append(
                f"{n} producto(s) sin costo cargado — no puedo calcular su margen "
                "hasta que cargues el costo unitario."
            )
        return self._analysis_response(
            request,
            "analizar_margenes_productos",
            "\n".join(lines),
            {
                "avg_margin_pct": ranked["avg_margin_pct"],
                "best": best,
                "worst": worst,
                "without_cost_count": len(ranked["without_cost"]),
            },
        )

    async def _handle_stock_analysis(
        self, request: AgentRequest, intent: str | None
    ) -> AgentResponse:
        if self._db is None:
            return self._empty_catalog_response(request)
        tenant_id = await self._tenant_uuid(request)
        if tenant_id is None:
            return self._empty_catalog_response(request)

        from app.application.agents.shared import analytics  # noqa: PLC0415
        from app.persistence.models.product import Product  # noqa: PLC0415
        from app.persistence.repositories.transaction_repository import (  # noqa: PLC0415
            SaleRepository,
        )

        rows = await self._db.execute(
            select(Product).where(
                Product.tenant_id == tenant_id,
                Product.is_active.is_(True),
            )
        )
        products_orm = list(rows.scalars().all())
        if not products_orm:
            return self._empty_catalog_response(request)

        products = [
            {
                "product_id": str(p.id),
                "name": p.name,
                "stock_units": p.stock_units,
                "low_stock_threshold_units": p.low_stock_threshold_units,
            }
            for p in products_orm
        ]
        velocity = await SaleRepository(self._db).get_daily_velocity(tenant_id, days=30)

        # ── detectar_quiebres_stock ───────────────────────────────────────────
        if intent == "detectar_quiebres_stock":
            critical = [
                p
                for p in products_orm
                if p.stock_units <= effective_threshold(p.low_stock_threshold_units)
            ]
            critical.sort(key=lambda p: p.stock_units)
            if not critical:
                return self._analysis_response(
                    request,
                    "quiebres_ninguno",
                    "No hay productos en quiebre ni cerca del umbral. Tu stock está sano.",
                )
            lines = [f"Tenés {len(critical)} producto(s) en quiebre o por debajo del umbral:"]
            for p in critical[:10]:
                v = velocity.get(str(p.id), 0.0)
                days = f", ~{p.stock_units / v:.0f} días" if v > 0 else ""
                lines.append(f"- {p.name}: {p.stock_units} u.{days}")
            return self._analysis_response(
                request,
                "detectar_quiebres_stock",
                "\n".join(lines),
                {"critical": [{"name": p.name, "stock": p.stock_units} for p in critical]},
            )

        # ── detectar_sobrestock ───────────────────────────────────────────────
        if intent == "detectar_sobrestock":
            config = HeuristicEngine.get(await self._business_vertical(tenant_id))
            # Umbral de sobrestock: el doble de la rotación máxima esperada del rubro
            overstock_days = float(config.inventory.rotation_days_max * 2)
            over = analytics.detect_overstock(products, velocity, overstock_days)
            if not over:
                return self._analysis_response(
                    request,
                    "sobrestock_ninguno",
                    "No detecté sobrestock significativo. Tu mercadería rota a buen ritmo.",
                )
            lines = [
                f"Detecté {len(over)} producto(s) con posible sobrestock (capital inmovilizado):"
            ]
            for r in over[:10]:
                cov = (
                    f"~{r['days_left']:.0f} días" if r["days_left"] is not None else "sin rotación"
                )
                lines.append(f"- {r['name']}: {r['stock_units']} u. ({cov})")
            return self._analysis_response(
                request,
                "detectar_sobrestock",
                "\n".join(lines),
                {"overstock": over},
            )

        # ── priorizar_reposicion ──────────────────────────────────────────────
        if intent == "priorizar_reposicion":
            ranking = analytics.replenishment_priority(products, velocity, effective_threshold)
            top = [
                r
                for r in ranking
                if r["stock_units"] <= 0 or (r["days_left"] is not None and r["days_left"] <= 14)
            ]
            if not top:
                return self._analysis_response(
                    request,
                    "reposicion_sin_urgencias",
                    "No hay urgencias de reposición: ningún producto está sin stock ni "
                    "por agotarse en los próximos 14 días.",
                )
            lines = ["Prioridad de reposición (más urgente primero):"]
            for r in top[:10]:
                cov = "SIN STOCK" if r["stock_units"] <= 0 else f"~{r['days_left']:.0f} días"
                lines.append(f"- {r['name']}: {r['stock_units']} u. ({cov})")
            return self._analysis_response(
                request,
                "priorizar_reposicion",
                "\n".join(lines),
                {"priority": top},
            )

        # ── estimar_dias_stock ────────────────────────────────────────────────
        if intent == "estimar_dias_stock":
            days_rows = analytics.estimate_stock_days(products, velocity)
            with_days = [r for r in days_rows if r["days_left"] is not None]
            with_days.sort(key=lambda r: r["days_left"])
            if not with_days:
                return self._analysis_response(
                    request,
                    "dias_stock_sin_ventas",
                    "No tengo ventas recientes para estimar días de stock. "
                    "Cargá tus ventas de las últimas semanas y vuelvo a calcular.",
                )
            lines = ["Días de stock estimados (según ventas de los últimos 30 días):"]
            for r in with_days[:10]:
                lines.append(f"- {r['name']}: {r['stock_units']} u. → ~{r['days_left']:.0f} días")
            return self._analysis_response(
                request,
                "estimar_dias_stock",
                "\n".join(lines),
                {"days": with_days},
            )

        # ── analizar_stock (default / panorama general) ──────────────────────
        total = len(products_orm)
        out_of_stock = [p for p in products_orm if p.stock_units == 0]
        low = [
            p
            for p in products_orm
            if 0 < p.stock_units <= effective_threshold(p.low_stock_threshold_units)
        ]
        lines = [
            f"Tu catálogo tiene {total} productos activos.",
            f"Sin stock: {len(out_of_stock)} | Pocas unidades: {len(low)} | "
            f"En stock: {total - len(out_of_stock) - len(low)}.",
        ]
        if out_of_stock:
            lines.append("Sin stock: " + ", ".join(p.name for p in out_of_stock[:5]))
        return self._analysis_response(
            request,
            "analizar_stock",
            "\n".join(lines),
            {
                "total_products": total,
                "out_of_stock": len(out_of_stock),
                "low_stock": len(low),
            },
        )

    @staticmethod
    def _extract_pct(message: str) -> float | None:
        """Extrae un porcentaje del mensaje (ej. '12%', 'aumento de 12 por ciento')."""
        import re  # noqa: PLC0415

        m = re.search(r"(\d{1,3}(?:[.,]\d+)?)\s*(?:%|por\s*ciento|porciento)", message.lower())
        if m:
            try:
                return float(m.group(1).replace(",", "."))
            except ValueError:
                return None
        return None
