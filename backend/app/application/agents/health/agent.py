"""AgentHealth — coordinador thin.

Flujo:
  sub_collector  → BusinessState (via BSL, misma fuente que Celery)
  sub_calculator → ComponentScoresV2 (fórmula v2: 5 dims)
  sub_narrator   → narrativa ejecutiva (Sonnet)

GUARDRAILS:
- No ejecuta acciones sobre el negocio.
- El score se calcula en Python, NUNCA con LLM.
- Datos con confidence=LOW → requires_clarification (ValidationGate).
- Context Budget: 4.000 tokens.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, cast

import anthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from redis.asyncio import Redis

from app.application.agents.base import BaseAgent
from app.application.agents.health.sub_calculator import ComponentScoresV2, compute_scores
from app.application.agents.health.sub_collector import collect
from app.application.agents.health.sub_narrator import generate
from app.application.agents.shared.event_bus import EventBus
from app.application.agents.shared.schemas import (
    AgentRequest,
    AgentResponse,
    RiskLevel,
    UsageSummary,
)
from app.application.agents.shared.vertical_lookup import load_tenant_vertical
from app.application.services.health_config_service import get_margin_benchmark
from app.domain.verticals import UnknownVerticalError, Vertical, parse_vertical
from app.heuristics.verticals import weakest_confidence
from app.heuristics.verticals.loader import load_vertical_heuristics
from app.integrations.anthropic_client import get_anthropic_async_client
from app.persistence.models.business import BusinessProfile
from app.persistence.models.tenant import Tenant


# Redis stub mínimo para sub_collector cuando no hay Redis real disponible
class _NullRedis:
    async def get(self, _key: str) -> None:
        return None

    async def set(
        self, _key: str, _value: str, *, nx: bool = False, ex: int | None = None
    ) -> bool | None:
        return True if not nx else True

    async def aclose(self) -> None:
        return None


class AgentHealth(BaseAgent):
    agent_name = "agent_health"

    def __init__(self, db: AsyncSession | None = None, redis: Any | None = None) -> None:
        self._db = db
        self._redis = redis or _NullRedis()
        self._client: Any | None = None

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = get_anthropic_async_client(anthropic.AsyncAnthropic)
        return self._client

    @client.setter
    def client(self, value: Any) -> None:
        self._client = value

    async def _load_business_meta(self, business_id: str) -> tuple[str, Vertical]:
        """Retorna (display_name, vertical).

        El display_name puede degradar a un genérico (es cosmético, no entra a
        ninguna heurística); el vertical NO: sin fila de negocio levanta
        `UnknownVerticalError` en vez de scorear con las heurísticas de kiosco.
        """
        if self._db is None:
            raise RuntimeError("AgentHealth requiere sesión de DB para resolver el negocio")
        tid = uuid.UUID(business_id)
        result = await self._db.execute(
            select(Tenant.display_name, BusinessProfile.vertical_code)
            .join(BusinessProfile, BusinessProfile.tenant_id == Tenant.tenant_id)
            .where(Tenant.tenant_id == tid)
        )
        row = result.first()
        if row is None:
            raise UnknownVerticalError(
                f"El tenant {business_id} no tiene BusinessProfile: no hay vertical que aplicar."
            )
        return row.display_name or "el negocio", parse_vertical(row.vertical_code)

    def _suggest_actions(self, scores: ComponentScoresV2) -> list[str]:
        suggestions: list[str] = []
        if scores.cash_score < 60:
            suggestions.append(
                "Revisá tu cobertura de caja — considerá adelantar cobros pendientes."
            )
        if scores.stock_score < 60:
            suggestions.append("Hay productos con quiebre — generá un pedido a tus proveedores.")
        if scores.margin_score < 50:
            suggestions.append("El margen está bajo el umbral saludable — revisá precios y costos.")
        if scores.growth_score < 40:
            suggestions.append("Las ventas cayeron vs el período anterior — analizá la tendencia.")
        return suggestions[:3]

    async def process(self, request: AgentRequest, task: Any | None = None) -> AgentResponse:
        # ── Sprint 17: flujo de caja / escenarios (SIMULATE_SCENARIO) ─────────
        from app.application.agents.shared.schemas import ActionType  # noqa: PLC0415

        action_type = getattr(task, "action_type", None)
        analysis_intent = (getattr(task, "entities", {}) or {}).get("_intent")

        # ANSWER_DATA_QUERY se despacha ANTES del check de _db (el handler lo gestiona propio)
        if action_type == ActionType.ANSWER_DATA_QUERY:
            return await self._handle_data_query(request, task)

        if self._db is None:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="error",
                risk_level=RiskLevel.LOW,
                confidence="LOW",
                result={"error": "AgentHealth requiere acceso a base de datos."},
            )

        if action_type == ActionType.SIMULATE_SCENARIO:
            return await self._handle_cashflow_scenario(request, analysis_intent)

        # ── 1. Negocio + estado ───────────────────────────────────────────────
        # `_load_business_meta` levanta `UnknownVerticalError` (ValueError) cuando
        # el tenant no tiene perfil: mismo empty state honesto que `collect`, en
        # vez de informar la salud del negocio con las heurísticas de otro rubro.
        try:
            business_name, vertical = await self._load_business_meta(request.business_id)
            state = await collect(
                request.business_id, self._db, cast("Redis", self._redis)
            )
        except ValueError:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                confidence="LOW",
                result={"summary": "No se encontró perfil de negocio configurado."},
                question=(
                    "Para generar el informe necesito que completes el perfil de tu negocio. "
                    "¿Querés hacerlo ahora?"
                ),
            )

        # ── 2. ValidationGate — datos insuficientes → empty state ────────────
        # Se gatea con la confianza EFECTIVA, no solo con la de los datos: medir
        # un negocio contra una vara sin fundamento tampoco es un diagnóstico
        # confiable. Hoy ninguna procedencia alcanzable llega a LOW, así que este
        # gate no cambia de comportamiento — pero si el data-driven vuelve, vuelve
        # ya cubierto en vez de dejar el agujero abierto para que lo encuentre un
        # usuario.
        # `vertical` ya viene parseado de `_load_business_meta`: re-parsear
        # `state.vertical_code` sería hacer dos veces el mismo trabajo y agregar
        # un segundo punto donde el rubro puede fallar de forma distinta.
        confianza_efectiva = weakest_confidence(
            state.confidence_level,
            load_vertical_heuristics(vertical).margin.confidence,
        )
        if confianza_efectiva == "LOW" or state.data_completeness_score < 50:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                confidence="LOW",
                result={
                    "summary": "Necesito más datos para analizar la salud del negocio.",
                    "data_completeness": state.data_completeness_score,
                },
                question=(
                    "Para generar el informe de salud necesito que cargues "
                    "ventas del último mes y tus gastos fijos. ¿Querés hacerlo ahora?"
                ),
            )

        # ── 3. Calcular scores v2 (determinístico, sin LLM) ──────────────────
        try:
            tenant_benchmark = await get_margin_benchmark(uuid.UUID(request.business_id), self._db)
        except (ValueError, Exception):
            tenant_benchmark = None  # fallback a benchmark por vertical
        scores: ComponentScoresV2 = compute_scores(state, benchmark=tenant_benchmark)

        # ── 4. Generar narrativa con LLM ─────────────────────────────────────
        narrative, narrator_call = await generate(scores, business_name, self.client)

        # ── 5. Emitir evento de actualización ────────────────────────────────
        EventBus.emit(
            "HEALTH_SCORE_UPDATED",
            {"business_id": request.business_id, "score": scores.total_score},
        )

        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="success",
            risk_level=RiskLevel.LOW,
            message=narrative,
            result={
                "summary": narrative,
                "health_score": scores.total_score,
                "formula_version": "v2",
                "components": {
                    "cash": scores.cash_score,
                    "stock": scores.stock_score,
                    "supplier": scores.supplier_score,
                    "margin": scores.margin_score,
                    "growth": scores.growth_score,
                },
                "alerts": self._build_alerts(scores),
                "suggested_next_actions": self._suggest_actions(scores),
            },
            usage=UsageSummary(calls=[narrator_call]),
        )

    # ── Sprint 17: flujo de caja y escenarios (read-only, determinístico) ─────

    def _analysis_response(
        self,
        request: AgentRequest,
        summary: str,
        message: str,
        structured: dict[str, Any] | None = None,
        confidence: str = "HIGH",
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

    async def _handle_cashflow_scenario(
        self, request: AgentRequest, intent: str | None
    ) -> AgentResponse:
        try:
            tenant_id = uuid.UUID(request.business_id)
        except (ValueError, TypeError):
            return self._analysis_response(
                request,
                "caja_sin_datos",
                "No pude identificar tu negocio.",
            )

        # ── simular_escenario_financiero → DeterministicFinance ───────────────
        if intent == "simular_escenario_financiero":
            return await self._handle_financial_simulation(request, tenant_id)

        # ── proyectar_caja / alertar_falta_liquidez → ForecastService ─────────
        from app.application.services.forecast_service import get_forecast  # noqa: PLC0415

        assert self._db is not None  # AgentHealth.process garantiza _db no-None
        try:
            forecast = await get_forecast(tenant_id, self._db, cast("Redis", self._redis))
        except Exception:
            return self._analysis_response(
                request,
                "forecast_error",
                "No pude calcular la proyección de caja en este momento. Intentá de nuevo "
                "en un rato.",
                confidence="LOW",
            )

        if forecast.confidence == "LOW" or not forecast.points:
            return self._analysis_response(
                request,
                "forecast_sin_datos",
                "Todavía no tengo suficiente historial de ventas y gastos para proyectar tu "
                "caja con confianza. Cargá al menos unas semanas de movimientos y vuelvo a "
                "calcular.",
                confidence="LOW",
            )

        net_total = round(sum(p.net for p in forecast.points), 2)
        horizon = forecast.horizon_days
        # Días hasta saldo acumulado negativo (alerta de liquidez)
        running = 0.0
        first_negative_day: int | None = None
        for i, p in enumerate(forecast.points, start=1):
            running += p.net
            if running < 0 and first_negative_day is None:
                first_negative_day = i

        if intent == "alertar_falta_liquidez":
            try:
                vertical = await self._vertical_code(tenant_id)
            except UnknownVerticalError:
                return self._analysis_response(
                    request,
                    "negocio_sin_rubro",
                    "Todavía no tengo el rubro de tu negocio configurado, así que no puedo "
                    "aplicar los umbrales de liquidez que le corresponden. Completá el perfil "
                    "del negocio y vuelvo a calcular.",
                    confidence="LOW",
                )
            from app.application.agents.shared.heuristic_engine import (  # noqa: PLC0415
                HeuristicEngine,
            )

            critical_days = HeuristicEngine.get(vertical).cash_health.critical_days_below
            if first_negative_day is not None and first_negative_day <= horizon:
                msg = (
                    f"⚠ Alerta de liquidez: con la tendencia actual, tu caja se pondría "
                    f"negativa alrededor del día {first_negative_day}. "
                    "Conviene adelantar cobros o postergar gastos no esenciales."
                )
                return self._analysis_response(
                    request,
                    "alertar_falta_liquidez",
                    msg,
                    {
                        "first_negative_day": first_negative_day,
                        "horizon_days": horizon,
                        "net_total": net_total,
                        "critical_days_threshold": critical_days,
                        "risk_code": "CASH_LOW",
                    },
                    confidence=forecast.confidence,
                )
            return self._analysis_response(
                request,
                "liquidez_ok",
                f"No veo riesgo de quedarte sin caja en los próximos {horizon} días. "
                f"Tu flujo neto proyectado es ${net_total:,.0f}.",
                {"horizon_days": horizon, "net_total": net_total},
                confidence=forecast.confidence,
            )

        # ── proyectar_caja (default) ──────────────────────────────────────────
        signo = "positivo" if net_total >= 0 else "negativo"
        lines = [
            f"Proyección de caja a {horizon} días (confianza {forecast.confidence.lower()}):",
            f"Flujo neto estimado: ${net_total:,.0f} ({signo}).",
        ]
        if first_negative_day is not None:
            lines.append(
                f"⚠ Ojo: el saldo acumulado se pondría negativo cerca del día {first_negative_day}."
            )
        else:
            lines.append("El saldo acumulado se mantiene positivo en todo el horizonte.")
        return self._analysis_response(
            request,
            "proyectar_caja",
            "\n".join(lines),
            {
                "horizon_days": horizon,
                "net_total": net_total,
                "tier": forecast.tier,
                "first_negative_day": first_negative_day,
            },
            confidence=forecast.confidence,
        )

    async def _handle_financial_simulation(
        self, request: AgentRequest, tenant_id: uuid.UUID
    ) -> AgentResponse:
        """Simula un escenario what-if sobre ventas/gastos (cambio_pct) en memoria."""
        from app.application.services.deterministic_finance import (  # noqa: PLC0415
            calcular_flujo_neto_30d,
        )

        variable, cambio_pct = self._extract_scenario(request.message)
        assert self._db is not None  # AgentHealth.process garantiza _db no-None
        flujo = await calcular_flujo_neto_30d(tenant_id, self._db)
        ventas = float(flujo["total_ventas"])
        gastos = float(flujo["total_gastos"])
        if ventas == 0 and gastos == 0:
            return self._analysis_response(
                request,
                "simulacion_sin_datos",
                "No tengo ventas ni gastos cargados en los últimos 30 días para simular un "
                "escenario. Cargá tus movimientos y vuelvo a intentar.",
                confidence="LOW",
            )
        if cambio_pct is None:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                confidence="MEDIUM",
                question=(
                    "¿Qué escenario querés simular? Por ejemplo: 'qué pasa si vendo 20% menos' "
                    "o 'si los gastos suben 15%'."
                ),
                result={"summary": "Falta el cambio porcentual a simular."},
            )

        base_neto = ventas - gastos
        if variable == "gastos":
            new_gastos = gastos * (1 + cambio_pct / 100.0)
            new_neto = ventas - new_gastos
            detalle = f"gastos {'+' if cambio_pct >= 0 else ''}{cambio_pct:.0f}%"
        else:  # default: ventas
            new_ventas = ventas * (1 + cambio_pct / 100.0)
            new_neto = new_ventas - gastos
            detalle = f"ventas {'+' if cambio_pct >= 0 else ''}{cambio_pct:.0f}%"

        lines = [
            f"Simulación ({detalle}, base últimos 30 días):",
            f"Flujo neto actual: ${base_neto:,.0f} → escenario: ${new_neto:,.0f}.",
        ]
        if base_neto >= 0 and new_neto < 0:
            lines.append("⚠ En ese escenario tu flujo pasaría a ser negativo.")
        lines.append("Es una simulación: no modifiqué ningún dato real.")
        return self._analysis_response(
            request,
            "simular_escenario_financiero",
            "\n".join(lines),
            {
                "variable": variable,
                "cambio_pct": cambio_pct,
                "neto_actual": round(base_neto, 2),
                "neto_simulado": round(new_neto, 2),
            },
        )

    @staticmethod
    def _extract_scenario(message: str) -> tuple[str, float | None]:
        """Extrae (variable, cambio_pct) del mensaje. variable ∈ {'ventas','gastos'}."""
        import re  # noqa: PLC0415

        low = message.lower()
        variable = (
            "gastos" if any(w in low for w in ("gasto", "gastos", "costo", "costos")) else "ventas"
        )
        m = re.search(r"(\d{1,3}(?:[.,]\d+)?)\s*(?:%|por\s*ciento|porciento)", low)
        if not m:
            return variable, None
        try:
            pct = float(m.group(1).replace(",", "."))
        except ValueError:
            return variable, None
        # Signo: "menos", "baja", "cae", "reduzco" → negativo
        if any(
            w in low
            for w in ("menos", "baja", "bajan", "cae", "caen", "reduzco", "reducir", "menor")
        ):
            pct = -pct
        return variable, pct

    async def _vertical_code(self, tenant_id: uuid.UUID) -> Vertical:
        """Vertical del tenant. Sin perfil (o con código no canónico) levanta:
        una proyección de caja con los umbrales de otro rubro es peor que no
        darla."""
        if self._db is None:
            raise RuntimeError("AgentHealth requiere sesión de DB para resolver el vertical")
        return await load_tenant_vertical(self._db, tenant_id)

    def _build_alerts(self, scores: ComponentScoresV2) -> list[dict[str, str]]:
        alerts: list[dict[str, str]] = []
        if scores.cash_score < 30:
            alerts.append(
                {"type": "CRITICAL", "message": "Cobertura de caja crítica", "component": "cash"}
            )
        elif scores.cash_score < 60:
            alerts.append(
                {"type": "WARNING", "message": "Cobertura de caja baja", "component": "cash"}
            )
        if scores.stock_score < 50:
            alerts.append(
                {
                    "type": "WARNING",
                    "message": "Varios productos con quiebre de stock",
                    "component": "stock",
                }
            )
        if scores.margin_score < 40:
            alerts.append(
                {
                    "type": "WARNING",
                    "message": "Margen por debajo del umbral crítico",
                    "component": "margin",
                }
            )
        if scores.growth_score < 40:
            alerts.append(
                {
                    "type": "INFO",
                    "message": "Ventas en caída vs período anterior",
                    "component": "growth",
                }
            )
        return alerts[:3]

    # ── F5b-2: ANSWER_DATA_QUERY — consulta libre sobre caja/finanzas ─────────

    async def _handle_data_query(
        self, request: AgentRequest, task: Any | None
    ) -> AgentResponse:
        """Consulta libre sobre caja y finanzas: datos determinísticos + narrador LLM.

        Política no-invention: sin datos financieros (estado=SIN_DATOS) → mensaje
        claro sin llamar al LLM.
        structured_data siempre JSON-serializable (Decimal → float; date ya es str
        en calcular_flujo_neto_30d).
        """
        from app.application.agents.shared.data_query_narrator import (  # noqa: PLC0415
            answer_data_query,
        )
        from app.application.agents.shared.stats_enrichment import (  # noqa: PLC0415
            enrich_timeseries,
        )
        from app.application.services.deterministic_finance import (  # noqa: PLC0415
            get_financial_summary,
        )
        from app.persistence.repositories.transaction_repository import (  # noqa: PLC0415
            get_daily_net_series,
        )

        if self._db is None:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="success",
                risk_level=RiskLevel.LOW,
                requires_approval=False,
                confidence="LOW",
                message=(
                    "Necesito acceso a tus datos para responder eso. "
                    "Cargá la info y vuelvo a intentar."
                ),
            )

        try:
            tenant_id = uuid.UUID(request.business_id)
        except (ValueError, TypeError):
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="success",
                risk_level=RiskLevel.LOW,
                requires_approval=False,
                confidence="LOW",
                message=(
                    "Necesito acceso a tus datos para responder eso. "
                    "Cargá la info y vuelvo a intentar."
                ),
            )

        summary = await get_financial_summary(tenant_id, self._db)

        # Sin datos → nada que narrar
        if summary.get("estado") == "SIN_DATOS":
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="success",
                risk_level=RiskLevel.LOW,
                requires_approval=False,
                confidence="LOW",
                message=(
                    "Todavía no tengo ventas ni gastos cargados en los últimos 30 días "
                    "para responderte. Cargá tus movimientos y vuelvo a analizar."
                ),
            )

        # Coerción Decimal → float para JSON-serializable
        flujo_raw = summary.get("flujo_neto_30d", {})
        margen_raw = summary.get("margen", {})
        ticket_raw = summary.get("ticket_promedio", {})

        def _to_float(v: Any) -> float | None:
            if v is None:
                return None
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        structured_data: dict[str, Any] = {
            "resumen_financiero": {
                "total_ventas": _to_float(flujo_raw.get("total_ventas")),
                "total_gastos": _to_float(flujo_raw.get("total_gastos")),
                "flujo_neto": _to_float(flujo_raw.get("flujo_neto")),
                "periodo_dias": flujo_raw.get("periodo_dias"),
                "desde": (
                    flujo_raw.get("desde").isoformat()
                    if hasattr(flujo_raw.get("desde"), "isoformat")
                    else flujo_raw.get("desde")
                ),
                "hasta": (
                    flujo_raw.get("hasta").isoformat()
                    if hasattr(flujo_raw.get("hasta"), "isoformat")
                    else flujo_raw.get("hasta")
                ),
                "margen_pct": _to_float(margen_raw.get("margen_pct")),
                "ticket_promedio": _to_float(ticket_raw.get("ticket_promedio")),
                "n_transacciones": ticket_raw.get("n_transacciones"),
            }
        }

        # Análisis estadístico de la serie diaria de flujo neto (ventas - gastos)
        net_series = await get_daily_net_series(self._db, tenant_id, days=60)
        structured_data["analisis"] = enrich_timeseries(net_series)

        text, call = await answer_data_query(
            question=request.message,
            domain="caja",
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
            confidence="HIGH",
            message=text,
            result={
                "summary": "Consulta respondida",
                "structured_data": structured_data,
                "analysis": True,
            },
            usage=UsageSummary(calls=[call]),
        )
