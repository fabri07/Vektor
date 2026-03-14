"""
Celery task: recalculate health score for a single tenant.

Flow
----
1. compute_business_state(tenant_id)   ← Business State Layer
2. calculate_health_score(state)        ← Heuristic Engine
3. Persist HealthScoreSnapshot          ← explicit subscore columns
4. Persist DecisionAuditLog             ← insert-only audit record
5. Upsert MomentumProfile               ← update best_score_ever if improved
6. Structured log                       ← tenant_id, score, confidence, duration
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime, date as _date
from decimal import Decimal
from typing import Any

from app.jobs.celery_app import celery_app
from app.observability.logger import get_logger

logger = get_logger(__name__)

HEURISTIC_VERSION = "v1"


# ── Score → level label ───────────────────────────────────────────────────────

def _score_level(score_total: int) -> str:
    if score_total >= 80:
        return "excellent"
    if score_total >= 60:
        return "healthy"
    if score_total >= 30:
        return "warning"
    return "critical"


# ── BusinessState → JSON-serializable dict ────────────────────────────────────

def _state_to_dict(state: Any) -> dict[str, Any]:
    """
    Serialize a BusinessState dataclass to a JSON-safe dict.
    Converts UUID, Decimal, and nested ProductSummary objects.
    """
    return {
        "snapshot_id": str(state.snapshot_id),
        "tenant_id": str(state.tenant_id),
        "vertical_code": state.vertical_code,
        "data_completeness_score": float(state.data_completeness_score),
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
                "product_id": str(p.product_id),
                "name": p.name,
                "stock_units": p.stock_units,
                "low_stock_threshold_units": p.low_stock_threshold_units,
                "sale_price_ars": str(p.sale_price_ars),
            }
            for p in state.products
        ],
    }


# ── HealthScoreResult → JSON-serializable dict ────────────────────────────────

def _result_to_dict(result: Any) -> dict[str, Any]:
    return {
        "score_total": result.score_total,
        "score_cash": result.score_cash,
        "score_margin": result.score_margin,
        "score_stock": result.score_stock,
        "score_supplier": result.score_supplier,
        "primary_risk_code": result.primary_risk_code,
        "risk_description": result.risk_description,
        "confidence_level": result.confidence_level,
        "data_completeness_score": result.data_completeness_score,
    }


# ── Main async implementation ─────────────────────────────────────────────────

async def _run(tenant_id_str: str) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: PLC0415
    from sqlalchemy.orm import sessionmaker  # noqa: PLC0415
    from sqlalchemy import select  # noqa: PLC0415
    from redis.asyncio import Redis  # noqa: PLC0415

    from app.config.settings import get_settings  # noqa: PLC0415
    from app.heuristics.health_engine import calculate_health_score  # noqa: PLC0415
    from app.state.business_state_service import compute_business_state  # noqa: PLC0415
    from app.persistence.models.audit import DecisionAuditLog  # noqa: PLC0415
    from app.persistence.models.business import MomentumProfile  # noqa: PLC0415
    from app.persistence.models.score import HealthScoreSnapshot  # noqa: PLC0415

    s = get_settings()
    tenant_id = uuid.UUID(tenant_id_str)
    t0 = time.monotonic()

    engine = create_async_engine(s.DATABASE_URL, pool_pre_ping=True)
    session_factory = sessionmaker(  # type: ignore[call-overload]
        engine, class_=AsyncSession, expire_on_commit=False
    )
    redis: Redis = Redis.from_url(s.REDIS_URL, decode_responses=True)

    try:
        async with session_factory() as session:
            # ── 1. Business State Layer ───────────────────────────────────────
            state = await compute_business_state(tenant_id, session, redis)

            # ── 2. Heuristic Engine ───────────────────────────────────────────
            result = calculate_health_score(state)
            now = datetime.now(UTC)

            # ── 3. Persist HealthScoreSnapshot ────────────────────────────────
            snapshot = HealthScoreSnapshot(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                total_score=Decimal(result.score_total),
                level=_score_level(result.score_total),
                dimensions={},               # legacy field — kept for compat
                triggered_by="celery:recalculate_health_score",
                snapshot_date=now,
                created_at=now,
                # F1-01 subscore columns
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
            session.add(snapshot)
            await session.flush()

            # ── 4. Persist DecisionAuditLog ───────────────────────────────────
            audit = DecisionAuditLog(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                decision_type="HEALTH_SCORE",
                decision_data={
                    "source_snapshot_id": str(state.snapshot_id),
                    "heuristic_version": HEURISTIC_VERSION,
                    "input_state_json": _state_to_dict(state),
                    "output_json": _result_to_dict(result),
                },
                triggered_by="celery:recalculate_health_score",
                actor_user_id=None,
                context={
                    "confidence_level": result.confidence_level,
                    "data_completeness_score": result.data_completeness_score,
                    "primary_risk_code": result.primary_risk_code,
                },
                created_at=now,
            )
            session.add(audit)
            await session.flush()

            # ── 5. Upsert MomentumProfile ─────────────────────────────────────
            mp_result = await session.execute(
                select(MomentumProfile).where(MomentumProfile.tenant_id == tenant_id)
            )
            momentum = mp_result.scalar_one_or_none()

            if momentum is None:
                momentum = MomentumProfile(
                    tenant_id=tenant_id,
                    best_score_ever=result.score_total,
                    best_score_date=now.date(),
                    milestones_json=[],
                    improving_streak_weeks=0,
                    updated_at=now,
                )
                session.add(momentum)
            elif momentum.best_score_ever is None or result.score_total > momentum.best_score_ever:
                momentum.best_score_ever = result.score_total
                momentum.best_score_date = now.date()
                momentum.updated_at = now

            await session.commit()

        duration_ms = int((time.monotonic() - t0) * 1000)

        # ── 6. Structured log ─────────────────────────────────────────────────
        logger.info(
            "recalculate_health_score.done",
            tenant_id=tenant_id_str,
            score_total=result.score_total,
            confidence_level=result.confidence_level,
            data_completeness_score=result.data_completeness_score,
            primary_risk_code=result.primary_risk_code,
            duration_ms=duration_ms,
        )

    finally:
        await redis.aclose()
        await engine.dispose()


# ── Celery task ───────────────────────────────────────────────────────────────

@celery_app.task(  # type: ignore[misc]
    bind=True,
    name="jobs.recalculate_health_score",
    queue="scores",
    max_retries=3,
    default_retry_delay=60,
)
def recalculate_health_score(self: Any, tenant_id: str, snapshot_id: str | None = None) -> None:
    """
    Recalculate and persist the health score for a single tenant.

    Parameters
    ----------
    tenant_id:   UUID string identifying the tenant.
    snapshot_id: Optional BSL snapshot_id hint (informational only).
    """
    try:
        asyncio.run(_run(tenant_id))
    except Exception as exc:
        logger.warning(
            "recalculate_health_score.retry",
            tenant_id=tenant_id,
            attempt=self.request.retries,
            error=str(exc),
        )
        raise self.retry(exc=exc)
