"""
Health Score Service.

Orchestrates: Business State Layer → Heuristic Engine → Score Persistence.
Scores are only recalculated when data changes (not on every request).
Every decision is logged to decision_audit_log.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.heuristics.health_engine import HealthScoreResult, calculate_health_score
from app.observability.logger import get_logger
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.score import HealthScoreSnapshot
from app.persistence.repositories.health_score_repository import HealthScoreRepository
from app.state.business_state_service import BusinessState, compute_business_state

logger = get_logger(__name__)

HEURISTIC_VERSION = "v1"


class _NullRedis:
    """Minimal async Redis stub used by synchronous score recalculation paths."""

    async def get(self, _key: str) -> None:
        return None

    async def set(
        self,
        _key: str,
        _value: str,
        *,
        nx: bool = False,
        ex: int | None = None,
    ) -> bool | None:
        return True if not nx else True

    async def aclose(self) -> None:
        return None


def _score_level(score_total: int) -> str:
    if score_total >= 80:
        return "excellent"
    if score_total >= 60:
        return "healthy"
    if score_total >= 30:
        return "warning"
    return "critical"


def _state_to_dict(state: BusinessState) -> dict[str, Any]:
    return {
        "snapshot_id": str(state.snapshot_id),
        "tenant_id": str(state.tenant_id),
        "vertical_code": state.vertical_code,
        "data_completeness_score": state.data_completeness_score,
        "confidence_level": state.confidence_level,
        "monthly_sales_est": str(state.monthly_sales_est),
        "monthly_inventory_cost_est": str(state.monthly_inventory_cost_est),
        "monthly_fixed_expenses_est": str(state.monthly_fixed_expenses_est),
        "cash_on_hand_est": str(state.cash_on_hand_est),
        "product_count": state.product_count,
        "supplier_count": state.supplier_count,
        "main_concern": state.main_concern,
        "products": [
            {
                "product_id": str(product.product_id),
                "name": product.name,
                "stock_units": product.stock_units,
                "low_stock_threshold_units": product.low_stock_threshold_units,
                "sale_price_ars": str(product.sale_price_ars),
            }
            for product in state.products
        ],
    }


def _build_dimensions(
    state: BusinessState,
    result: HealthScoreResult,
) -> list[dict[str, str]]:
    if state.monthly_fixed_expenses_est > 0:
        cash_ratio = state.cash_on_hand_est / state.monthly_fixed_expenses_est
        cash_explanation = (
            f"Caja estimada {state.cash_on_hand_est:.2f} ARS, "
            f"equivale a {cash_ratio:.2f}x de gastos fijos."
        )
    else:
        cash_explanation = "Sin gastos fijos cargados; score de caja neutral."

    if state.monthly_sales_est > 0:
        margin_pct = (
            (
                state.monthly_sales_est
                - state.monthly_inventory_cost_est
                - state.monthly_fixed_expenses_est
            )
            / state.monthly_sales_est
        ) * Decimal("100")
        margin_explanation = f"Margen estimado mensual de {margin_pct:.1f}%."
    else:
        margin_explanation = "Sin ventas estimadas; score de margen en mínimo."

    if state.products:
        low_stock_count = sum(
            1
            for product in state.products
            if product.stock_units <= product.low_stock_threshold_units
        )
        stock_explanation = (
            f"{low_stock_count} de {len(state.products)} productos están en stock bajo."
        )
    else:
        stock_explanation = "Sin catálogo cargado; score de stock neutral."

    supplier_explanation = (
        f"Base estimada de {state.supplier_count} proveedores activos."
        if state.supplier_count > 0
        else "Sin proveedores cargados; score de proveedores en mínimo."
    )

    return [
        {
            "dimension": "cash",
            "value": str(result.score_cash),
            "weight": "0.30",
            "weighted_value": f"{Decimal(result.score_cash) * Decimal('0.30'):.3f}",
            "explanation": cash_explanation,
        },
        {
            "dimension": "margin",
            "value": str(result.score_margin),
            "weight": "0.30",
            "weighted_value": f"{Decimal(result.score_margin) * Decimal('0.30'):.3f}",
            "explanation": margin_explanation,
        },
        {
            "dimension": "stock",
            "value": str(result.score_stock),
            "weight": "0.25",
            "weighted_value": f"{Decimal(result.score_stock) * Decimal('0.25'):.3f}",
            "explanation": stock_explanation,
        },
        {
            "dimension": "supplier",
            "value": str(result.score_supplier),
            "weight": "0.15",
            "weighted_value": f"{Decimal(result.score_supplier) * Decimal('0.15'):.3f}",
            "explanation": supplier_explanation,
        },
    ]


class HealthScoreService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._score_repo = HealthScoreRepository(session)

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
        now = datetime.now(UTC)
        redis = _NullRedis()

        # ── 1. Business State Layer ───────────────────────────────────────────
        state = await compute_business_state(tenant_id, self._session, redis)

        # ── 2. Heuristic Engine (F1-01) ───────────────────────────────────────
        result = calculate_health_score(state)
        dimensions = _build_dimensions(state, result)

        # ── 3. Persist snapshot ───────────────────────────────────────────────
        snapshot = HealthScoreSnapshot(
            tenant_id=tenant_id,
            total_score=Decimal(result.score_total),
            level=_score_level(result.score_total),
            dimensions=dimensions,
            triggered_by=triggered_by,
            snapshot_date=now,
            created_at=now,
            score_cash=result.score_cash,
            score_margin=result.score_margin,
            score_stock=result.score_stock,
            score_supplier=result.score_supplier,
            source_snapshot_id=state.snapshot_id,
            heuristic_version=HEURISTIC_VERSION,
            primary_risk_code=result.primary_risk_code,
            confidence_level=result.confidence_level,
            data_completeness_score=Decimal(str(result.data_completeness_score)),
            score_inputs_json=_state_to_dict(state),
        )
        await self._score_repo.save(snapshot)

        # ── 4. Audit log ──────────────────────────────────────────────────────
        audit = DecisionAuditLog(
            tenant_id=tenant_id,
            decision_type="health_score_recalculated",
            decision_data={
                "total_score": result.score_total,
                "level": snapshot.level,
                "snapshot_id": str(snapshot.id),
                "primary_risk_code": result.primary_risk_code,
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
            score=result.score_total,
            level=snapshot.level,
            triggered_by=triggered_by,
        )

        await redis.aclose()
        return snapshot
