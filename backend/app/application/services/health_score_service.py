"""
Health Score Service.

Orchestrates: Business State Layer → Heuristic Engine → Score Persistence.
Scores are only recalculated when data changes (not on every request).
Every decision is logged to decision_audit_log.
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.domain.health_score import DimensionScore, HealthScore, ScoreDimension
from app.observability.logger import get_logger
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.score import HealthScoreSnapshot
from app.persistence.repositories.health_score_repository import HealthScoreRepository
from app.persistence.repositories.transaction_repository import ExpenseRepository, SaleRepository
from app.state.business_state_layer import BusinessStateLayer

logger = get_logger(__name__)
settings = get_settings()


class HealthScoreService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._score_repo = HealthScoreRepository(session)
        self._sale_repo = SaleRepository(session)
        self._expense_repo = ExpenseRepository(session)

    async def recalculate_for_tenant(
        self,
        tenant_id: uuid.UUID,
        triggered_by: str,
        actor_user_id: uuid.UUID | None = None,
    ) -> HealthScoreSnapshot:
        """
        Full recalculation pipeline:
        1. Build Business State (30-day window)
        2. Apply heuristic rules per vertical
        3. Compute composite score
        4. Persist snapshot
        5. Log decision to audit log
        """
        now = datetime.utcnow()
        period_start = now - timedelta(days=30)

        # ── 1. Business State Layer ───────────────────────────────────────────
        bsl = BusinessStateLayer(self._session)
        state = await bsl.compute(tenant_id, period_start, now)

        # ── 2. Compute dimension scores ───────────────────────────────────────
        from decimal import Decimal  # noqa: PLC0415

        dimensions = [
            DimensionScore(
                dimension=ScoreDimension.LIQUIDITY,
                value=Decimal(str(state.liquidity_score)),
                weight=Decimal("0.25"),
                explanation=state.liquidity_explanation,
            ),
            DimensionScore(
                dimension=ScoreDimension.PROFITABILITY,
                value=Decimal(str(state.profitability_score)),
                weight=Decimal("0.25"),
                explanation=state.profitability_explanation,
            ),
            DimensionScore(
                dimension=ScoreDimension.COST_CONTROL,
                value=Decimal(str(state.cost_control_score)),
                weight=Decimal("0.20"),
                explanation=state.cost_control_explanation,
            ),
            DimensionScore(
                dimension=ScoreDimension.SALES_MOMENTUM,
                value=Decimal(str(state.sales_momentum_score)),
                weight=Decimal("0.20"),
                explanation=state.sales_momentum_explanation,
            ),
            DimensionScore(
                dimension=ScoreDimension.DEBT_COVERAGE,
                value=Decimal(str(state.debt_coverage_score)),
                weight=Decimal("0.10"),
                explanation=state.debt_coverage_explanation,
            ),
        ]

        health_score = HealthScore.from_dimensions(
            tenant_id=tenant_id,
            dimensions=dimensions,
            snapshot_date=now,
            triggered_by=triggered_by,
        )

        # ── 3. Persist snapshot ───────────────────────────────────────────────
        snapshot = HealthScoreSnapshot(
            tenant_id=tenant_id,
            total_score=health_score.total_score,
            level=health_score.level.value,
            dimensions=[
                {
                    "dimension": d.dimension.value,
                    "value": str(d.value),
                    "weight": str(d.weight),
                    "weighted_value": str(d.weighted_value),
                    "explanation": d.explanation,
                }
                for d in dimensions
            ],
            triggered_by=triggered_by,
            snapshot_date=now,
            created_at=now,
        )
        await self._score_repo.save(snapshot)

        # ── 4. Audit log ──────────────────────────────────────────────────────
        audit = DecisionAuditLog(
            tenant_id=tenant_id,
            decision_type="health_score_recalculated",
            decision_data={
                "total_score": str(health_score.total_score),
                "level": health_score.level.value,
                "snapshot_id": str(snapshot.id),
            },
            triggered_by=triggered_by,
            actor_user_id=actor_user_id,
            created_at=now,
        )
        self._session.add(audit)
        await self._session.flush()

        logger.info(
            "health_score.recalculated",
            tenant_id=str(tenant_id),
            score=str(health_score.total_score),
            level=health_score.level.value,
            triggered_by=triggered_by,
        )

        return snapshot
