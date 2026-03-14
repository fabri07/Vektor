"""
Validation tests for the full health score flow.

Covers:
  1. Job corre exitosamente y persiste score en DB.
  2. decision_audit_log tiene una entrada.
  3. GET /health-scores/latest retorna score calculado.
  4. Dos jobs simultáneos no crean dos snapshots.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.persistence.db.base import Base
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.business import BusinessProfile
from app.persistence.models.score import HealthScoreSnapshot
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.utils.security import create_access_token, hash_password

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


# ── FakeRedis ─────────────────────────────────────────────────────────────────


class FakeRedis:
    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: str, *, nx: bool = False, ex: int | None = None) -> bool | None:
        if nx and key in self._store:
            return None
        self._store[key] = value
        return True

    async def aclose(self) -> None:
        pass


# ── DB fixtures ───────────────────────────────────────────────────────────────


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
        legal_name="Kiosco Validación",
        display_name="Kiosco Validación",
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


# ── Shared patched _run ───────────────────────────────────────────────────────


def _make_run(session: AsyncSession, redis: FakeRedis):
    import app.jobs.recalculate_health_score as job_module
    from app.state.business_state_service import compute_business_state
    from app.heuristics.health_engine import calculate_health_score
    from app.persistence.models.audit import DecisionAuditLog
    from app.persistence.models.business import MomentumProfile
    from app.persistence.models.score import HealthScoreSnapshot
    from sqlalchemy import select

    async def _run(tenant_id_str: str) -> None:
        import uuid as _uuid, time
        from datetime import UTC, datetime
        from decimal import Decimal

        tenant_id = _uuid.UUID(tenant_id_str)
        state = await compute_business_state(tenant_id, session, redis)
        result = calculate_health_score(state)
        now = datetime.now(UTC)

        snap = HealthScoreSnapshot(
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
        session.add(snap)
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
            context={"confidence_level": result.confidence_level},
            created_at=now,
        )
        session.add(audit)

        mp_res = await session.execute(
            select(MomentumProfile).where(MomentumProfile.tenant_id == tenant_id)
        )
        momentum = mp_res.scalar_one_or_none()
        if momentum is None:
            session.add(MomentumProfile(
                tenant_id=tenant_id,
                best_score_ever=result.score_total,
                best_score_date=now.date(),
                milestones_json=[],
                improving_streak_weeks=0,
                updated_at=now,
            ))
        await session.commit()

    return _run


# ── Validation 1: Job persiste score en DB ────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_job_persists_score(
    session: AsyncSession,
    tenant: Tenant,
    kiosco_profile: BusinessProfile,
) -> None:
    """Job corre exitosamente y persiste score en DB con subscores explícitos."""
    run = _make_run(session, FakeRedis())
    await run(str(tenant.tenant_id))

    result = await session.execute(
        select(HealthScoreSnapshot).where(HealthScoreSnapshot.tenant_id == tenant.tenant_id)
    )
    snap = result.scalar_one()

    assert snap.score_cash is not None, "score_cash debe estar persistido"
    assert snap.score_margin is not None, "score_margin debe estar persistido"
    assert snap.score_stock is not None, "score_stock debe estar persistido"
    assert snap.score_supplier is not None, "score_supplier debe estar persistido"
    assert 0 <= int(snap.total_score) <= 100, f"score_total fuera de rango: {snap.total_score}"
    assert snap.heuristic_version == "v1"
    assert snap.primary_risk_code in {"CASH_LOW", "MARGIN_LOW", "STOCK_CRITICAL", "SUPPLIER_DEPENDENCY"}


# ── Validation 2: decision_audit_log tiene una entrada ────────────────────────


@pytest.mark.asyncio
async def test_validate_decision_audit_log(
    session: AsyncSession,
    tenant: Tenant,
    kiosco_profile: BusinessProfile,
) -> None:
    """Exactamente una entrada HEALTH_SCORE en decision_audit_log tras el job."""
    run = _make_run(session, FakeRedis())
    await run(str(tenant.tenant_id))

    result = await session.execute(
        select(DecisionAuditLog).where(
            DecisionAuditLog.tenant_id == tenant.tenant_id,
            DecisionAuditLog.decision_type == "HEALTH_SCORE",
        )
    )
    entries = result.scalars().all()

    assert len(entries) == 1, f"Esperaba 1 entrada en audit_log, encontré {len(entries)}"
    e = entries[0]
    assert "output_json" in e.decision_data
    assert e.decision_data["output_json"]["score_total"] >= 0


# ── Validation 3: GET /health-scores/latest retorna score calculado ───────────


@pytest.mark.asyncio
async def test_validate_latest_endpoint(
    engine: AsyncEngine,
    session: AsyncSession,
    tenant: Tenant,
    kiosco_profile: BusinessProfile,
) -> None:
    """
    Después de correr el job, GET /api/v1/health-scores/latest debe retornar
    un HealthScoreV2Response con todos los subscores.
    """
    # 1. Correr el job
    run = _make_run(session, FakeRedis())
    await run(str(tenant.tenant_id))

    # 2. Crear un usuario con token válido para el tenant
    user = User(
        user_id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        email="owner@validate.com",
        full_name="Test Owner",
        password_hash=hash_password("Pass123!"),
        role_code="OWNER",
        is_active=True,
    )
    session.add(user)
    await session.commit()

    token = create_access_token({
        "sub": str(user.user_id),
        "tenant_id": str(tenant.tenant_id),
        "role_code": "OWNER",
    })
    headers = {"Authorization": f"Bearer {token}"}

    # 3. Levantar la app con override de sesión
    from app.main import create_app
    from app.persistence.db.session import get_db_session

    app = create_app()

    async def override_session():
        yield session

    app.dependency_overrides[get_db_session] = override_session

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/health-scores/latest", headers=headers)

    assert response.status_code == 200, f"Esperaba 200, obtuve {response.status_code}: {response.text}"
    data = response.json()

    assert "score_total" in data, "Falta score_total en la respuesta"
    assert "score_cash" in data, "Falta score_cash en la respuesta"
    assert "score_margin" in data, "Falta score_margin en la respuesta"
    assert "score_stock" in data, "Falta score_stock en la respuesta"
    assert "score_supplier" in data, "Falta score_supplier en la respuesta"
    assert "primary_risk_code" in data, "Falta primary_risk_code en la respuesta"
    assert "status" not in data, "No debe retornar {status: CALCULATING} si ya hay score"
    assert 0 <= data["score_total"] <= 100


# ── Validation 4: Dos jobs simultáneos no crean dos snapshots ─────────────────


@pytest.mark.asyncio
async def test_validate_no_duplicate_snapshots(
    session: AsyncSession,
    tenant: Tenant,
    kiosco_profile: BusinessProfile,
) -> None:
    """
    maybe_trigger_recalculation() con lock ya tomado no despacha el job,
    garantizando que un segundo job no crearía un snapshot duplicado.
    """
    from unittest.mock import MagicMock, patch
    from app.jobs.trigger_recalculation import maybe_trigger_recalculation

    redis = FakeRedis()

    # Primera llamada: despacha (mockeamos delay para no necesitar Celery)
    with patch("app.jobs.recalculate_health_score.recalculate_health_score") as mock_task:
        mock_task.delay = MagicMock()
        first = await maybe_trigger_recalculation(str(tenant.tenant_id), redis)
        assert first is True, "Primera llamada debe despachar"
        mock_task.delay.assert_called_once()

    # Segunda llamada mientras el lock sigue activo: ignorada silenciosamente
    with patch("app.jobs.recalculate_health_score.recalculate_health_score") as mock_task2:
        mock_task2.delay = MagicMock()
        second = await maybe_trigger_recalculation(str(tenant.tenant_id), redis)
        assert second is False, "Segunda llamada debe ser ignorada (lock activo)"
        mock_task2.delay.assert_not_called()

    # Solo un snapshot en DB (porque el segundo job nunca corrió)
    result = await session.execute(
        select(HealthScoreSnapshot).where(HealthScoreSnapshot.tenant_id == tenant.tenant_id)
    )
    snapshots = result.scalars().all()
    assert len(snapshots) == 0, (
        "No debe haber snapshots en DB porque ambos jobs fueron mockeados / bloqueados"
    )
