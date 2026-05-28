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
from typing import Any

import anthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.base import BaseAgent
from app.application.agents.health.sub_calculator import ComponentScoresV2, compute_scores
from app.application.agents.health.sub_collector import collect
from app.application.agents.health.sub_narrator import generate
from app.application.services.health_config_service import get_margin_benchmark
from app.application.agents.shared.event_bus import EventBus
from app.application.agents.shared.schemas import (
    AgentRequest,
    AgentResponse,
    RiskLevel,
    UsageSummary,
)
from app.integrations.anthropic_client import get_anthropic_async_client
from app.persistence.models.business import BusinessProfile
from app.persistence.models.tenant import Tenant

# Redis stub mínimo para sub_collector cuando no hay Redis real disponible
class _NullRedis:
    async def get(self, _key: str) -> None:
        return None

    async def set(self, _key: str, _value: str, *, nx: bool = False, ex: int | None = None) -> bool | None:
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

    async def _load_business_meta(self, business_id: str) -> tuple[str, str]:
        """Retorna (display_name, vertical_code). Fallback a valores neutros."""
        if self._db is None:
            return "el negocio", "kiosco_almacen"
        tid = uuid.UUID(business_id)
        result = await self._db.execute(
            select(Tenant.display_name, BusinessProfile.vertical_code)
            .join(BusinessProfile, BusinessProfile.tenant_id == Tenant.tenant_id)
            .where(Tenant.tenant_id == tid)
        )
        row = result.first()
        if row:
            return row.display_name, row.vertical_code
        return "el negocio", "kiosco_almacen"

    def _suggest_actions(self, scores: ComponentScoresV2) -> list[str]:
        suggestions: list[str] = []
        if scores.cash_score < 60:
            suggestions.append("Revisá tu cobertura de caja — considerá adelantar cobros pendientes.")
        if scores.stock_score < 60:
            suggestions.append("Hay productos con quiebre — generá un pedido a tus proveedores.")
        if scores.margin_score < 50:
            suggestions.append("El margen está bajo el umbral saludable — revisá precios y costos.")
        if scores.growth_score < 40:
            suggestions.append("Las ventas cayeron vs el período anterior — analizá la tendencia.")
        return suggestions[:3]

    async def process(self, request: AgentRequest, task: Any | None = None) -> AgentResponse:
        if self._db is None:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="error",
                risk_level=RiskLevel.LOW,
                confidence="LOW",
                result={"error": "AgentHealth requiere acceso a base de datos."},
            )

        business_name, _vertical = await self._load_business_meta(request.business_id)

        # ── 1. Recolectar estado de negocio ───────────────────────────────────
        try:
            state = await collect(request.business_id, self._db, self._redis)
        except ValueError as exc:
            return AgentResponse(
                request_id=request.request_id,
                agent_name=self.agent_name,
                status="requires_clarification",
                risk_level=RiskLevel.LOW,
                confidence="LOW",
                result={"summary": "No se encontró perfil de negocio configurado."},
                question="Para generar el informe necesito que completes el perfil de tu negocio. ¿Querés hacerlo ahora?",
            )

        # ── 2. ValidationGate — datos insuficientes → empty state ────────────
        if state.confidence_level == "LOW" or state.data_completeness_score < 50:
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

    def _build_alerts(self, scores: ComponentScoresV2) -> list[dict[str, str]]:
        alerts: list[dict[str, str]] = []
        if scores.cash_score < 30:
            alerts.append({"type": "CRITICAL", "message": "Cobertura de caja crítica", "component": "cash"})
        elif scores.cash_score < 60:
            alerts.append({"type": "WARNING", "message": "Cobertura de caja baja", "component": "cash"})
        if scores.stock_score < 50:
            alerts.append({"type": "WARNING", "message": "Varios productos con quiebre de stock", "component": "stock"})
        if scores.margin_score < 40:
            alerts.append({"type": "WARNING", "message": "Margen por debajo del umbral crítico", "component": "margin"})
        if scores.growth_score < 40:
            alerts.append({"type": "INFO", "message": "Ventas en caída vs período anterior", "component": "growth"})
        return alerts[:3]
