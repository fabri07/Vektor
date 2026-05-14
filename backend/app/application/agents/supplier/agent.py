"""AgentSupplier — proveedor via MCP Gmail (borrador, clasificación) o registro manual."""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import anthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.base import BaseAgent
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

    async def process(self, request: AgentRequest) -> AgentResponse:
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
                question="Para armar el email necesito: ¿A qué proveedor querés enviarlo y qué necesitás pedirle o comunicarle?",
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

    async def _generate_email_draft(self, message: str) -> tuple[dict, LLMCall | None]:
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
            model="claude-haiku-4-5-20251001",
            system=system,
            messages=[{"role": "user", "content": wrap_user_input(message)}],
            max_tokens=600,
        )
        if raw is None:
            return {"has_enough_info": False}, None
        try:
            return json.loads(raw), llm_call
        except Exception:
            logger.warning("agent_supplier.draft_json_parse_failed", raw=raw[:200])
            return {"has_enough_info": False}, llm_call

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

        payload: dict = {
            "amount": amount,
            "category": "compra_proveedor",
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

    async def _extract_purchase_entities(self, message: str) -> tuple[dict, LLMCall | None]:
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
            "Si no podés identificar el monto total, devolvé {\"error\": \"No pude identificar el monto. "
            "Indicame cuánto pagaste en total.\"}."
        )
        raw, llm_call = await call_llm(
            client=self.client,
            source="agent_supplier",
            model="claude-haiku-4-5-20251001",
            system=system,
            messages=[{"role": "user", "content": wrap_user_input(message)}],
            max_tokens=300,
        )
        if raw is None:
            return {"error": "No pude interpretar la compra. Indicame el monto y el producto."}, None
        try:
            return json.loads(raw), llm_call
        except Exception:
            logger.warning("agent_supplier.purchase_json_parse_failed", raw=raw[:200])
            return {"error": "No pude interpretar la compra. Indicame el monto y el producto."}, llm_call

    def _classify_intent(self, message: str) -> str:
        inbox_keywords = (
            "clasificar", "revisar inbox", "revisar gmail", "mensajes recibidos", "bandeja",
            "llegó mail", "llegó email", "llegó un mail", "llegó correo",
            "recibí mail", "recibí email", "recibí un correo", "recibí un mail",
        )
        draft_keywords = (
            "borrador", "redact", "escribi", "enviá",
            "enviar mail", "enviar email", "enviar un mail", "enviar un email",
            "mandar correo", "mandar mail", "mandar un mail", "mandar un email",
            "quiero enviar", "quiero mandar",
            "escribir mail", "escribir email", "redactar",
            "email a ", "mail a ", "un mail a", "un email a",
            "podés enviar", "puedes enviar", "podés mandar", "puedes mandar",
        )
        # Frases analíticas: se evalúan ANTES que purchase_keywords porque "compré"
        # está en ambas y las consultas como "a quién le compré más" son más específicas.
        query_keywords = (
            "qué proveedores", "proveedores tengo", "a quién le compré", "a quien le compré",
            "proveedores principales", "cuánto le compré", "cuánto les compré",
            "últimas compras por proveedor", "mis proveedores", "análisis de proveedores",
            "proveedores del mes", "proveedores del año",
        )
        # Frases de registro/mutación: verbos de acción concretos.
        purchase_keywords = (
            "registrar compra", "registrá compra", "factura de", "proveedor cobró",
            "me mandaron factura", "quiero registrar",
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
                    "message": f"No hay gastos con proveedor registrados en los últimos {period_days} días.",
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
                "last_purchase": str(data["last_date"]),
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
