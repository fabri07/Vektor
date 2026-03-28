"""
Celery task: generate one insight + one action suggestion after a health score cycle.

Flow
----
1. Read the latest HealthScoreSnapshot (if none → warn + return).
2. Resolve BusinessState from Redis cache or recompute via BSL.
3. render_insight(risk_code, state, result) → (title, description, action).
4. Persist Insight.
5. Persist ActionSuggestion linked to the Insight.
6. Persist DecisionAuditLog (decision_type="INSIGHT").
7. Structured log.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.jobs.celery_app import celery_app
from app.observability.logger import get_logger

logger = get_logger(__name__)

HEURISTIC_VERSION = "v1"


# ── Main async implementation ─────────────────────────────────────────────────

async def _run(tenant_id_str: str) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: PLC0415
    from sqlalchemy.orm import sessionmaker  # noqa: PLC0415
    from sqlalchemy import select  # noqa: PLC0415
    from redis.asyncio import Redis  # noqa: PLC0415

    from app.config.settings import get_settings  # noqa: PLC0415
    from app.heuristics.insight_templates import (  # noqa: PLC0415
        render_insight,
        severity_from_score,
    )
    from app.state.business_state_service import compute_business_state  # noqa: PLC0415
    from app.persistence.models.audit import DecisionAuditLog  # noqa: PLC0415
    from app.persistence.models.business import ActionSuggestion, Insight  # noqa: PLC0415
    from app.persistence.models.score import HealthScoreSnapshot  # noqa: PLC0415

    s = get_settings()
    tenant_id = uuid.UUID(tenant_id_str)
    t0 = time.monotonic()

    engine = create_async_engine(s.DATABASE_URL, pool_pre_ping=True, connect_args=s.pg_connect_args)
    session_factory = sessionmaker(  # type: ignore[call-overload]
        engine, class_=AsyncSession, expire_on_commit=False
    )
    redis: Redis = Redis.from_url(s.REDIS_URL, decode_responses=True)

    try:
        async with session_factory() as session:
            # ── 1. Latest HealthScoreSnapshot ─────────────────────────────────
            snap_result = await session.execute(
                select(HealthScoreSnapshot)
                .where(HealthScoreSnapshot.tenant_id == tenant_id)
                .order_by(HealthScoreSnapshot.created_at.desc())
                .limit(1)
            )
            snapshot = snap_result.scalar_one_or_none()

            if snapshot is None or snapshot.primary_risk_code is None:
                logger.warning(
                    "generate_insight.no_snapshot",
                    tenant_id=tenant_id_str,
                )
                return

            risk_code: str = snapshot.primary_risk_code
            score_total: int = int(snapshot.total_score)
            severity: str = severity_from_score(score_total)

            # ── 2. BusinessState (cache or recompute) ─────────────────────────
            state = await compute_business_state(tenant_id, session, redis)

            # ── 3. Render insight text ─────────────────────────────────────────
            title, description, action_text = render_insight(risk_code, state, snapshot)

            now = datetime.now(UTC)

            # ── 4. Persist Insight ────────────────────────────────────────────
            insight = Insight(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                insight_type=risk_code,
                title=title,
                description=description,
                severity_code=severity,
                heuristic_version=HEURISTIC_VERSION,
                created_at=now,
                updated_at=now,
            )
            session.add(insight)
            await session.flush()  # populate insight.id

            # ── 5. Persist ActionSuggestion ───────────────────────────────────
            action = ActionSuggestion(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                insight_id=insight.id,
                action_type=risk_code,
                title=action_text,
                description=action_text,
                risk_level=severity,
                status="SUGGESTED",
                created_at=now,
                updated_at=now,
            )
            session.add(action)
            await session.flush()

            # ── 6. Persist DecisionAuditLog ───────────────────────────────────
            audit = DecisionAuditLog(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                decision_type="INSIGHT",
                decision_data={
                    "risk_code": risk_code,
                    "score_total": score_total,
                    "severity_code": severity,
                    "insight_id": str(insight.id),
                    "action_suggestion_id": str(action.id),
                    "title": title,
                    "description": description,
                    "action_text": action_text,
                    "heuristic_version": HEURISTIC_VERSION,
                    "source_snapshot_id": str(snapshot.id),
                },
                triggered_by="celery:generate_insight",
                actor_user_id=None,
                context={
                    "risk_code": risk_code,
                    "severity_code": severity,
                    "confidence_level": snapshot.confidence_level or "",
                },
                created_at=now,
            )
            session.add(audit)

            await session.commit()

        duration_ms = int((time.monotonic() - t0) * 1000)

        # ── 7. Structured log ──────────────────────────────────────────────────
        logger.info(
            "generate_insight.done",
            tenant_id=tenant_id_str,
            risk_code=risk_code,
            severity=severity,
            score_total=score_total,
            duration_ms=duration_ms,
        )

    finally:
        await redis.aclose()
        await engine.dispose()


# ── Celery task ───────────────────────────────────────────────────────────────

@celery_app.task(  # type: ignore[misc]
    bind=True,
    name="jobs.generate_insight",
    queue="scores",
    max_retries=2,
    default_retry_delay=30,
)
def generate_insight(self: Any, tenant_id: str) -> None:
    """
    Generate and persist one Insight + one ActionSuggestion for a tenant.
    Called automatically after recalculate_health_score completes.
    """
    try:
        asyncio.run(_run(tenant_id))
    except Exception as exc:
        logger.warning(
            "generate_insight.retry",
            tenant_id=tenant_id,
            attempt=self.request.retries,
            error=str(exc),
        )
        raise self.retry(exc=exc)
