"""AgentMarketing — análisis de marketing read-only para Véktor v4 (Fase 4).

Responsabilidades:
- Dashboard de marketing: seguidores, alcance, gasto en ads, ratio ads/ventas
  (sub-análisis `analizar_marketing` / `dashboard`)
- ROI de publicidad: relación ads vs ingresos (sub-análisis `analizar_roi_ads`)
- Sugerir campaña: top clientes a los que apuntar una promo (sub-análisis `sugerir_campana`)

Reglas duras:
- Read-only, determinístico: las cifras salen de MarketingService + shared/analytics.
  El LLM nunca calcula ni narra — el message se arma en Python.
- tenant_id del JWT (request.business_id). Test cross-tenant obligatorio.
- No-invention: sin métricas / sin clientes → mensaje de empty-state, nunca cifras.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from app.application.agents.base import BaseAgent
from app.application.agents.shared.schemas import (
    ActionType,
    AgentRequest,
    AgentResponse,
    Confidence,
    RiskLevel,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class AgentMarketing(BaseAgent):
    agent_name = "agent_marketing"

    def __init__(self, db: AsyncSession | None = None) -> None:
        self._db = db

    async def process(
        self,
        request: AgentRequest,
        task: Any | None = None,
    ) -> AgentResponse:
        action_type = getattr(task, "action_type", None)
        _intent = (getattr(task, "entities", {}) or {}).get("_intent")

        if action_type == ActionType.ANALYZE_MARKETING_DATA:
            return await self._handle_marketing_analysis(request, _intent)

        # Fallback cortés para cualquier otra acción (no reventar)
        return self._analysis_response(
            request,
            "agent_marketing_capabilities",
            "Puedo analizar tus métricas de marketing: rendimiento de redes sociales, "
            "gasto en publicidad vs ventas y sugerirte a qué clientes apuntar una promo.",
            confidence=Confidence.MEDIUM,
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

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

    # ── Handler principal ─────────────────────────────────────────────────────

    async def _handle_marketing_analysis(
        self, request: AgentRequest, intent: str | None
    ) -> AgentResponse:
        """Despacha el sub-análisis según el discriminador `_intent`.

        Determinístico: las cifras vienen de MarketingService (ya calculadas).
        El LLM nunca interviene — el message se construye en Python.
        """
        tenant_id = await self._tenant_uuid(request)
        if self._db is None or tenant_id is None:
            return self._analysis_response(
                request,
                "marketing_sin_datos",
                "Necesito acceso a tus datos de marketing para analizarlos. "
                "Cargá métricas de redes sociales desde el dashboard de marketing.",
                confidence=Confidence.MEDIUM,
            )

        # ── sugerir_campana: top clientes para promo ──────────────────────────
        if intent == "sugerir_campana":
            return await self._handle_suggest_campaign(request, tenant_id)

        # ── analizar_roi_ads: foco en gasto vs ingresos ───────────────────────
        if intent == "analizar_roi_ads":
            return await self._handle_roi_ads(request, tenant_id)

        # ── analizar_marketing (default / dashboard) ──────────────────────────
        return await self._handle_dashboard(request, tenant_id)

    async def _handle_dashboard(
        self, request: AgentRequest, tenant_id: uuid.UUID
    ) -> AgentResponse:
        """Dashboard completo: seguidores, alcance, gasto en ads y ratio ads/ventas."""
        from app.application.services.marketing_service import MarketingService  # noqa: PLC0415

        dashboard = await MarketingService(self._db).get_dashboard(tenant_id, days=30)  # type: ignore[arg-type]

        if not dashboard.has_data:
            return self._analysis_response(
                request,
                "marketing_sin_datos",
                "Todavía no hay datos de marketing para los últimos 30 días. "
                "Cargá métricas de redes sociales (seguidores, alcance, gasto en ads) "
                "desde el panel de marketing y vuelvo a analizarlas.",
                confidence=Confidence.MEDIUM,
            )

        lines = [
            f"Métricas de marketing — últimos {dashboard.days} días "
            f"({dashboard.from_date} al {dashboard.to_date}):"
        ]

        for p in dashboard.platforms:
            lines.append(
                f"- {p.platform.capitalize()}: "
                f"{p.followers:,} seguidores, "
                f"{p.reach:,} alcance, "
                f"${float(p.ads_spend_ars):,.0f} en ads".replace(",", ".")
            )

        if len(dashboard.platforms) > 1:
            lines.append(
                f"Total: {dashboard.total_followers:,} seguidores, "
                f"{dashboard.total_reach:,} alcance, "
                f"${float(dashboard.total_ads_spend_ars):,.0f} en ads".replace(",", ".")
            )

        avs = dashboard.ads_vs_sales
        revenue_f = float(avs.revenue_ars)
        ads_f = float(avs.ads_spend_ars)
        if revenue_f > 0 and avs.ratio is not None:
            pct = avs.ratio * 100
            lines.append(
                f"Relación ads/ventas: gastás ${ads_f:,.0f} en publicidad "
                f"sobre ${revenue_f:,.0f} en ventas ({pct:.1f}%).".replace(",", ".")
            )
        elif ads_f > 0:
            lines.append(
                f"Gastás ${ads_f:,.0f} en publicidad pero todavía no hay ventas registradas "
                "en este período.".replace(",", ".")
            )
        else:
            lines.append("No registraste gasto en ads en este período.")

        return self._analysis_response(
            request,
            "analizar_marketing",
            "\n".join(lines),
            {
                "days": dashboard.days,
                "total_followers": dashboard.total_followers,
                "total_reach": dashboard.total_reach,
                "total_ads_spend_ars": float(dashboard.total_ads_spend_ars),
                "revenue_ars": float(avs.revenue_ars),
                "ratio": avs.ratio,
                "platforms": [
                    {
                        "platform": p.platform,
                        "followers": p.followers,
                        "reach": p.reach,
                        "ads_spend_ars": float(p.ads_spend_ars),
                    }
                    for p in dashboard.platforms
                ],
            },
        )

    async def _handle_roi_ads(
        self, request: AgentRequest, tenant_id: uuid.UUID
    ) -> AgentResponse:
        """ROI de publicidad: foco en gasto ads vs ingresos."""
        from app.application.services.marketing_service import MarketingService  # noqa: PLC0415

        dashboard = await MarketingService(self._db).get_dashboard(tenant_id, days=30)  # type: ignore[arg-type]

        if not dashboard.has_data:
            return self._analysis_response(
                request,
                "marketing_sin_datos",
                "No hay métricas de publicidad cargadas. "
                "Registrá tu gasto en ads desde el panel de marketing para ver el ROI.",
                confidence=Confidence.MEDIUM,
            )

        avs = dashboard.ads_vs_sales
        ads_f = float(avs.ads_spend_ars)
        revenue_f = float(avs.revenue_ars)

        if ads_f == 0:
            return self._analysis_response(
                request,
                "roi_ads_sin_gasto",
                "No registraste gasto en publicidad en los últimos 30 días. "
                "Cargá el gasto en ads para calcular el ROI.",
                confidence=Confidence.MEDIUM,
            )

        if revenue_f == 0:
            return self._analysis_response(
                request,
                "roi_ads_sin_ventas",
                f"Gastaste ${ads_f:,.0f} en publicidad en los últimos 30 días, "
                "pero todavía no hay ventas registradas en ese período para calcular el ROI. "
                "Registrá tus ventas para comparar.".replace(",", "."),
                confidence=Confidence.MEDIUM,
            )

        pct = (avs.ratio or 0.0) * 100
        lines = [
            f"ROI de publicidad — últimos {dashboard.days} días:",
            f"- Gasto en ads: ${ads_f:,.0f}".replace(",", "."),
            f"- Ingresos por ventas: ${revenue_f:,.0f}".replace(",", "."),
            f"- Ratio ads/ventas: {pct:.1f}%",
        ]
        if pct < 10:
            lines.append(
                "Tu inversión publicitaria es baja respecto a los ingresos — "
                "considerá aumentar el presupuesto si querés más alcance."
            )
        elif pct > 30:
            lines.append(
                "Estás gastando bastante en ads respecto a tus ingresos. "
                "Evaluá si la publicidad está trayendo los clientes esperados."
            )
        else:
            lines.append("El ratio ads/ventas está en un rango razonable.")

        return self._analysis_response(
            request,
            "analizar_roi_ads",
            "\n".join(lines),
            {
                "ads_spend_ars": ads_f,
                "revenue_ars": revenue_f,
                "ratio": avs.ratio,
                "ratio_pct": pct,
            },
        )

    async def _handle_suggest_campaign(
        self, request: AgentRequest, tenant_id: uuid.UUID
    ) -> AgentResponse:
        """Sugiere a qué clientes top apuntar una promo, usando analytics.rank_customers."""
        from app.application.agents.shared import analytics  # noqa: PLC0415
        from app.persistence.repositories.transaction_repository import (  # noqa: PLC0415
            SaleRepository,
        )

        sales_by_customer = await SaleRepository(self._db).get_sales_by_customer(tenant_id)  # type: ignore[arg-type]

        if not sales_by_customer:
            return self._analysis_response(
                request,
                "sugerir_campana_sin_clientes",
                "Todavía no hay ventas con clientes identificados. "
                "Cuando registres ventas con cliente, te sugiero a quiénes "
                "apuntar la próxima promo.",
                confidence=Confidence.MEDIUM,
            )

        ranking = analytics.rank_customers(sales_by_customer)
        top = ranking.get("top", [])

        if not top:
            return self._analysis_response(
                request,
                "sugerir_campana_sin_clientes",
                "No encontré clientes con historial de compras suficiente para sugerir una promo.",
                confidence=Confidence.MEDIUM,
            )

        lines = [
            f"Para tu próxima campaña, apuntá a estos {min(len(top), 5)} clientes top:"
        ]
        for r in top[:5]:
            name = r.get("customer_name") or "Cliente sin nombre"
            total = r.get("total", 0)
            n_sales = r.get("n_sales", 0)
            lines.append(
                f"- {name}: ${total:,.0f} en {n_sales} compra(s)".replace(",", ".")
            )
        lines.append(
            "Son tus mejores compradores — un descuento o promo exclusiva puede "
            "retenerlos y aumentar la frecuencia de compra."
        )

        return self._analysis_response(
            request,
            "sugerir_campana",
            "\n".join(lines),
            {
                "n_customers": ranking["n_customers"],
                "total_revenue": ranking["total_revenue"],
                "top": top[:10],
            },
        )
