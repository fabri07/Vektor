"""AgentSupplier — proveedor via MCP Gmail (borrador, clasificación) o registro manual."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import anthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.base import BaseAgent
from app.application.agents.shared.json_parse import parse_llm_json
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
from app.integrations.anthropic_client import get_anthropic_async_client
from app.observability.logger import get_logger

if TYPE_CHECKING:
    from app.application.ports.mcp_gateway import McpToolGateway

logger = get_logger(__name__)


class AgentSupplier(BaseAgent):
    agent_name = "agent_supplier"

    def __init__(
        self,
        session: AsyncSession | None = None,
        gateway: McpToolGateway | None = None,
    ) -> None:
        self._session = session
        self._gateway = gateway
        self._client: Any | None = None

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = get_anthropic_async_client(anthropic.AsyncAnthropic)
        return self._client

    async def process(self, request: AgentRequest, task: Any | None = None) -> AgentResponse:
        # ── Sprint 17: análisis de proveedores read-only ─────────────────────
        action_type = getattr(task, "action_type", None)
        analysis_intent = (getattr(task, "entities", {}) or {}).get("_intent")
        if action_type == ActionType.ANALYZE_SUPPLIER_DATA:
            return await self._handle_supplier_analysis(request, analysis_intent)

        # ── F3a: persistir borrador de pedido desde stock crítico ─────────────
        if action_type == ActionType.CREATE_PURCHASE_SUGGESTION:
            return await self._handle_create_purchase_suggestion(request)

        message = request.message.lower()
        intent = self._classify_intent(message)

        if intent == "create_draft":
            return await self._handle_create_draft(request)

        if intent == "classify_inbox":
            return self._handle_classify_inbox(request)

        if intent == "record_purchase":
            return await self._handle_record_purchase(request)

        if intent == "query":
            return await self._handle_query(request)

        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="requires_clarification",
            risk_level=RiskLevel.LOW,
            confidence=Confidence.MEDIUM,
            question=(
                "¿Qué necesitás hacer con el proveedor?\n"
                "- Consultar mis proveedores y compras\n"
                "- Redactar un email de compra\n"
                "- Revisar mensajes recibidos\n"
                "- Registrar una compra manualmente"
            ),
            result={"summary": "Aclará qué acción de proveedor necesitás."},
        )

    async def _handle_create_draft(self, request: AgentRequest) -> AgentResponse:
        mode = "mcp" if self._gateway else "informational"
        draft, draft_call = await self._generate_email_draft(request.message)
        usage = UsageSummary(calls=[draft_call]) if draft_call else None

        if not draft.get("has_enough_info"):
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.HIGH,
                requires_approval=False,
                question=(
                    "Para armar el email necesito: ¿A qué proveedor querés enviarlo y qué "
                    "necesitás pedirle o comunicarle?"
                ),
                result={
                    "action_type": ActionType.CREATE_SUPPLIER_DRAFT,
                    "summary": "Necesito más datos para generar el email.",
                    "mode": mode,
                    "payload": {"message": request.message},
                },
                usage=usage,
            )

        to_name = draft.get("to_name") or "proveedor"
        subject = draft.get("subject") or "Consulta"
        body = draft.get("body") or request.message

        summary_preview = body[:200] + "..." if len(body) > 200 else body
        summary = f"Email para {to_name}\nAsunto: {subject}\n\n{summary_preview}"

        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="requires_approval",
            risk_level=RiskLevel.MEDIUM,
            confidence=Confidence.HIGH,
            requires_approval=True,
            result={
                "action_type": ActionType.CREATE_SUPPLIER_DRAFT,
                "summary": summary,
                "mode": mode,
                "payload": {
                    "to": draft.get("to_email") or "",
                    "to_name": to_name,
                    "subject": subject,
                    "body": body,
                    "email_mode": "draft",
                    "mode": mode,
                },
            },
            usage=usage,
        )

    def _handle_classify_inbox(self, request: AgentRequest) -> AgentResponse:
        mode = "mcp" if self._gateway else "informational"
        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="requires_approval",
            risk_level=RiskLevel.LOW,
            confidence=Confidence.HIGH,
            requires_approval=True,
            result={
                "action_type": ActionType.CLASSIFY_GMAIL_MESSAGE,
                "summary": "Revisar mensajes recibidos de proveedores en Gmail.",
                "mode": mode,
                "payload": {
                    "message": request.message,
                    "message_id": "",
                    "mode": mode,
                },
            },
        )

    async def _generate_email_draft(self, message: str) -> tuple[dict[str, Any], LLMCall | None]:
        system = (
            "Sos el asistente de Véktor. Analizá el mensaje del usuario y generá un email "
            "formal en español rioplatense para un proveedor.\n\n"
            "Retorná SOLO un JSON:\n"
            "{\n"
            '  "has_enough_info": true|false,\n'
            '  "to_name": "nombre del proveedor o null",\n'
            '  "to_email": "email si se menciona explícitamente, o null",\n'
            '  "subject": "asunto del email",\n'
            '  "body": "cuerpo completo del email, profesional y directo"\n'
            "}\n\n"
            "has_enough_info=true solo si el mensaje tiene suficiente contexto para saber "
            "QUÉ comunicarle al proveedor (qué pedir, reclamar o consultar).\n"
            "Si falta el destinatario, igual generá el email con to_name=null.\n"
            "Si falta el contenido → has_enough_info=false."
        )
        raw, llm_call = await call_llm(
            client=self.client,
            source="agent_supplier",
            model="claude-sonnet-4-6",
            system=system,
            messages=[{"role": "user", "content": wrap_user_input(message)}],
            max_tokens=1200,
        )
        if raw is None:
            return {"has_enough_info": False}, None
        parsed = parse_llm_json(raw)
        if parsed is None:
            return {"has_enough_info": False}, llm_call
        return parsed, llm_call

    async def _handle_record_purchase(self, request: AgentRequest) -> AgentResponse:
        entities, purchase_call = await self._extract_purchase_entities(request.message)
        usage = UsageSummary(calls=[purchase_call]) if purchase_call else None

        if "error" in entities:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.LOW,
                question=entities["error"],
                result={"summary": "Faltan datos para registrar la compra."},
                usage=usage,
            )

        product_id: str | None = None
        alternatives: list[str] = []
        product_name = entities.get("product_name")
        sku = entities.get("sku")

        if self._session is not None and (product_name or sku):
            product_id, alternatives = await resolve_product_id(
                self._session, request.business_id, product_name, sku
            )

        if product_name and product_id is None:
            if alternatives:
                names = ", ".join(f'"{n}"' for n in alternatives)
                question = (
                    f"Encontré varios productos que coinciden con '{product_name}': {names}. "
                    "¿Cuál es el nombre exacto o el SKU del producto que compraste?"
                )
            else:
                question = (
                    f"No encontré '{product_name}' en tu catálogo. "
                    "Indicame el nombre exacto o el SKU para poder actualizar el stock."
                )
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.LOW,
                question=question,
                result={"summary": "Producto no identificado en el catálogo."},
                usage=usage,
            )

        amount = entities.get("amount", "0")
        qty = int(entities.get("qty") or 0)
        supplier = entities.get("supplier_name") or "proveedor"

        parts = [f"Registrar compra a {supplier}: ${amount}"]
        if product_name and qty:
            parts.append(f"{qty} × {product_name}")

        summary = ". ".join(parts) + "."
        if product_id and qty:
            summary += f" Actualiza stock +{qty} unidades."

        payload: dict[str, Any] = {
            "amount": amount,
            "category": "INVENTORY",
            "expense_type": "COGS",
            "description": f"Compra a {supplier}" + (f" — {product_name}" if product_name else ""),
            "transaction_date": entities.get("transaction_date", ""),
            "notes": request.message,
        }
        if product_id:
            payload["product_id"] = product_id
            payload["product_name"] = product_name
            payload["qty"] = qty
            if entities.get("unit_cost"):
                payload["unit_cost"] = entities["unit_cost"]

        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="requires_approval",
            risk_level=RiskLevel.MEDIUM,
            confidence=Confidence.HIGH,
            requires_approval=True,
            result={
                "action_type": ActionType.REGISTER_PURCHASE,
                "summary": summary,
                "payload": payload,
            },
            usage=usage,
        )

    async def _extract_purchase_entities(
        self, message: str
    ) -> tuple[dict[str, Any], LLMCall | None]:
        from datetime import date  # noqa: PLC0415

        today = date.today().isoformat()
        system = (
            f"Hoy es {today}. Extraé datos de una compra a un proveedor.\n"
            "Retorná SOLO un JSON:\n"
            "{\n"
            '  "amount": "<monto total en string, ej: 45000>",\n'
            '  "supplier_name": "<nombre del proveedor o null>",\n'
            '  "product_name": "<nombre del producto comprado o null>",\n'
            '  "sku": "<SKU si se menciona o null>",\n'
            '  "qty": <cantidad entera comprada o null>,\n'
            '  "unit_cost": "<costo unitario en string o null>",\n'
            '  "transaction_date": "<fecha ISO 8601 o fecha de hoy>"\n'
            "}\n\n"
            'Si no podés identificar el monto total, devolvé {"error": "No pude identificar el '
            "monto. "
            'Indicame cuánto pagaste en total."}.'
        )
        raw, llm_call = await call_llm(
            client=self.client,
            source="agent_supplier",
            model="claude-sonnet-4-6",
            system=system,
            messages=[{"role": "user", "content": wrap_user_input(message)}],
            max_tokens=800,
        )
        if raw is None:
            return {
                "error": "No pude interpretar la compra. Indicame el monto y el producto."
            }, None
        parsed = parse_llm_json(raw)
        if parsed is None:
            return {
                "error": "No pude interpretar la compra. Indicame el monto y el producto."
            }, llm_call
        return parsed, llm_call

    # ── Sprint 17: análisis de proveedores (read-only, determinístico) ────────

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

    async def _supplier_totals(self, tenant_id: uuid.UUID, days: int = 90) -> list[dict[str, Any]]:
        """Agrega gasto por proveedor en una ventana: total, count, last_date, pct."""
        from app.persistence.models.transaction import ExpenseEntry  # noqa: PLC0415

        cutoff = date.today() - timedelta(days=days)
        assert self._session is not None  # garantizado por _handle_supplier_analysis
        rows = await self._session.execute(
            select(ExpenseEntry).where(
                ExpenseEntry.tenant_id == tenant_id,
                ExpenseEntry.transaction_date >= cutoff,
                ExpenseEntry.supplier_name.isnot(None),
                ExpenseEntry.supplier_name != "",
                ExpenseEntry.voided_at.is_(None),
            )
        )
        expenses = list(rows.scalars().all())
        suppliers: dict[str, dict[str, Any]] = {}
        for e in expenses:
            name = (e.supplier_name or "").strip()
            if not name:
                continue
            slot = suppliers.setdefault(
                name, {"total": Decimal(0), "count": 0, "last_date": e.transaction_date}
            )
            slot["total"] += e.amount
            slot["count"] += 1
            if e.transaction_date > slot["last_date"]:
                slot["last_date"] = e.transaction_date
        grand_total = float(sum(s["total"] for s in suppliers.values())) or 0.0
        out = [
            {
                "name": name,
                "total": float(s["total"]),
                "count": s["count"],
                "last_purchase": str(s["last_date"].date()),
                "days_since": (date.today() - s["last_date"].date()).days,
                "pct": round(float(s["total"]) / grand_total * 100, 1) if grand_total > 0 else 0.0,
            }
            for name, s in suppliers.items()
        ]
        out.sort(key=lambda r: r["total"], reverse=True)
        return out

    async def _handle_supplier_analysis(
        self, request: AgentRequest, intent: str | None
    ) -> AgentResponse:
        if self._session is None:
            return self._analysis_response(
                request,
                "proveedores_sin_datos",
                "Necesito acceso a tus gastos para analizar proveedores.",
            )
        try:
            tenant_id = uuid.UUID(request.business_id)
        except (ValueError, TypeError):
            return self._analysis_response(
                request,
                "proveedores_sin_datos",
                "No pude identificar tu negocio.",
            )

        # ── comparar_precios_proveedores → comparación de 2 adjuntos ──────────
        if intent == "comparar_precios_proveedores":
            return await self._handle_supplier_price_comparison(request)

        suppliers = await self._supplier_totals(tenant_id, days=90)
        if not suppliers:
            return self._analysis_response(
                request,
                "proveedores_ninguno",
                "No tengo gastos con proveedor registrados en los últimos 90 días. "
                "Cuando registres compras con el nombre del proveedor, te muestro el ranking "
                "y tu dependencia de cada uno.",
            )

        # ── detectar_dependencia_proveedor ────────────────────────────────────
        if intent == "detectar_dependencia_proveedor":
            top = suppliers[0]
            if top["pct"] > 50:
                msg = (
                    f"⚠ Dependés fuertemente de {top['name']}: representa el {top['pct']:.0f}% "
                    f"de tu gasto con proveedores (${top['total']:,.0f} en 90 días). "
                    "Una interrupción de este proveedor te afectaría mucho — conviene tener "
                    "una alternativa identificada."
                )
                return self._analysis_response(
                    request,
                    "detectar_dependencia_proveedor",
                    msg,
                    {
                        "risk_code": "SUPPLIER_DEPENDENCY",
                        "top_supplier": top,
                        "suppliers": suppliers,
                    },
                )
            return self._analysis_response(
                request,
                "dependencia_baja",
                f"Tu proveedor principal ({top['name']}) concentra el {top['pct']:.0f}% del "
                "gasto, por debajo del 50%. No veo una dependencia riesgosa: tu abastecimiento "
                "está razonablemente diversificado.",
                {"top_supplier": top, "suppliers": suppliers},
            )

        # ── analizar_proveedores (default) ────────────────────────────────────
        total = sum(s["total"] for s in suppliers)
        lines = [f"Tenés {len(suppliers)} proveedores activos (90 días), ${total:,.0f} en compras:"]
        for s in suppliers[:6]:
            lines.append(
                f"- {s['name']}: ${s['total']:,.0f} ({s['pct']:.0f}%), última compra hace "
                f"{s['days_since']} días"
            )
        return self._analysis_response(
            request,
            "analizar_proveedores",
            "\n".join(lines),
            {"suppliers": suppliers},
        )

    async def _critical_stock_for_order(self, tenant_id: uuid.UUID) -> list[dict[str, Any]]:
        """Devuelve productos con stock <= umbral efectivo.

        Claves devueltas (extendido en F3a — se agregaron sin romper callers existentes):
          ``name``        — nombre del producto
          ``stock``       — stock actual (int)
          ``product_id``  — str(uuid) del producto
          ``unit_cost``   — Decimal (unit_cost_ars o 0 si no configurado)
          ``threshold``   — umbral efectivo (int)
        """
        from app.domain.product import effective_threshold  # noqa: PLC0415
        from app.persistence.models.product import Product  # noqa: PLC0415

        assert self._session is not None  # garantizado por _handle_supplier_analysis
        rows = await self._session.execute(
            select(Product).where(
                Product.tenant_id == tenant_id,
                Product.is_active.is_(True),
            )
        )
        products = list(rows.scalars().all())
        critical = [
            p for p in products if p.stock_units <= effective_threshold(p.low_stock_threshold_units)
        ]
        critical.sort(key=lambda p: p.stock_units)
        return [
            {
                "name": p.name,
                "stock": p.stock_units,
                "product_id": str(p.id),
                "unit_cost": p.unit_cost_ars if p.unit_cost_ars is not None else Decimal(0),
                "threshold": effective_threshold(p.low_stock_threshold_units),
            }
            for p in critical
        ]

    async def _handle_create_purchase_suggestion(self, request: AgentRequest) -> AgentResponse:
        """Persiste un borrador de PurchaseOrder desde el stock en quiebre.

        Reglas:
        - Sin quiebres → no crea PO, mensaje claro.
        - Cantidad de reposición = max(threshold - stock, 1): repone hasta el umbral,
          mínimo 1 unidad (garantiza que el pedido tenga sentido aunque el stock sea
          igual al umbral exacto).
        - Subtotales y total calculados con Decimal — el LLM nunca calcula montos.
        - supplier_id se resuelve por nombre del proveedor más frecuente (fuzzy→exact).
          Si no se resuelve, queda NULL (el usuario lo edita).
        """
        from decimal import ROUND_HALF_UP  # noqa: PLC0415

        from app.persistence.models.purchase_order import PurchaseOrder  # noqa: PLC0415
        from app.persistence.repositories.purchase_order_repository import (  # noqa: PLC0415
            PurchaseOrderRepository,
        )
        from app.persistence.repositories.supplier_repository import (  # noqa: PLC0415
            SupplierRepository,
        )
        from app.schemas.purchase_order import PurchaseOrderItem  # noqa: PLC0415

        if self._session is None:
            return self._analysis_response(
                request,
                "pedido_sin_sesion",
                "Necesito acceso a la base de datos para armar el pedido.",
            )

        try:
            tenant_id = uuid.UUID(request.business_id)
        except (ValueError, TypeError):
            return self._analysis_response(
                request,
                "pedido_sin_sesion",
                "No pude identificar tu negocio.",
            )

        critical = await self._critical_stock_for_order(tenant_id)
        if not critical:
            return self._analysis_response(
                request,
                "pedido_sin_quiebres",
                "No tenés productos en quiebre que requieran un pedido ahora. "
                "Tu stock está cubierto.",
            )

        # Resolver el proveedor principal por nombre del más frecuente
        suppliers = await self._supplier_totals(tenant_id)
        main_supplier_name: str | None = suppliers[0]["name"] if suppliers else None
        supplier_id: uuid.UUID | None = None
        if main_supplier_name:
            sup_repo = SupplierRepository(self._session)
            supplier = await sup_repo.find_by_name(
                main_supplier_name.strip().lower(), tenant_id
            )
            if supplier is not None:
                supplier_id = supplier.id

        # Armar items con aritmética determinística (Decimal)
        two_places = Decimal("0.01")
        items: list[PurchaseOrderItem] = []
        for p in critical:
            # Reposición: cuántas unidades se necesitan para llegar al umbral.
            # Mínimo 1 — si stock == threshold exacto, igual pedimos 1 unidad.
            quantity = max(p["threshold"] - p["stock"], 1)
            unit_cost: Decimal = p["unit_cost"]
            subtotal = (Decimal(quantity) * unit_cost).quantize(two_places, rounding=ROUND_HALF_UP)
            items.append(
                PurchaseOrderItem(
                    product_id=p["product_id"],
                    product_name=p["name"],
                    sku=None,
                    quantity=quantity,
                    unit_cost=unit_cost,
                    subtotal=subtotal,
                )
            )

        total = sum((item.subtotal for item in items), Decimal(0)).quantize(
            two_places, rounding=ROUND_HALF_UP
        )

        po = PurchaseOrder(
            tenant_id=tenant_id,
            supplier_id=supplier_id,
            status="draft",
            total=total,
            items=[item.model_dump(mode="json") for item in items],
            notes=None,
        )
        po_repo = PurchaseOrderRepository(self._session)
        po = await po_repo.create(po)

        supplier_label = main_supplier_name or "tu proveedor habitual"
        n = len(items)
        message = (
            f"Guardé un borrador de pedido para {supplier_label} con {n} producto(s) "
            f"por ${float(total):,.2f}. Revisalo y ajustá las cantidades antes de enviarlo."
        )

        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="success",
            risk_level=RiskLevel.LOW,
            requires_approval=False,
            confidence=Confidence.HIGH,
            message=message,
            result={
                "summary": f"Borrador de pedido creado: {n} producto(s), total ${float(total):,.2f}",
                "structured_data": {
                    "purchase_order_id": str(po.id),
                    "items": [item.model_dump(mode="json") for item in items],
                    "total": str(total),
                    "supplier": supplier_label,
                },
                "analysis": False,
            },
        )

    async def _handle_supplier_price_comparison(self, request: AgentRequest) -> AgentResponse:
        from app.application.agents.shared import analytics  # noqa: PLC0415
        from app.persistence.models.file import UploadedFile  # noqa: PLC0415

        summaries: list[dict[str, Any]] = []
        for attachment in request.attachments or []:
            file_id = attachment.get("file_id") if isinstance(attachment, dict) else None
            if not file_id:
                continue
            try:
                file_uuid = uuid.UUID(str(file_id))
                tenant_uuid = uuid.UUID(str(request.business_id))
            except (ValueError, TypeError):
                continue
            assert self._session is not None  # garantizado por _handle_supplier_analysis
            res = await self._session.execute(
                select(UploadedFile).where(
                    UploadedFile.id == file_uuid, UploadedFile.tenant_id == tenant_uuid
                )
            )
            f = res.scalar_one_or_none()
            if f is not None and isinstance(f.parsed_summary_json, dict):
                summaries.append(f.parsed_summary_json)

        if len(summaries) < 2:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.MEDIUM,
                question=(
                    "Para comparar proveedores necesito dos listas de precios adjuntas "
                    "(una por proveedor). Subí ambas y las comparo producto por producto."
                ),
                result={"summary": "Faltan dos listas de proveedores para comparar."},
            )

        from app.application.agents.shared.analytics import parse_money  # noqa: PLC0415

        def _price_map(summary: dict[str, Any]) -> dict[str, float]:
            _buckets = (
                "stock_detectado",
                "ventas_detectadas",
                "gastos_detectados",
                "otros_detectados",
            )
            for key in _buckets:
                rows = summary.get(key)
                if isinstance(rows, list) and rows:
                    out: dict[str, float] = {}
                    for r in rows:
                        if not isinstance(r, dict):
                            continue
                        lowered = {str(k).strip().lower(): v for k, v in r.items()}
                        name = next(
                            (
                                str(lowered[k])
                                for k in ("nombre", "producto", "name", "descripcion")
                                if lowered.get(k)
                            ),
                            None,
                        )
                        price_raw = next(
                            (
                                lowered[k]
                                for k in ("precio", "price", "costo", "importe", "valor")
                                if lowered.get(k) is not None
                            ),
                            None,
                        )
                        if not name:
                            continue
                        price = parse_money(price_raw)
                        if price is None:
                            continue
                        out[name.strip().lower()] = price
                    return out
            return {}

        map_a = _price_map(summaries[0])
        map_b = _price_map(summaries[1])
        diff = analytics.diff_price_lists(map_a, map_b)
        if not diff:
            return self._analysis_response(
                request,
                "comparar_proveedores_sin_match",
                "No encontré productos en común entre las dos listas. Verificá que los "
                "nombres coincidan para poder compararlos.",
            )
        a_cheaper = [d for d in diff if d["delta_abs"] > 0]  # B más caro → A conviene
        lines = [f"Comparé {len(diff)} productos en común entre los dos proveedores."]
        if a_cheaper:
            lines.append(
                f"En {len(a_cheaper)} productos, la primera lista es más barata. " "Ejemplos:"
            )
            for d in sorted(a_cheaper, key=lambda x: x["delta_abs"], reverse=True)[:6]:
                lines.append(
                    f"- {d['key']}: ${d['old_price']:.0f} vs ${d['new_price']:.0f} "
                    f"(diferencia ${d['delta_abs']:.0f})"
                )
        return self._analysis_response(
            request,
            "comparar_precios_proveedores",
            "\n".join(lines),
            {"diff": diff},
        )

    def _classify_intent(self, message: str) -> str:
        inbox_keywords = (
            "clasificar",
            "revisar inbox",
            "revisar gmail",
            "mensajes recibidos",
            "bandeja",
            "llegó mail",
            "llegó email",
            "llegó un mail",
            "llegó correo",
            "recibí mail",
            "recibí email",
            "recibí un correo",
            "recibí un mail",
        )
        draft_keywords = (
            "borrador",
            "redact",
            "escribi",
            "enviá",
            "enviar mail",
            "enviar email",
            "enviar un mail",
            "enviar un email",
            "mandar correo",
            "mandar mail",
            "mandar un mail",
            "mandar un email",
            "quiero enviar",
            "quiero mandar",
            "escribir mail",
            "escribir email",
            "redactar",
            "email a ",
            "mail a ",
            "un mail a",
            "un email a",
            "podés enviar",
            "puedes enviar",
            "podés mandar",
            "puedes mandar",
        )
        # Frases analíticas: se evalúan ANTES que purchase_keywords porque "compré"
        # está en ambas y las consultas como "a quién le compré más" son más específicas.
        query_keywords = (
            "qué proveedores",
            "proveedores tengo",
            "a quién le compré",
            "a quien le compré",
            "proveedores principales",
            "cuánto le compré",
            "cuánto les compré",
            "últimas compras por proveedor",
            "mis proveedores",
            "análisis de proveedores",
            "proveedores del mes",
            "proveedores del año",
        )
        # Frases de registro/mutación: verbos de acción concretos.
        purchase_keywords = (
            "registrar compra",
            "registrá compra",
            "factura de",
            "proveedor cobró",
            "me mandaron factura",
            "quiero registrar",
        )

        if any(kw in message for kw in inbox_keywords):
            return "classify_inbox"
        if any(kw in message for kw in draft_keywords):
            return "create_draft"
        # query va antes que purchase para que frases analíticas no pisen registro
        if any(kw in message for kw in query_keywords):
            return "query"
        if any(kw in message for kw in purchase_keywords):
            return "record_purchase"
        # Fallback: si el CEO rutea ask_supplier_status → query
        return "query"

    async def _handle_query(self, request: AgentRequest) -> AgentResponse:
        """Analiza proveedores de los últimos 90 días a partir de gastos reales."""
        if self._session is None:
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

        from app.persistence.models.transaction import ExpenseEntry  # noqa: PLC0415

        period_days = 90
        cutoff = date.today() - timedelta(days=period_days)

        assert self._session is not None  # garantizado por _handle_supplier_analysis
        rows = await self._session.execute(
            select(ExpenseEntry).where(
                ExpenseEntry.tenant_id == tenant_id,
                ExpenseEntry.transaction_date >= cutoff,
                ExpenseEntry.supplier_name.isnot(None),
                ExpenseEntry.supplier_name != "",
                ExpenseEntry.voided_at.is_(None),
            )
        )
        expenses = list(rows.scalars().all())

        if not expenses:
            logger.info("agent_supplier.query.empty", tenant_id=str(tenant_id))
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="success",
                risk_level=RiskLevel.LOW,
                confidence=Confidence.HIGH,
                result={
                    "summary": "SIN_PROVEEDORES",
                    "period_days": period_days,
                    "message": (
                        f"No hay gastos con proveedor registrados en los últimos {period_days} "
                        f"días."
                    ),
                },
            )

        # Agrupar por proveedor
        suppliers: dict[str, dict[str, Any]] = {}
        for e in expenses:
            name = (e.supplier_name or "").strip()
            if not name:
                continue
            if name not in suppliers:
                suppliers[name] = {
                    "total": Decimal(0),
                    "count": 0,
                    "last_date": e.transaction_date,
                }
            suppliers[name]["total"] += e.amount
            suppliers[name]["count"] += 1
            if e.transaction_date > suppliers[name]["last_date"]:
                suppliers[name]["last_date"] = e.transaction_date

        top = sorted(suppliers.items(), key=lambda x: x[1]["total"], reverse=True)[:5]
        top_list = [
            {
                "name": name,
                "total": float(data["total"]),
                "count": data["count"],
                "last_purchase": str(data["last_date"].date()),
            }
            for name, data in top
        ]

        logger.info(
            "agent_supplier.query",
            tenant_id=str(tenant_id),
            unique_suppliers=len(suppliers),
            period_days=period_days,
        )

        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="success",
            risk_level=RiskLevel.LOW,
            confidence=Confidence.HIGH,
            result={
                "summary": "supplier_query",
                "period_days": period_days,
                "unique_suppliers": len(suppliers),
                "top_suppliers": top_list,
            },
        )
