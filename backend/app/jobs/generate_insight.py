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
from typing import Any

from app.jobs.celery_app import celery_app
from app.observability.logger import get_logger, log_job

logger = get_logger(__name__)

HEURISTIC_VERSION = "v1"


async def create_health_action_notifications(
    session: Any,
    tenant_id: uuid.UUID,
    title: str,
    action_text: str,
    insight_id: uuid.UUID,
    action_suggestion_id: uuid.UUID,
    risk_code: str,
    now: datetime,
) -> int:
    """Create in-app health action notifications for active tenant owners."""
    from sqlalchemy import select  # noqa: PLC0415

    from app.persistence.models.notification import Notification  # noqa: PLC0415
    from app.persistence.models.user import User  # noqa: PLC0415

    owner_result = await session.execute(
        select(User).where(
            User.tenant_id == tenant_id,
            User.role_code == "OWNER",
            User.is_active.is_(True),
        )
    )
    owner_users = list(owner_result.scalars().all())
    for user in owner_users:
        session.add(
            Notification(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                user_id=user.user_id,
                title=title,
                body=action_text,
                notification_type="health_action",
                channel="in_app",
                is_read=False,
                metadata_={
                    "action_url": "/dashboard?focus=health",
                    "insight_id": str(insight_id),
                    "action_suggestion_id": str(action_suggestion_id),
                    "risk_code": risk_code,
                },
                created_at=now,
                updated_at=now,
            )
        )
    return len(owner_users)


def should_skip_insight(confidence_level: str | None, data_completeness: float) -> bool:
    """Devuelve True cuando los datos son insuficientes para generar un insight válido."""
    return confidence_level is None or confidence_level == "LOW" or data_completeness < 50


# ── Main async implementation ─────────────────────────────────────────────────

async def _run(tenant_id_str: str) -> None:
    from redis.asyncio import Redis  # noqa: PLC0415
    from sqlalchemy import select  # noqa: PLC0415
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: PLC0415
    from sqlalchemy.orm import sessionmaker  # noqa: PLC0415

    from app.config.settings import get_settings  # noqa: PLC0415
    from app.heuristics.insight_templates import (  # noqa: PLC0415
        render_insight,
        severity_from_score,
    )
    from app.persistence.models.audit import DecisionAuditLog  # noqa: PLC0415
    from app.persistence.models.business import ActionSuggestion, Insight  # noqa: PLC0415
    from app.persistence.models.score import HealthScoreSnapshot  # noqa: PLC0415
    from app.state.business_state_service import compute_business_state  # noqa: PLC0415

    s = get_settings()
    tenant_id = uuid.UUID(tenant_id_str)
    t0 = time.monotonic()

    engine = create_async_engine(s.DATABASE_URL, pool_pre_ping=True, connect_args=s.pg_connect_args)
    session_factory = sessionmaker(  # type: ignore[call-overload]
        engine, class_=AsyncSession, expire_on_commit=False
    )
    redis: Redis = Redis.from_url(s.REDIS_URL, decode_responses=True)

    try:
        with log_job("jobs.generate_insight", tenant_id=tenant_id, logger=logger):
            async with session_factory() as session:
                # ── 1. Latest HealthScoreSnapshot ─────────────────────────────
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

                # ── 1b. Guard: sin datos suficientes no persistir nada ────────
                completeness = float(snapshot.data_completeness_score or 0)
                if should_skip_insight(snapshot.confidence_level, completeness):
                    logger.info(
                        "generate_insight.skipped_low_confidence",
                        tenant_id=tenant_id_str,
                        confidence=snapshot.confidence_level,
                        data_completeness=completeness,
                    )
                    return

                confidence = snapshot.confidence_level or ""
                risk_code: str = snapshot.primary_risk_code
                score_total: int = int(snapshot.total_score)
                severity: str = severity_from_score(score_total)

                # ── 2. BusinessState (cache or recompute) ─────────────────────
                state = await compute_business_state(tenant_id, session, redis)

                # ── 3. Render insight text ────────────────────────────────────
                title, description, action_text = render_insight(risk_code, state, snapshot)

                # ── 3b. LLM narrative (solo si confidence HIGH o MEDIUM) ──────
                if confidence in ("HIGH", "MEDIUM"):
                    try:
                        import anthropic as _anthropic  # noqa: PLC0415, I001
                        from sqlalchemy import select as _sq  # noqa: PLC0415
                        from app.integrations.anthropic_client import (  # noqa: PLC0415
                            get_anthropic_async_client,
                        )
                        from app.persistence.models.tenant import Tenant  # noqa: PLC0415

                        name_res = await session.execute(
                            _sq(Tenant.display_name).where(Tenant.tenant_id == tenant_id)
                        )
                        business_name = name_res.scalar_one_or_none() or "tu negocio"

                        _client = get_anthropic_async_client(_anthropic.AsyncAnthropic)
                        _resp = await _client.messages.create(
                            model="claude-haiku-4-5",
                            max_tokens=600,
                            system=(
                                "Sos un consultor financiero de Véktor. Generá una narrativa "
                                "ejecutiva clara y accionable basada en los datos numéricos.\n\n"
                                "REGLAS: Usá los números dados. Máximo 3 párrafos. "
                                "Priorizá las alertas urgentes. Español argentino, directo."
                            ),
                            messages=[{
                                "role": "user",
                                "content": (
                                    f"Negocio: {business_name}\n"
                                    f"Score de salud: {float(snapshot.total_score):.0f}/100\n"
                                    f"  - Liquidez/Caja: {snapshot.score_cash or 0}/100\n"
                                    f"  - Inventario: {snapshot.score_stock or 0}/100\n"
                                    f"  - Proveedores: {snapshot.score_supplier or 0}/100\n"
                                    f"  - Margen comercial: {snapshot.score_margin or 0}/100\n"
                                    f"Riesgo principal: {risk_code}\n"
                                    f"Confianza de datos: {confidence}"
                                ),
                            }],
                        )
                        narrative = _resp.content[0].text.strip()
                        if narrative:
                            description = narrative
                    except Exception as exc:
                        logger.warning(
                            "generate_insight.narrative_failed",
                            tenant_id=tenant_id_str,
                            error=str(exc),
                        )

                now = datetime.now(UTC)

                # ── 4. Persist Insight ────────────────────────────────────────
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

                # ── 5. Persist ActionSuggestion ───────────────────────────────
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

                notification_count = await create_health_action_notifications(
                    session=session,
                    tenant_id=tenant_id,
                    title=title,
                    action_text=action_text,
                    insight_id=insight.id,
                    action_suggestion_id=action.id,
                    risk_code=risk_code,
                    now=now,
                )

                # ── 6. Persist DecisionAuditLog ───────────────────────────────
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
                        "notification_count": notification_count,
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
    soft_time_limit=45,
    time_limit=60,
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
        raise self.retry(exc=exc) from exc
