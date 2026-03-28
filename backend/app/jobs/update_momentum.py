"""
Celery task: update_momentum_profile.

Runs weekly (via Celery Beat) for each active tenant.
Computes:
  1. Weekly snapshot + trend label  → weekly_score_history
  2. Best score ever                → momentum_profiles.best_score_ever
  3. Dynamic goal (active_goal_json)→ momentum_profiles.active_goal_json
  4. Milestone evaluation           → momentum_profiles.milestones_json + notifications
  5. Estimated value protected      → momentum_profiles.estimated_value_protected_ars

v1: Celery Beat fires for ALL tenants simultaneously using hardcoded defaults
    (Monday, 08:00 Argentina). Per-tenant scheduling (weekly_report_day /
    weekly_report_hour in business_profiles) is data-ready but not yet wired
    to the scheduler.
    # TODO: implementar scheduler por tenant usando weekly_report_day
    # y weekly_report_hour de business_profiles
"""

from __future__ import annotations

import asyncio
import uuid as _uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from app.jobs.celery_app import celery_app
from app.observability.logger import get_logger

logger = get_logger(__name__)

# ── Goal templates per weak dimension ────────────────────────────────────────

_GOAL_TEMPLATES: dict[str, dict[str, Any]] = {
    "cash": {
        "goal": "Mejorar cobertura de caja",
        "action": "Reducir gastos variables un 10%",
        "estimated_delta": 8,
        "estimated_weeks": 3,
    },
    "margin": {
        "goal": "Mejorar margen operativo",
        "action": "Revisar precios de productos de mayor volumen",
        "estimated_delta": 6,
        "estimated_weeks": 2,
    },
    "stock": {
        "goal": "Reducir riesgo de quiebre de stock",
        "action": "Reponer productos bajo umbral mínimo",
        "estimated_delta": 5,
        "estimated_weeks": 1,
    },
    "supplier": {
        "goal": "Diversificar proveedores",
        "action": "Incorporar al menos 1 proveedor alternativo",
        "estimated_delta": 10,
        "estimated_weeks": 4,
    },
}

# ── Milestone definitions ─────────────────────────────────────────────────────

_MILESTONE_DEFS = [
    ("M1", "Primera semana de mejora"),
    ("M2", "3 semanas consecutivas mejorando"),
    ("M3", "Score de margen ≥ 70 por 14 días"),
    ("M4", "14 días sin riesgo de stock crítico"),
]

_CASH_ALERTS_AVOIDED_ARS = Decimal("500")


# ── Helper: current week boundaries (Monday–Sunday, UTC-3) ───────────────────

def _current_week(ref: date | None = None) -> tuple[date, date]:
    today = ref or datetime.now(tz=timezone.utc).date()
    # weekday(): 0=Monday … 6=Sunday
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)
    return week_start, week_end


def _previous_week(ref: date | None = None) -> tuple[date, date]:
    week_start, _ = _current_week(ref)
    prev_end = week_start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=6)
    return prev_start, prev_end


# ── Trend label ───────────────────────────────────────────────────────────────

def compute_trend_label(delta: Decimal) -> str:
    if delta >= 3:
        return "improving"
    if delta >= -2:
        return "stable"
    return "declining"


# ── Core async logic (extracted for testability) ──────────────────────────────

async def run_momentum_update(tenant_id: _uuid.UUID, session: Any) -> None:  # noqa: ANN401
    """All 5 momentum steps. `session` is an AsyncSession."""
    from sqlalchemy import func, select  # noqa: PLC0415

    from app.persistence.models.business import BusinessProfile, MomentumProfile  # noqa: PLC0415
    from app.persistence.models.notification import Notification  # noqa: PLC0415
    from app.persistence.models.score import HealthScoreSnapshot, WeeklyScoreHistory  # noqa: PLC0415

    now_utc = datetime.now(tz=timezone.utc)
    today = now_utc.date()

    # ── 1. WEEKLY SNAPSHOT ────────────────────────────────────────────────────

    week_start, week_end = _current_week(today)
    week_start_dt = datetime(week_start.year, week_start.month, week_start.day, tzinfo=timezone.utc)
    week_end_dt = datetime(week_end.year, week_end.month, week_end.day, 23, 59, 59, tzinfo=timezone.utc)

    scores_this_week_q = await session.execute(
        select(HealthScoreSnapshot.total_score)
        .where(
            HealthScoreSnapshot.tenant_id == tenant_id,
            HealthScoreSnapshot.created_at >= week_start_dt,
            HealthScoreSnapshot.created_at <= week_end_dt,
        )
        .order_by(HealthScoreSnapshot.created_at)
    )
    this_week_scores: list[Decimal] = list(scores_this_week_q.scalars().all())

    if not this_week_scores:
        logger.info("momentum.no_scores_this_week", tenant_id=str(tenant_id))
        return

    avg_score = Decimal(sum(this_week_scores)) / Decimal(len(this_week_scores))
    min_score = min(this_week_scores)
    max_score = max(this_week_scores)

    # Determine level from avg
    avg_int = int(avg_score)
    if avg_int >= 80:
        level = "excellent"
    elif avg_int >= 60:
        level = "good"
    elif avg_int >= 40:
        level = "warning"
    else:
        level = "critical"

    # Previous week avg for delta
    prev_start, prev_end = _previous_week(today)
    prev_q = await session.execute(
        select(WeeklyScoreHistory.avg_score)
        .where(
            WeeklyScoreHistory.tenant_id == tenant_id,
            WeeklyScoreHistory.week_start == prev_start,
        )
    )
    prev_row = prev_q.scalar_one_or_none()
    delta = (avg_score - prev_row).quantize(Decimal("0.01")) if prev_row is not None else Decimal("0")
    trend_label = compute_trend_label(delta)

    # Upsert: check if row for this week already exists
    existing_q = await session.execute(
        select(WeeklyScoreHistory).where(
            WeeklyScoreHistory.tenant_id == tenant_id,
            WeeklyScoreHistory.week_start == week_start,
        )
    )
    existing = existing_q.scalar_one_or_none()

    if existing is None:
        week_row = WeeklyScoreHistory(
            tenant_id=tenant_id,
            week_start=week_start,
            week_end=week_end,
            avg_score=avg_score,
            min_score=min_score,
            max_score=max_score,
            level=level,
            delta=delta,
            trend_label=trend_label,
            created_at=now_utc,
        )
        session.add(week_row)
    else:
        existing.avg_score = avg_score
        existing.min_score = min_score
        existing.max_score = max_score
        existing.level = level
        existing.delta = delta
        existing.trend_label = trend_label

    # ── 2. BEST SCORE ─────────────────────────────────────────────────────────

    # Fetch or create MomentumProfile
    mp_q = await session.execute(
        select(MomentumProfile).where(MomentumProfile.tenant_id == tenant_id)
    )
    mp: MomentumProfile | None = mp_q.scalar_one_or_none()

    if mp is None:
        mp = MomentumProfile(
            tenant_id=tenant_id,
            best_score_ever=None,
            best_score_date=None,
            active_goal_json=None,
            milestones_json=[],
            estimated_value_protected_ars=Decimal("0"),
            improving_streak_weeks=0,
            updated_at=now_utc,
        )
        session.add(mp)
        await session.flush()  # assign PK before updating fields

    avg_int_score = int(avg_score.to_integral_value())
    if mp.best_score_ever is None or avg_int_score > mp.best_score_ever:
        mp.best_score_ever = avg_int_score
        mp.best_score_date = today

    # ── 3. DYNAMIC GOAL ───────────────────────────────────────────────────────

    latest_score_q = await session.execute(
        select(HealthScoreSnapshot)
        .where(HealthScoreSnapshot.tenant_id == tenant_id)
        .order_by(HealthScoreSnapshot.created_at.desc())
        .limit(1)
    )
    latest: HealthScoreSnapshot | None = latest_score_q.scalar_one_or_none()

    if latest is not None:
        subscores: dict[str, int] = {
            "cash": latest.score_cash or 0,
            "margin": latest.score_margin or 0,
            "stock": latest.score_stock or 0,
            "supplier": latest.score_supplier or 0,
        }
        weak_dim = min(subscores, key=lambda k: subscores[k])
        template = _GOAL_TEMPLATES[weak_dim]
        mp.active_goal_json = {"weak_dimension": weak_dim, **template}

    # ── 4. MILESTONES ─────────────────────────────────────────────────────────

    already_unlocked: set[str] = {m["code"] for m in (mp.milestones_json or [])}
    new_milestones: list[dict[str, Any]] = list(mp.milestones_json or [])
    new_notifications: list[Notification] = []

    def _unlock(code: str, label: str) -> None:
        if code in already_unlocked:
            return
        entry: dict[str, Any] = {
            "code": code,
            "label": label,
            "unlocked_at": now_utc.isoformat(),
        }
        new_milestones.append(entry)
        already_unlocked.add(code)
        notif = Notification(
            tenant_id=tenant_id,
            title=f"Hito desbloqueado: {label}",
            body=f"¡Felicitaciones! Desbloqueaste el hito «{label}».",
            notification_type="milestone",
            channel="in_app",
        )
        new_notifications.append(notif)
        logger.info("momentum.milestone_unlocked", code=code, tenant_id=str(tenant_id))

    # M1: first week with delta > 0
    if delta > 0:
        _unlock("M1", "Primera semana de mejora")

    # M2: 3 consecutive improving weeks
    last_3_q = await session.execute(
        select(WeeklyScoreHistory.trend_label)
        .where(
            WeeklyScoreHistory.tenant_id == tenant_id,
            WeeklyScoreHistory.week_start < week_start,
        )
        .order_by(WeeklyScoreHistory.week_start.desc())
        .limit(2)
    )
    prev_2_labels = list(last_3_q.scalars().all())
    if (
        trend_label == "improving"
        and len(prev_2_labels) == 2
        and all(lbl == "improving" for lbl in prev_2_labels)
    ):
        _unlock("M2", "3 semanas consecutivas mejorando")

    # M3: score_margin >= 70 for 14 days continuously
    if latest is not None and (latest.score_margin or 0) >= 70:
        cutoff_14 = datetime(today.year, today.month, today.day, tzinfo=timezone.utc) - timedelta(days=14)
        low_margin_q = await session.execute(
            select(func.count())
            .select_from(HealthScoreSnapshot)
            .where(
                HealthScoreSnapshot.tenant_id == tenant_id,
                HealthScoreSnapshot.created_at >= cutoff_14,
                HealthScoreSnapshot.score_margin < 70,
            )
        )
        low_margin_count: int = low_margin_q.scalar_one()
        if low_margin_count == 0:
            _unlock("M3", "Score de margen ≥ 70 por 14 días")

    # M4: 14 days without primary_risk_code = STOCK_CRITICAL
    cutoff_14 = datetime(today.year, today.month, today.day, tzinfo=timezone.utc) - timedelta(days=14)
    stock_critical_q = await session.execute(
        select(func.count())
        .select_from(HealthScoreSnapshot)
        .where(
            HealthScoreSnapshot.tenant_id == tenant_id,
            HealthScoreSnapshot.created_at >= cutoff_14,
            HealthScoreSnapshot.primary_risk_code == "STOCK_CRITICAL",
        )
    )
    stock_critical_count: int = stock_critical_q.scalar_one()
    if stock_critical_count == 0:
        _unlock("M4", "14 días sin riesgo de stock crítico")

    mp.milestones_json = new_milestones
    for n in new_notifications:
        session.add(n)

    # Update improving streak
    if trend_label == "improving":
        mp.improving_streak_weeks = (mp.improving_streak_weeks or 0) + 1
    else:
        mp.improving_streak_weeks = 0

    # ── 5. ESTIMATED VALUE PROTECTED ──────────────────────────────────────────

    # margin_recovered = monthly_sales_est * (delta_margin / 100) * 0.30
    delta_margin = Decimal("0")
    if latest is not None:
        cutoff_30 = datetime(today.year, today.month, today.day, tzinfo=timezone.utc) - timedelta(days=30)
        margin_30_q = await session.execute(
            select(HealthScoreSnapshot.score_margin)
            .where(
                HealthScoreSnapshot.tenant_id == tenant_id,
                HealthScoreSnapshot.created_at <= cutoff_30,
            )
            .order_by(HealthScoreSnapshot.created_at.desc())
            .limit(1)
        )
        margin_30 = margin_30_q.scalar_one_or_none()
        if margin_30 is not None and latest.score_margin is not None:
            delta_margin = Decimal(latest.score_margin) - Decimal(margin_30)

    margin_recovered = Decimal("0")
    profile_q = await session.execute(
        select(BusinessProfile.monthly_sales_estimate_ars)
        .where(BusinessProfile.tenant_id == tenant_id)
    )
    monthly_sales = profile_q.scalar_one_or_none()
    if monthly_sales and delta_margin > 0:
        margin_recovered = monthly_sales * (delta_margin / Decimal("100")) * Decimal("0.30")

    # cash_alerts_avoided: weeks in history without CASH_LOW in primary_risk_code
    all_weeks_q = await session.execute(
        select(WeeklyScoreHistory.week_start)
        .where(WeeklyScoreHistory.tenant_id == tenant_id)
    )
    all_week_starts: list[date] = list(all_weeks_q.scalars().all())

    # Count distinct ISO weeks that had at least one CASH_LOW snapshot
    cash_low_dates_q = await session.execute(
        select(HealthScoreSnapshot.created_at)
        .where(
            HealthScoreSnapshot.tenant_id == tenant_id,
            HealthScoreSnapshot.primary_risk_code == "CASH_LOW",
        )
    )
    cash_low_dates = list(cash_low_dates_q.scalars().all())
    cash_low_week_count = len({
        (d.isocalendar()[0], d.isocalendar()[1]) for d in cash_low_dates
    })
    cash_safe_weeks = max(0, len(all_week_starts) - cash_low_week_count)
    cash_alerts_avoided = Decimal(cash_safe_weeks) * _CASH_ALERTS_AVOIDED_ARS

    current_protected = mp.estimated_value_protected_ars or Decimal("0")
    mp.estimated_value_protected_ars = (
        current_protected + margin_recovered + cash_alerts_avoided
    ).quantize(Decimal("0.01"))

    mp.updated_at = now_utc
    await session.commit()

    logger.info(
        "momentum.updated",
        tenant_id=str(tenant_id),
        avg_score=float(avg_score),
        delta=float(delta),
        trend_label=trend_label,
        milestones_new=len(new_notifications),
    )


# ── Celery task ────────────────────────────────────────────────────────────────


@celery_app.task(  # type: ignore[misc]
    name="jobs.update_momentum_profile",
    queue="scores",
    max_retries=3,
    default_retry_delay=60,
)
def update_momentum_profile(tenant_id: str) -> None:
    """
    Update momentum profile for a single tenant.
    Called per-tenant by the Beat schedule or directly after a score recalculation.
    """
    from app.config.settings import get_settings  # noqa: PLC0415

    s = get_settings()

    async def _run() -> None:
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: PLC0415
        from sqlalchemy.orm import sessionmaker  # noqa: PLC0415

        engine = create_async_engine(s.DATABASE_URL, pool_pre_ping=True, connect_args=s.pg_connect_args)
        factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)  # type: ignore[call-overload]

        async with factory() as session:
            await run_momentum_update(_uuid.UUID(tenant_id), session)

        await engine.dispose()

    asyncio.run(_run())


@celery_app.task(  # type: ignore[misc]
    name="jobs.update_momentum_all_tenants",
    queue="scores",
    max_retries=2,
    default_retry_delay=120,
)
def update_momentum_all_tenants() -> None:
    """
    Periodic task dispatched by Celery Beat (weekly, Monday 08:00 ART).
    Fans out one update_momentum_profile task per active tenant.

    v1: runs all tenants at the same time with default schedule.
    TODO: implementar scheduler por tenant usando weekly_report_day
    y weekly_report_hour de business_profiles
    """
    from app.config.settings import get_settings  # noqa: PLC0415

    s = get_settings()

    async def _collect_tenants() -> list[str]:
        from sqlalchemy import select  # noqa: PLC0415
        from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: PLC0415
        from sqlalchemy.orm import sessionmaker  # noqa: PLC0415

        from app.persistence.models.tenant import Tenant  # noqa: PLC0415

        engine = create_async_engine(s.DATABASE_URL, pool_pre_ping=True, connect_args=s.pg_connect_args)
        factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)  # type: ignore[call-overload]

        async with factory() as session:
            result = await session.execute(
                select(Tenant.tenant_id).where(Tenant.status.in_(["active", "trial", "ACTIVE"]))
            )
            ids = [str(tid) for tid in result.scalars().all()]

        await engine.dispose()
        return ids

    tenant_ids = asyncio.run(_collect_tenants())
    logger.info("momentum.fan_out", tenant_count=len(tenant_ids))

    for tid in tenant_ids:
        update_momentum_profile.delay(tid)
