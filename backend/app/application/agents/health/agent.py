"""AgentHealth — calcula score de salud y genera narrativa ejecutiva.

Contrato del agente:
- Inputs: snapshots de caja, inventario, proveedores; heurística del rubro; histórico.
- Outputs: health_score (0-100), breakdown por componente, narrativa ejecutiva,
           top-3 alertas, sugerencias.
- Modelo LLM: claude-sonnet-4-6 SOLO para la narrativa. El cálculo es 100% determinístico.
- Context Budget: 4.000 tokens.

GUARDRAILS:
- No ejecuta acciones sobre el negocio.
- No confunde correlación con causalidad en la narrativa.
- El score se calcula en Python, NUNCA con LLM.
"""

import uuid
from typing import Optional

import anthropic
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.base import BaseAgent
from app.application.agents.shared.event_bus import EventBus
from app.application.agents.shared.heuristic_engine import HeuristicConfig, HeuristicEngine
from app.application.agents.shared.schemas import (
    AgentRequest,
    AgentResponse,
    RiskLevel,
)
from app.application.agents.health.scorer import (
    ComponentScores,
    HealthScore,
    compute_cash_score,
    compute_discipline_score,
    compute_health_score,
    compute_stock_score,
    compute_supplier_score,
)


class AgentHealth(BaseAgent):
    agent_name = "agent_health"

    def __init__(self, db: Optional[AsyncSession] = None) -> None:
        self.client = anthropic.AsyncAnthropic()
        self._db = db
        self._heuristics: HeuristicConfig | None = None

    def get_heuristics(self, business_type: str = "kiosco_almacen") -> HeuristicConfig:
        """Carga la heurística del rubro. Usa caché en instancia."""
        if self._heuristics is None:
            self._heuristics = HeuristicEngine.get(business_type)
        return self._heuristics

    async def calculate_health(
        self,
        business_id: str,
        business_type: str = "kiosco_almacen",
        db: Optional[AsyncSession] = None,
    ) -> HealthScore:
        """
        Paso 1: calcular todos los componentes (DETERMINÍSTICO, sin LLM).
        Prioriza el snapshot más reciente de la BD si está disponible.
        """
        effective_db = db or self._db

        if effective_db is not None:
            from app.persistence.repositories.health_score_repository import HealthScoreRepository  # noqa: PLC0415
            repo = HealthScoreRepository(effective_db)
            snapshot = await repo.get_latest(uuid.UUID(business_id))
            if snapshot is not None:
                components = ComponentScores(
                    cash_score=float(snapshot.score_cash or 70),
                    stock_score=float(snapshot.score_stock or 70),
                    supplier_score=float(snapshot.score_supplier or 70),
                    discipline_score=float(snapshot.score_margin or 70),
                )
                return HealthScore(
                    business_id=business_id,
                    health_score=float(snapshot.total_score),
                    components=components,
                    alerts=self._generate_alerts(components),
                    period=snapshot.snapshot_date.strftime("%Y-%m-%d"),
                )

        # Fallback: scorer.py con valores de muestra
        heuristics = self.get_heuristics(business_type)
        components = ComponentScores(
            cash_score=compute_cash_score(15.0, heuristics),     # 15 días de cobertura
            stock_score=compute_stock_score(0, 2, 50),            # 0 quiebres, 2 slow, 50 productos
            supplier_score=compute_supplier_score(3, 0),          # 3 activos, 0 vencidos
            discipline_score=compute_discipline_score(6, 7),      # 6 de 7 días con datos
        )
        final_score = compute_health_score(components)
        return HealthScore(
            business_id=business_id,
            health_score=round(final_score, 1),
            components=components,
            alerts=self._generate_alerts(components),
            period="current",
        )

    def _generate_alerts(self, components: ComponentScores) -> list[dict[str, str]]:
        """Generar top-3 alertas más urgentes (DETERMINÍSTICO)."""
        alerts: list[dict[str, str]] = []
        if components.cash_score < 30:
            alerts.append(
                {"type": "CRITICAL", "message": "Cobertura de caja crítica", "component": "cash"}
            )
        elif components.cash_score < 60:
            alerts.append(
                {"type": "WARNING", "message": "Cobertura de caja baja", "component": "cash"}
            )
        if components.stock_score < 50:
            alerts.append(
                {
                    "type": "WARNING",
                    "message": "Varios productos con quiebre de stock",
                    "component": "stock",
                }
            )
        if components.discipline_score < 50:
            alerts.append(
                {
                    "type": "INFO",
                    "message": "Completitud de datos mejorable",
                    "component": "discipline",
                }
            )
        return alerts[:3]  # top-3

    async def generate_narrative(self, health: HealthScore, business_name: str) -> str:
        """
        Paso 2: generar narrativa con LLM (Sonnet).
        El LLM recibe los números ya calculados, NO los calcula.
        """
        system = (
            "Sos un consultor financiero de Véktor. Generá una narrativa ejecutiva "
            "clara y accionable basada en los datos numéricos que te dan.\n\n"
            "REGLAS:\n"
            "- Usá los números dados, no los inventes.\n"
            "- Máximo 3 párrafos.\n"
            "- Priorizá las alertas más urgentes.\n"
            "- No confundas correlación con causalidad.\n"
            "- Idioma: español argentino, tono directo y profesional."
        )
        data = (
            f"Negocio: {self.wrap_user_input(business_name)}\n"
            f"Score de salud: {health.health_score}/100\n"
            f"- Caja: {health.components.cash_score:.0f}/100\n"
            f"- Inventario: {health.components.stock_score:.0f}/100\n"
            f"- Proveedores: {health.components.supplier_score:.0f}/100\n"
            f"- Disciplina operativa: {health.components.discipline_score:.0f}/100\n"
            f"Alertas: {health.alerts}"
        )
        response = await self.client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=600,
            system=system,
            messages=[{"role": "user", "content": data}],
        )
        return response.content[0].text.strip()

    async def process(self, request: AgentRequest) -> AgentResponse:
        health = await self.calculate_health(request.business_id)
        narrative = await self.generate_narrative(health, "el negocio")

        # Emitir evento de actualización
        EventBus.emit(
            "HEALTH_SCORE_UPDATED",
            {"business_id": request.business_id, "score": health.health_score},
        )

        return AgentResponse(
            request_id=request.request_id,
            agent_name=self.agent_name,
            status="success",
            risk_level=RiskLevel.LOW,
            result={
                "summary": narrative,
                "health_score": health.health_score,
                "components": health.components.model_dump(),
                "alerts": health.alerts,
                "suggested_next_actions": self._suggest_actions(health),
            },
        )

    def _suggest_actions(self, health: HealthScore) -> list[str]:
        suggestions: list[str] = []
        if health.components.cash_score < 60:
            suggestions.append(
                "Revisá tu cobertura de caja — considerá adelantar cobros pendientes."
            )
        if health.components.stock_score < 60:
            suggestions.append(
                "Hay productos con quiebre — generá un pedido a tus proveedores."
            )
        if health.components.discipline_score < 70:
            suggestions.append(
                "Cargá tus ventas diariamente para mejorar la precisión del score."
            )
        return suggestions[:3]
