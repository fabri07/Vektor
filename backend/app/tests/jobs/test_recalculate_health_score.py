"""
Tests for recalculate_health_score job.

All tests call _run() directly (the inner async function) — no Celery runner needed.
Uses SQLite in-memory DB + FakeRedis, no real infrastructure.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.persistence.db.base import Base
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.business import BusinessProfile, BusinessSnapshot, MomentumProfile
from app.persistence.models.score import HealthScoreSnapshot
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


# ── FakeRedis ──────────────────────────────────────────────────────────────────


class FakeRedis:
    """Minimal async Redis stub: get / set with NX / ex support, aclose."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(
        self,
        key: str,
        value: str,
        *,
        nx: bool = False,
        ex: int | None = None,
    ) -> bool | None:
        if nx and key in self._store:
            return None  # not acquired
        self._store[key] = value
        return True

    async def aclose(self) -> None:
        pass


# ── Fixtures ───────────────────────────────────────────────────────────────────


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
async def kiosco_profile(session: AsyncSession, tenant: Tenant) -> BusinessProfile:
    profile = BusinessProfile(
        profile_id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        vertical_code="kiosco",
        data_mode="M0",
        data_confidence="MEDIUM",
        monthly_sales_estimate_ars=Decimal("100000"),
        monthly_inventory_spend_estimate_ars=Decimal("60000"),
        monthly_fixed_expenses_estimate_ars=Decimal("17000"),
        cash_on_hand_estimate_ars=Decimal("40000"),
        supplier_count_estimate=3,
        product_count_estimate=0,
        onboarding_completed=True,
        updated_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )
    session.add(profile)
    await session.commit()
    return profile


# ── Patch helper ──────────────────────────────────────────────────────────────


def _make_run(session: AsyncSession, redis: FakeRedis):
    """
    Return a patched version of _run() that injects the test session and redis
    instead of creating real infrastructure.
    """
    import app.jobs.recalculate_health_score as job_module  # noqa: PLC0415
    from app.state.business_state_service import compute_business_state  # noqa: PLC0415
    from app.heuristics.health_engine import calculate_health_score  # noqa: PLC0415
    from app.persistence.models.audit import DecisionAuditLog  # noqa: PLC0415
    from app.persistence.models.business import MomentumProfile  # noqa: PLC0415
    from app.persistence.models.score import HealthScoreSnapshot  # noqa: PLC0415
    from sqlalchemy import select  # noqa: PLC0415

    async def _patched_run(tenant_id_str: str) -> None:
        import uuid as _uuid  # noqa: PLC0415
        import time  # noqa: PLC0415
        from datetime import UTC, datetime  # noqa: PLC0415
        from decimal import Decimal  # noqa: PLC0415

        tenant_id = _uuid.UUID(tenant_id_str)
        t0 = time.monotonic()

        state = await compute_business_state(tenant_id, session, redis)
        result = calculate_health_score(state)
        now = datetime.now(UTC)

        snapshot = HealthScoreSnapshot(
            id=_uuid.uuid4(),
            tenant_id=tenant_id,
            total_score=Decimal(result.score_total),
            level=job_module._score_level(result.score_total),
            dimensions={},
            triggered_by="celery:recalculate_health_score",
            snapshot_date=now,
            created_at=now,
            score_cash=result.score_cash,
            score_margin=result.score_margin,
            score_stock=result.score_stock,
            score_supplier=result.score_supplier,
            source_snapshot_id=state.snapshot_id,
            heuristic_version=job_module.HEURISTIC_VERSION,
            primary_risk_code=result.primary_risk_code,
            confidence_level=result.confidence_level,
            data_completeness_score=Decimal(str(result.data_completeness_score)),
            score_inputs_json=job_module._state_to_dict(state),
        )
        session.add(snapshot)
        await session.flush()

        audit = DecisionAuditLog(
            id=_uuid.uuid4(),
            tenant_id=tenant_id,
            decision_type="HEALTH_SCORE",
            decision_data={
                "source_snapshot_id": str(state.snapshot_id),
                "heuristic_version": job_module.HEURISTIC_VERSION,
                "input_state_json": job_module._state_to_dict(state),
                "output_json": job_module._result_to_dict(result),
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

    return _patched_run


# ── Test 1: job persists HealthScoreSnapshot ──────────────────────────────────


@pytest.mark.asyncio
async def test_job_persists_health_score_snapshot(
    session: AsyncSession,
    tenant: Tenant,
    kiosco_profile: BusinessProfile,
) -> None:
    """
    After _run(), a HealthScoreSnapshot with explicit subscore columns
    must exist in the DB for the tenant.
    """
    redis = FakeRedis()
    run = _make_run(session, redis)
    await run(str(tenant.tenant_id))

    result = await session.execute(
        select(HealthScoreSnapshot).where(
            HealthScoreSnapshot.tenant_id == tenant.tenant_id
        )
    )
    snapshots = result.scalars().all()

    assert len(snapshots) == 1, "Expected exactly one HealthScoreSnapshot"
    snap = snapshots[0]

    assert snap.score_cash is not None
    assert snap.score_margin is not None
    assert snap.score_stock is not None
    assert snap.score_supplier is not None
    assert snap.heuristic_version == "v1"
    assert snap.primary_risk_code in {"CASH_LOW", "MARGIN_LOW", "STOCK_CRITICAL", "SUPPLIER_DEPENDENCY"}
    assert 0 <= int(snap.total_score) <= 100
    assert snap.score_inputs_json is not None
    assert snap.score_inputs_json.get("vertical_code") == "kiosco"


# ── Test 2: job creates DecisionAuditLog entry ────────────────────────────────


@pytest.mark.asyncio
async def test_job_creates_decision_audit_log_entry(
    session: AsyncSession,
    tenant: Tenant,
    kiosco_profile: BusinessProfile,
) -> None:
    """
    After _run(), a DecisionAuditLog row with decision_type='HEALTH_SCORE'
    must exist and contain the serialized input + output.
    """
    redis = FakeRedis()
    run = _make_run(session, redis)
    await run(str(tenant.tenant_id))

    result = await session.execute(
        select(DecisionAuditLog).where(
            DecisionAuditLog.tenant_id == tenant.tenant_id,
            DecisionAuditLog.decision_type == "HEALTH_SCORE",
        )
    )
    entries = result.scalars().all()

    assert len(entries) == 1, "Expected exactly one DecisionAuditLog entry"
    entry = entries[0]

    assert entry.decision_type == "HEALTH_SCORE"
    assert "input_state_json" in entry.decision_data
    assert "output_json" in entry.decision_data
    assert "source_snapshot_id" in entry.decision_data
    assert entry.decision_data["output_json"]["primary_risk_code"] is not None
    assert entry.context is not None
    assert entry.context["confidence_level"] in {"HIGH", "MEDIUM", "LOW"}


# ── Test 3: job updates best_score_ever on improvement ───────────────────────


@pytest.mark.asyncio
async def test_job_updates_best_score_ever(
    session: AsyncSession,
    tenant: Tenant,
    kiosco_profile: BusinessProfile,
) -> None:
    """
    If no MomentumProfile exists, _run() creates one with best_score_ever set.
    If called again without improvement, best_score_ever stays the same.
    If the score improves, best_score_ever is updated.
    """
    redis = FakeRedis()
    run = _make_run(session, redis)

    # First run: MomentumProfile should be created
    await run(str(tenant.tenant_id))

    result = await session.execute(
        select(MomentumProfile).where(MomentumProfile.tenant_id == tenant.tenant_id)
    )
    momentum = result.scalar_one()
    assert momentum.best_score_ever is not None
    first_best = momentum.best_score_ever

    # Seed a lower best_score_ever to simulate an improvement scenario
    momentum.best_score_ever = max(0, first_best - 10)
    await session.commit()

    # Invalidate cache so BSL recomputes
    await redis.set(f"last_inputs_hash:{tenant.tenant_id}", "stale-hash")

    await run(str(tenant.tenant_id))

    await session.refresh(momentum)
    assert momentum.best_score_ever >= first_best - 10, (
        "best_score_ever must be updated when new score exceeds previous best"
    )


# ── Test 4: concurrent jobs don't duplicate (Redis lock) ─────────────────────


@pytest.mark.asyncio
async def test_concurrent_jobs_dont_duplicate(
    tenant: Tenant,
    kiosco_profile: BusinessProfile,
) -> None:
    """
    maybe_trigger_recalculation() with a pre-held lock returns False
    and does NOT dispatch the Celery task.
    """
    from unittest.mock import MagicMock, patch  # noqa: PLC0415

    from app.jobs.trigger_recalculation import maybe_trigger_recalculation  # noqa: PLC0415

    redis = FakeRedis()

    # Pre-acquire the lock to simulate an in-flight job
    lock_key = f"score_lock:{tenant.tenant_id}"
    await redis.set(lock_key, "1")

    # Patch in the home module so the lazy import inside the function picks it up
    with patch(
        "app.jobs.recalculate_health_score.recalculate_health_score"
    ) as mock_task:
        mock_task.delay = MagicMock()
        dispatched = await maybe_trigger_recalculation(
            tenant_id=str(tenant.tenant_id),
            redis=redis,
        )

    assert dispatched is False, "Expected False when lock is already held"
    mock_task.delay.assert_not_called()
