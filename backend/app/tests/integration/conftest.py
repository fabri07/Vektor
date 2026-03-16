"""
Fixtures compartidas para la suite de integración de Véktor.

Usa SQLite in-memory + FakeRedis — sin infraestructura externa.
Cada test obtiene un engine y sesión frescos (scope=function).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.main import create_app
from app.persistence.db.base import Base
from app.persistence.db.session import get_db_session
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.business import BusinessProfile, MomentumProfile
from app.persistence.models.score import HealthScoreSnapshot
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.utils.security import create_access_token, hash_password

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


# ── FakeRedis ─────────────────────────────────────────────────────────────────


class FakeRedis:
    """Minimal Redis stub — dict-backed, satisfies compute_business_state interface."""

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
            return None
        self._store[key] = value
        return True

    async def aclose(self) -> None:
        pass


# ── DB fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def event_loop():  # type: ignore[override]
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def engine() -> AsyncGenerator[AsyncEngine, None]:
    eng = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await eng.dispose()


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()


@pytest_asyncio.fixture
async def client(session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    app = create_app()

    async def _override_session() -> AsyncGenerator[AsyncSession, None]:
        yield session

    app.dependency_overrides[get_db_session] = _override_session

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


# ── Entity factory helpers ────────────────────────────────────────────────────


async def make_tenant(session: AsyncSession, **overrides: Any) -> Tenant:
    """Create and persist a Tenant. Pass keyword overrides to customise any field."""
    defaults: dict[str, Any] = {
        "tenant_id": uuid.uuid4(),
        "legal_name": "Test PYME",
        "display_name": "Test PYME",
        "currency": "ARS",
        "pricing_reference_mode": "MEP",
        "status": "ACTIVE",
    }
    defaults.update(overrides)
    tenant = Tenant(**defaults)
    session.add(tenant)
    await session.commit()
    return tenant


async def make_user(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    **overrides: Any,
) -> User:
    """Create and persist an OWNER user for the given tenant."""
    defaults: dict[str, Any] = {
        "user_id": uuid.uuid4(),
        "tenant_id": tenant_id,
        "email": f"owner+{uuid.uuid4().hex[:8]}@test.com",
        "full_name": "Test Owner",
        "password_hash": hash_password("Secure123"),
        "role_code": "OWNER",
        "is_active": True,
    }
    defaults.update(overrides)
    user = User(**defaults)
    session.add(user)
    await session.commit()
    return user


async def make_profile(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    vertical: str,
    *,
    monthly_sales: Decimal,
    monthly_inventory: Decimal,
    monthly_fixed: Decimal,
    cash_on_hand: Decimal,
    supplier_count: int = 3,
    product_count: int = 0,
) -> BusinessProfile:
    """
    Create and persist a BusinessProfile with onboarding completed.
    All monetary values are in ARS / month (except weekly_sales which is converted
    automatically by the onboarding service — here we write monthly directly).
    """
    now = datetime.now(UTC)
    profile = BusinessProfile(
        tenant_id=tenant_id,
        vertical_code=vertical,
        data_mode="M0",
        data_confidence="MEDIUM",
        monthly_sales_estimate_ars=monthly_sales,
        monthly_inventory_spend_estimate_ars=monthly_inventory,
        monthly_fixed_expenses_estimate_ars=monthly_fixed,
        cash_on_hand_estimate_ars=cash_on_hand,
        supplier_count_estimate=supplier_count,
        product_count_estimate=product_count,
        onboarding_completed=True,
        # updated_at within last 7 days → onboarding_recent=True → cash is used
        updated_at=now,
        created_at=now,
    )
    session.add(profile)
    await session.commit()
    return profile


def make_auth_headers(user_id: uuid.UUID, tenant_id: uuid.UUID) -> dict[str, str]:
    """Return Authorization headers with a valid JWT for the given user/tenant."""
    token = create_access_token(
        {
            "sub": str(user_id),
            "tenant_id": str(tenant_id),
            "role_code": "OWNER",
        }
    )
    return {"Authorization": f"Bearer {token}"}


# ── Pipeline helper: BSL → Health Engine → Persist ───────────────────────────


async def run_pipeline(
    session: AsyncSession,
    redis: FakeRedis,
    tenant_id: uuid.UUID,
) -> HealthScoreSnapshot:
    """
    Replicates recalculate_health_score Celery task without real Celery or Redis.

    Pipeline:
        1. compute_business_state  (Business State Layer)
        2. calculate_health_score  (Heuristic Engine)
        3. Persist HealthScoreSnapshot + DecisionAuditLog
        4. Upsert MomentumProfile (best_score_ever)
    """
    import app.jobs.recalculate_health_score as job_module  # noqa: PLC0415
    from sqlalchemy import select  # noqa: PLC0415

    from app.heuristics.health_engine import calculate_health_score  # noqa: PLC0415
    from app.state.business_state_service import compute_business_state  # noqa: PLC0415

    now = datetime.now(UTC)

    # 1. Business State Layer
    state = await compute_business_state(tenant_id, session, redis)  # type: ignore[arg-type]

    # 2. Heuristic Engine
    result = calculate_health_score(state)

    # 3. Persist snapshot
    snap = HealthScoreSnapshot(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        total_score=Decimal(result.score_total),
        level=job_module._score_level(result.score_total),
        dimensions={},
        triggered_by="test:run_pipeline",
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
    session.add(snap)
    await session.flush()

    # 4. Audit log
    audit = DecisionAuditLog(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        decision_type="HEALTH_SCORE",
        decision_data={
            "source_snapshot_id": str(state.snapshot_id),
            "heuristic_version": job_module.HEURISTIC_VERSION,
            "input_state_json": job_module._state_to_dict(state),
            "output_json": job_module._result_to_dict(result),
        },
        triggered_by="test:run_pipeline",
        actor_user_id=None,
        context={"confidence_level": result.confidence_level},
        created_at=now,
    )
    session.add(audit)

    # 5. Upsert MomentumProfile
    mp_res = await session.execute(
        select(MomentumProfile).where(MomentumProfile.tenant_id == tenant_id)
    )
    momentum = mp_res.scalar_one_or_none()
    if momentum is None:
        session.add(
            MomentumProfile(
                tenant_id=tenant_id,
                best_score_ever=result.score_total,
                best_score_date=now.date(),
                milestones_json=[],
                improving_streak_weeks=0,
                updated_at=now,
            )
        )
    elif momentum.best_score_ever is None or result.score_total > momentum.best_score_ever:
        momentum.best_score_ever = result.score_total
        momentum.best_score_date = now.date()
        momentum.updated_at = now

    await session.commit()
    return snap
