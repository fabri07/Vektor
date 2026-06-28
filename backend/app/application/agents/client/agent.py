"""AgentClient — análisis de clientes para Véktor v4 (Fase 2).

Responsabilidades:
- Analizar clientes por facturación (analizar_clientes)
- Detectar clientes inactivos (detectar_clientes_inactivos)
- Analizar cuentas por cobrar (analizar_cuentas_por_cobrar)
- Priorizar cobranza (priorizar_cobranza)

Handler movido desde AgentIncome. Reusa ActionType.ANALYZE_SALES_DATA (LOW risk).
No toca RiskEngine, sin ActionType nuevo, sin migración.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import anthropic

from app.application.agents.base import BaseAgent
from app.application.agents.shared.schemas import (
    ActionType,
    AgentRequest,
    AgentResponse,
    Confidence,
    RiskLevel,
    UsageSummary,
)
from app.integrations.anthropic_client import get_anthropic_async_client

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class AgentClient(BaseAgent):
    agent_name = "agent_client"

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

    async def process(
        self,
        request: AgentRequest,
        task: Any | None = None,
    ) -> AgentResponse:
        action_type = getattr(task, "action_type", None)
        analysis_intent = (getattr(task, "entities", {}) or {}).get("_intent")

        if action_type == ActionType.ANSWER_DATA_QUERY:
            return await self._handle_data_query(request, task)

        if action_type == ActionType.ANALYZE_SALES_DATA:
            return await self._handle_customer_analysis(request, analysis_intent)

        # Fallback cortés para cualquier otra acción (no reventar)
        return self._analysis_response(
            request,
            "agent_client_capabilities",
            "Puedo analizar tus clientes: mejores compradores, inactivos y cuentas por cobrar.",
            confidence=Confidence.MEDIUM,
        )

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

    async def _handle_customer_analysis(
        self, request: AgentRequest, intent: str | None
    ) -> AgentResponse:
        """Análisis de clientes: mejores clientes, inactivos, cuentas por cobrar.

        Determinístico: las cifras salen de las queries del repo + `shared.analytics`.
        Sub-análisis por discriminador `_intent`:
        - analizar_clientes (default): top por facturación + ventas + ticket promedio.
        - detectar_clientes_inactivos: clientes activos sin compras en N días.
        - analizar_cuentas_por_cobrar / priorizar_cobranza: fiado por cliente + total.

        Política no-invention: sin clientes / sin ventas con cliente → mensaje claro
        de "todavía no hay datos de clientes", nunca cifras inventadas.
        """
        tenant_id = await self._tenant_uuid(request)
        if self._db is None or tenant_id is None:
            return self._analysis_response(
                request,
                "clientes_sin_datos",
                "Necesito acceso a tus clientes y ventas para analizarlos. Cargá clientes "
                "y vendéles con el cliente identificado, y vuelvo a intentar.",
                confidence=Confidence.MEDIUM,
            )

        from app.application.agents.shared import analytics  # noqa: PLC0415
        from app.persistence.repositories.customer_repository import (  # noqa: PLC0415
            CustomerRepository,
        )
        from app.persistence.repositories.transaction_repository import (  # noqa: PLC0415
            SaleRepository,
        )

        customer_repo = CustomerRepository(self._db)
        sale_repo = SaleRepository(self._db)
        n_active = await customer_repo.count_active(tenant_id)

        if n_active == 0:
            return self._analysis_response(
                request,
                "clientes_sin_datos",
                "Todavía no tenés clientes cargados. Cuando registres tus clientes y les "
                "asocies ventas, te muestro quiénes son los mejores, quiénes dejaron de "
                "comprar y a quién le tenés que cobrar.",
                confidence=Confidence.MEDIUM,
            )

        # ── Cuentas por cobrar / cobranza ─────────────────────────────────────
        if intent in ("analizar_cuentas_por_cobrar", "priorizar_cobranza"):
            receivables = await sale_repo.get_receivables_by_customer(tenant_id)
            summary = analytics.summarize_receivables(receivables)
            if not summary["by_customer"]:
                return self._analysis_response(
                    request,
                    "cuentas_por_cobrar_sin_datos",
                    "No tenés ventas a cuenta corriente (fiado) registradas con cliente "
                    "identificado. Cuando registres una venta con medio de pago «cuenta "
                    "corriente», la voy a sumar acá para que sepas a quién cobrarle.",
                )
            total = summary["total_owed"]
            lines = [
                f"Te deben ${total:,.0f} en total (fiado), "
                f"repartido en {summary['n_customers']} cliente(s):".replace(",", ".")
            ]
            for r in summary["by_customer"][:10]:
                name = r.get("customer_name") or "Cliente sin nombre"
                lines.append(
                    f"- {name}: ${r['total_owed']:,.0f} en {r['n_sales']} venta(s)".replace(
                        ",", "."
                    )
                )
            if intent == "priorizar_cobranza":
                top = summary["by_customer"][0]
                top_name = top.get("customer_name") or "Cliente sin nombre"
                lines.append(
                    f"Empezá por {top_name}: es quien más te debe "
                    f"(${top['total_owed']:,.0f}).".replace(",", ".")
                )
            label = (
                "priorizar_cobranza"
                if intent == "priorizar_cobranza"
                else "analizar_cuentas_por_cobrar"
            )
            return self._analysis_response(
                request,
                label,
                "\n".join(lines),
                {"total_owed": total, "by_customer": summary["by_customer"][:30]},
            )

        # ── Clientes inactivos ────────────────────────────────────────────────
        if intent == "detectar_clientes_inactivos":
            days = 60
            inactive = await customer_repo.get_inactive_customers(tenant_id, days=days)
            if not inactive:
                return self._analysis_response(
                    request,
                    "clientes_inactivos_ninguno",
                    f"Buenas noticias: todos tus clientes compraron en los últimos {days} "
                    "días. Ninguno está inactivo.",
                )
            lines = [
                f"Tenés {len(inactive)} cliente(s) que no compran hace más de {days} días:"
            ]
            for c in inactive[:15]:
                name = c.get("customer_name") or "Cliente sin nombre"
                last = c.get("last_sale_date")
                when = f"última compra: {last}" if last is not None else "nunca te compró"
                lines.append(f"- {name} ({when})")
            return self._analysis_response(
                request,
                "detectar_clientes_inactivos",
                "\n".join(lines),
                {"days": days, "inactive": inactive[:30]},
            )

        # ── analizar_clientes (default): top por facturación ──────────────────
        sales_by_customer = await sale_repo.get_sales_by_customer(tenant_id)
        if not sales_by_customer:
            return self._analysis_response(
                request,
                "clientes_sin_ventas",
                f"Tenés {n_active} cliente(s) cargado(s), pero todavía no hay ventas "
                "asociadas a ellos. Cuando registres ventas con el cliente identificado, "
                "te muestro quiénes son tus mejores compradores.",
                confidence=Confidence.MEDIUM,
            )
        ranking = analytics.rank_customers(sales_by_customer)
        lines = [
            f"Analicé {ranking['n_customers']} cliente(s) con compras, "
            f"${ranking['total_revenue']:,.0f} facturados en "
            f"{ranking['total_sales']} venta(s). Tus mejores clientes:".replace(",", ".")
        ]
        for r in ranking["top"]:
            name = r.get("customer_name") or "Cliente sin nombre"
            ticket = (
                f"ticket promedio ${r['avg_ticket']:,.0f}".replace(",", ".")
                if r["avg_ticket"] is not None
                else "ticket promedio s/datos"
            )
            lines.append(
                f"- {name}: ${r['total']:,.0f} en {r['n_sales']} venta(s), {ticket}".replace(
                    ",", "."
                )
            )
        return self._analysis_response(
            request,
            "analizar_clientes",
            "\n".join(lines),
            {
                "n_customers": ranking["n_customers"],
                "total_revenue": ranking["total_revenue"],
                "total_sales": ranking["total_sales"],
                "top": ranking["top"],
            },
        )

    # ── F5b: ANSWER_DATA_QUERY — consulta libre sobre clientes ───────────────

    async def _handle_data_query(
        self, request: AgentRequest, task: Any | None
    ) -> AgentResponse:
        """Consulta libre sobre clientes: responde con datos determinísticos + narrador LLM.

        Política no-invention: sin clientes activos o sin ventas asociadas →
        mensaje claro sin llamar al LLM (no inflar tokens sin datos que narrar).
        """
        from app.application.agents.shared import analytics  # noqa: PLC0415
        from app.application.agents.shared.data_query_narrator import (  # noqa: PLC0415
            answer_data_query,
        )
        from app.persistence.repositories.customer_repository import (  # noqa: PLC0415
            CustomerRepository,
        )
        from app.persistence.repositories.transaction_repository import (  # noqa: PLC0415
            SaleRepository,
        )

        tenant_id = await self._tenant_uuid(request)
        if self._db is None or tenant_id is None:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="success",
                risk_level=RiskLevel.LOW,
                requires_approval=False,
                confidence=Confidence.LOW,
                message=(
                    "Necesito acceso a tus datos para responder eso. "
                    "Cargá la info y vuelvo a intentar."
                ),
            )

        customer_repo = CustomerRepository(self._db)
        sale_repo = SaleRepository(self._db)

        n_active = await customer_repo.count_active(tenant_id)
        if n_active == 0:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="success",
                risk_level=RiskLevel.LOW,
                requires_approval=False,
                confidence=Confidence.LOW,
                message=(
                    "Todavía no tenés clientes cargados. "
                    "Cuando registres clientes y les asociés ventas, "
                    "puedo responderte sobre ellos."
                ),
            )

        # Datos determinísticos desde repos
        sales_by_customer = await sale_repo.get_sales_by_customer(tenant_id)
        saldos_raw = await sale_repo.get_balances_by_customer(tenant_id)
        inactive = await customer_repo.get_inactive_customers(tenant_id, days=60)

        # Sin ventas ni saldos → nada que narrar
        if not sales_by_customer and not saldos_raw:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="success",
                risk_level=RiskLevel.LOW,
                requires_approval=False,
                confidence=Confidence.LOW,
                message=(
                    f"Tenés {n_active} cliente(s) cargado(s), pero todavía no hay ventas "
                    "asociadas. Cuando registres ventas con el cliente identificado, "
                    "puedo responder consultas sobre ellos."
                ),
            )

        ranking = analytics.rank_customers(sales_by_customer)

        # Serializable (no Decimal / date)
        saldos: list[dict[str, Any]] = [
            {
                "customer_name": s.get("customer_name"),
                "balance": float(s.get("balance", 0)),
                "total_account": float(s.get("total_account", 0)),
                "total_paid": float(s.get("total_paid", 0)),
            }
            for s in saldos_raw[:15]
        ]

        structured_data: dict[str, Any] = {
            "saldos": saldos,
            "ranking_top": ranking["top"][:10],
            "inactivos_n": len(inactive),
        }

        text, call = await answer_data_query(
            question=request.message,
            domain="clientes",
            structured_data=structured_data,
            business_name="tu negocio",
            client=self.client,
        )
        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="success",
            risk_level=RiskLevel.LOW,
            requires_approval=False,
            confidence=Confidence.HIGH,
            message=text,
            result={
                "summary": "Consulta respondida",
                "structured_data": structured_data,
                "analysis": True,
            },
            usage=UsageSummary(calls=[call]),
        )
