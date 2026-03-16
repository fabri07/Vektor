"""
Tests for send_weekly_email job.

- test_email_content_includes_score_and_risk
- test_scheduler_configured_correctly
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.jobs.send_weekly_email import _build_html, _build_plain, _gather_email_data
from app.jobs.celery_app import celery_app
from app.persistence.db.base import Base
from app.persistence.models.business import BusinessProfile, MomentumProfile, Insight
from app.persistence.models.notification import Notification
from app.persistence.models.score import HealthScoreSnapshot, WeeklyScoreHistory
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.utils.security import hash_password

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


# ── Fixtures ───────────────────────────────────────────────────────────────────

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


@pytest_asyncio.fixture
async def tenant(session: AsyncSession) -> Tenant:
    t = Tenant(
        tenant_id=uuid.uuid4(),
        legal_name="Kiosco Test",
        display_name="Kiosco Test",
        currency="ARS",
        pricing_reference_mode="MEP",
        status="ACTIVE",
    )
    session.add(t)
    await session.commit()
    return t


@pytest_asyncio.fixture
async def owner_user(session: AsyncSession, tenant: Tenant) -> User:
    u = User(
        user_id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        email="owner@test.com",
        full_name="Test Owner",
        password_hash=hash_password("pass"),
        role_code="OWNER",
        is_active=True,
    )
    session.add(u)
    await session.commit()
    return u


@pytest_asyncio.fixture
async def health_score(session: AsyncSession, tenant: Tenant) -> HealthScoreSnapshot:
    hs = HealthScoreSnapshot(
        tenant_id=tenant.tenant_id,
        total_score=Decimal("72.00"),
        level="GOOD",
        snapshot_date=datetime.now(tz=UTC),
        triggered_by="test",
        dimensions={},
        created_at=datetime.now(tz=UTC),
    )
    session.add(hs)
    await session.commit()
    return hs


@pytest_asyncio.fixture
async def weekly_history(session: AsyncSession, tenant: Tenant) -> WeeklyScoreHistory:
    wh = WeeklyScoreHistory(
        tenant_id=tenant.tenant_id,
        week_start=date(2026, 3, 9),
        week_end=date(2026, 3, 15),
        avg_score=Decimal("72.00"),
        min_score=Decimal("68.00"),
        max_score=Decimal("75.00"),
        level="GOOD",
        delta=Decimal("5.00"),
        trend_label="IMPROVING",
        created_at=datetime.now(tz=UTC),
    )
    session.add(wh)
    await session.commit()
    return wh


@pytest_asyncio.fixture
async def insight(session: AsyncSession, tenant: Tenant) -> Insight:
    i = Insight(
        tenant_id=tenant.tenant_id,
        insight_type="CASH_FLOW",
        title="Riesgo de caja en los próximos 7 días",
        description="La cobertura de caja está por debajo del mínimo recomendado.",
        severity_code="HIGH",
    )
    session.add(i)
    await session.commit()
    return i


@pytest_asyncio.fixture
async def momentum(session: AsyncSession, tenant: Tenant) -> MomentumProfile:
    m = MomentumProfile(
        tenant_id=tenant.tenant_id,
        best_score_ever=72,
        active_goal_json={
            "weak_dimension": "cash",
            "goal": "Mejorar cobertura de caja",
            "action": "Reducir gastos variables un 10%",
            "estimated_delta": 8,
            "estimated_weeks": 3,
        },
        milestones_json=[],
        estimated_value_protected_ars=Decimal("45000.00"),
        improving_streak_weeks=1,
        updated_at=datetime.now(tz=UTC),
    )
    session.add(m)
    await session.commit()
    return m


# ── Tests ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestEmailContent:
    async def test_email_content_includes_score_and_risk(
        self,
        session: AsyncSession,
        tenant: Tenant,
        owner_user: User,
        health_score: HealthScoreSnapshot,
        weekly_history: WeeklyScoreHistory,
        insight: Insight,
        momentum: MomentumProfile,
    ) -> None:
        data = await _gather_email_data(tenant.tenant_id, session)

        assert data is not None
        assert data["score"] == 72
        assert data["delta"] == 5
        assert data["risk_title"] == "Riesgo de caja en los próximos 7 días"
        assert data["goal_text"] == "Mejorar cobertura de caja"
        assert data["action_text"] == "Reducir gastos variables un 10%"
        assert data["value_protected_ars"] == Decimal("45000.00")
        assert len(data["owner_users"]) == 1
        assert data["owner_users"][0].email == "owner@test.com"

    async def test_html_contains_score_and_business_name(self) -> None:
        html = _build_html(
            business_name="Kiosco Test",
            score=72,
            delta=5,
            risk_title="Riesgo de caja",
            goal_text="Mejorar cobertura de caja",
            action_text="Reducir gastos un 10%",
            value_protected_ars=Decimal("45000"),
            dashboard_url="https://app.vektor.com.ar/dashboard",
        )
        assert "Kiosco Test" in html
        assert "72" in html
        assert "Riesgo de caja" in html
        assert "Mejorar cobertura de caja" in html
        assert "#1A1A2E" in html
        assert "#E63946" in html
        assert "Ver mi dashboard" in html

    async def test_html_delta_positive_shows_green(self) -> None:
        html = _build_html(
            business_name="Test",
            score=80,
            delta=10,
            risk_title=None,
            goal_text=None,
            action_text=None,
            value_protected_ars=Decimal("0"),
            dashboard_url="https://example.com",
        )
        assert "#10b981" in html  # emerald green for positive delta
        assert "↑ +10" in html

    async def test_html_delta_negative_shows_red(self) -> None:
        html = _build_html(
            business_name="Test",
            score=60,
            delta=-5,
            risk_title=None,
            goal_text=None,
            action_text=None,
            value_protected_ars=Decimal("0"),
            dashboard_url="https://example.com",
        )
        assert "#ef4444" in html  # red for negative delta
        assert "↓ -5" in html

    async def test_plain_text_includes_score(self) -> None:
        plain = _build_plain(
            business_name="Kiosco Test",
            score=72,
            delta=5,
            risk_title="Riesgo de caja",
            goal_text="Mejorar cobertura de caja",
            action_text="Reducir gastos un 10%",
            value_protected_ars=Decimal("45000"),
        )
        assert "72/100" in plain
        assert "Riesgo de caja" in plain
        assert "Mejorar cobertura de caja" in plain

    async def test_gather_returns_none_if_no_health_score(
        self,
        session: AsyncSession,
        tenant: Tenant,
        owner_user: User,
    ) -> None:
        # No health score inserted — should return None
        data = await _gather_email_data(tenant.tenant_id, session)
        assert data is None


@pytest.mark.asyncio
class TestSchedulerConfiguredCorrectly:
    async def test_scheduler_has_weekly_email_task(self) -> None:
        schedule = celery_app.conf.beat_schedule
        assert "send-weekly-email-all-tenants" in schedule
        entry = schedule["send-weekly-email-all-tenants"]
        assert entry["task"] == "jobs.send_weekly_email_all_tenants"
        assert entry["options"]["queue"] == "notifications"

    async def test_scheduler_has_momentum_task(self) -> None:
        schedule = celery_app.conf.beat_schedule
        assert "update-momentum-all-tenants" in schedule

    async def test_celery_timezone_is_argentina(self) -> None:
        assert celery_app.conf.timezone == "America/Argentina/Buenos_Aires"

    async def test_weekly_email_task_registered(self) -> None:
        routes = celery_app.conf.task_routes
        assert "jobs.send_weekly_email_summary" in routes
        assert routes["jobs.send_weekly_email_summary"]["queue"] == "notifications"
        assert "jobs.send_weekly_email_all_tenants" in routes
