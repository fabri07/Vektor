"""
Tests for the Momentum Engine (update_momentum.py).

All tests call run_momentum_update() directly — no Celery runner needed.
Uses SQLite in-memory DB, no real infrastructure.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.jobs.update_momentum import compute_trend_label, run_momentum_update, _current_week
from app.persistence.db.base import Base
from app.persistence.models.business import BusinessProfile, MomentumProfile
from app.persistence.models.notification import Notification
from app.persistence.models.score import HealthScoreSnapshot, WeeklyScoreHistory
from app.persistence.models.tenant import Tenant

# ── Test DB setup ──────────────────────────────────────────────────────────────

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def engine() -> AsyncEngine:
    eng = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncSession:
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s


# ── Helpers ────────────────────────────────────────────────────────────────────

def _tenant() -> Tenant:
    return Tenant(
        tenant_id=uuid.uuid4(),
        legal_name="Test PYME",
        display_name="Test PYME",
        currency="ARS",
        pricing_reference_mode="MEP",
        status="ACTIVE",
    )


def _score(
    tenant_id: uuid.UUID,
    total: int,
    cash: int = 60,
    margin: int = 60,
    stock: int = 60,
    supplier: int = 60,
    primary_risk_code: str = "NONE",
    days_ago: int = 0,
) -> HealthScoreSnapshot:
    created = datetime.now(tz=UTC) - timedelta(days=days_ago)
    return HealthScoreSnapshot(
        tenant_id=tenant_id,
        total_score=Decimal(total),
        level="good",
        dimensions={},
        triggered_by="test",
        snapshot_date=created,
        created_at=created,
        score_cash=cash,
        score_margin=margin,
        score_stock=stock,
        score_supplier=supplier,
        primary_risk_code=primary_risk_code,
        confidence_level="HIGH",
        data_completeness_score=Decimal("90"),
        heuristic_version="v1",
    )


def _week_history(
    tenant_id: uuid.UUID,
    week_start: date,
    avg: Decimal,
    trend_label: str = "stable",
) -> WeeklyScoreHistory:
    return WeeklyScoreHistory(
        tenant_id=tenant_id,
        week_start=week_start,
        week_end=week_start + timedelta(days=6),
        avg_score=avg,
        min_score=avg,
        max_score=avg,
        level="good",
        delta=Decimal("0"),
        trend_label=trend_label,
        created_at=datetime.now(tz=UTC),
    )


# ── Tests ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_weekly_delta_calculated_correctly(session: AsyncSession) -> None:
    """
    Given a score of 70 this week and a previous week history of 60,
    the weekly delta should be 10 and the inserted row should reflect it.
    """
    tenant = _tenant()
    session.add(tenant)
    await session.commit()

    week_start, week_end = _current_week()
    prev_start = week_start - timedelta(days=7)

    # Previous week history row (avg = 60)
    session.add(_week_history(tenant.tenant_id, prev_start, Decimal("60")))

    # This week's score snapshot (70)
    session.add(_score(tenant.tenant_id, total=70, days_ago=0))
    await session.commit()

    await run_momentum_update(tenant.tenant_id, session)

    result = await session.execute(
        select(WeeklyScoreHistory).where(
            WeeklyScoreHistory.tenant_id == tenant.tenant_id,
            WeeklyScoreHistory.week_start == week_start,
        )
    )
    row = result.scalar_one()

    assert float(row.avg_score) == pytest.approx(70.0)
    assert float(row.delta) == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_trend_label_improving(session: AsyncSession) -> None:
    """
    delta >= 3 should produce trend_label = 'improving'.
    delta in [-2, 3) should produce 'stable'.
    delta < -2 should produce 'declining'.
    """
    assert compute_trend_label(Decimal("3")) == "improving"
    assert compute_trend_label(Decimal("10")) == "improving"
    assert compute_trend_label(Decimal("2")) == "stable"
    assert compute_trend_label(Decimal("-2")) == "stable"
    assert compute_trend_label(Decimal("-3")) == "declining"
    assert compute_trend_label(Decimal("-10")) == "declining"


@pytest.mark.asyncio
async def test_milestone_m1_unlocks_on_first_improvement(session: AsyncSession) -> None:
    """
    M1 should unlock when delta > 0 and was not already in milestones_json.
    A notification should also be created.
    """
    tenant = _tenant()
    session.add(tenant)
    await session.commit()

    week_start, _ = _current_week()
    prev_start = week_start - timedelta(days=7)

    # Previous week avg = 50, this week = 60 → delta = 10 → improving
    session.add(_week_history(tenant.tenant_id, prev_start, Decimal("50")))
    session.add(_score(tenant.tenant_id, total=60, days_ago=0))
    await session.commit()

    await run_momentum_update(tenant.tenant_id, session)

    mp_q = await session.execute(
        select(MomentumProfile).where(MomentumProfile.tenant_id == tenant.tenant_id)
    )
    mp = mp_q.scalar_one()

    milestone_codes = [m["code"] for m in mp.milestones_json]
    assert "M1" in milestone_codes

    notif_q = await session.execute(
        select(Notification).where(
            Notification.tenant_id == tenant.tenant_id,
            Notification.notification_type == "milestone",
        )
    )
    notifications = notif_q.scalars().all()
    assert any("M1" in n.title or "Primera" in n.body for n in notifications)


@pytest.mark.asyncio
async def test_value_protected_increases_with_margin_improvement(session: AsyncSession) -> None:
    """
    When margin score improves from 30 days ago and monthly_sales_estimate is set,
    estimated_value_protected_ars should increase.
    """
    tenant = _tenant()
    session.add(tenant)

    # Business profile with monthly sales estimate
    bp = BusinessProfile(
        tenant_id=tenant.tenant_id,
        vertical_code="kiosco",
        data_mode="M1",
        data_confidence="HIGH",
        monthly_sales_estimate_ars=Decimal("100000"),
        onboarding_completed=True,
    )
    session.add(bp)
    await session.commit()

    week_start, _ = _current_week()

    # Score 31 days ago with low margin (30)
    session.add(_score(tenant.tenant_id, total=50, margin=30, days_ago=31))
    # Current score with high margin (70) — delta_margin = 40
    session.add(_score(tenant.tenant_id, total=70, margin=70, days_ago=0))
    await session.commit()

    await run_momentum_update(tenant.tenant_id, session)

    mp_q = await session.execute(
        select(MomentumProfile).where(MomentumProfile.tenant_id == tenant.tenant_id)
    )
    mp = mp_q.scalar_one()

    # margin_recovered = 100_000 * (40 / 100) * 0.30 = 12_000
    assert mp.estimated_value_protected_ars is not None
    assert float(mp.estimated_value_protected_ars) > 0
