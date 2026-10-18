"""D·5 contra Postgres: dos submits del onboarding a la vez dejan un solo capital inicial.

Lo que lo evita es el ``FOR UPDATE`` sobre ``business_profiles``: el segundo
submit espera al primero, ve ``onboarding_completed`` y sale con
``AlreadyOnboardedError``. SQLite no tiene ese lock; acá cada submit va en su
propia conexión y commitea de verdad.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from datetime import timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.application.services.onboarding_service import (
    AlreadyOnboardedError,
    OnboardingService,
)
from app.domain.business_time import today_ar
from app.domain.verticals import Vertical
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.business import BusinessProfile, BusinessSnapshot
from app.persistence.models.owner_capital import OwnerCapitalMovement
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.schemas.onboarding import OnboardingSubmitRequest
from app.tests.integration import _reread_scenario as esc

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere TEST_PG_DSN (Postgres real)"),
]

_DDL_ADVISORY_KEY = 0x5645_4B54_4F52_4435  # "VEKTOR" + D5


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    assert TEST_PG_DSN is not None
    engine = await esc.crear_engine(TEST_PG_DSN, _DDL_ADVISORY_KEY)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def fabrica(pg_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(pg_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def tenant_y_usuario(
    fabrica: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[tuple[uuid.UUID, uuid.UUID], None]:
    # Sin broker: el recálculo del score que encola el submit no hace al test.
    monkeypatch.setattr(
        "app.application.services.onboarding_service._enqueue_score_recalculation",
        lambda **_: None,
    )
    tid, uid = uuid.uuid4(), uuid.uuid4()
    async with fabrica() as s:
        s.add(Tenant(tenant_id=tid, legal_name="D5", display_name="D5"))
        await s.flush()
        s.add(User(user_id=uid, tenant_id=tid, email=f"d5-{uid}@example.com",
                   full_name="D5", password_hash="x", role_code="OWNER"))
        s.add(BusinessProfile(profile_id=uuid.uuid4(), tenant_id=tid,
                              vertical_code=Vertical.KIOSCO_ALMACEN.value, data_mode="M0",
                              data_confidence="LOW", onboarding_completed=False))
        await s.commit()
    try:
        yield tid, uid
    finally:
        async with fabrica() as s:
            for modelo in (DecisionAuditLog, OwnerCapitalMovement, BusinessSnapshot,
                           BusinessProfile, User):
                await s.execute(delete(modelo).where(modelo.tenant_id == tid))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tid))
            await s.commit()


async def test_dos_submits_a_la_vez_dejan_un_solo_capital_inicial(
    fabrica: async_sessionmaker[AsyncSession], tenant_y_usuario: tuple[uuid.UUID, uuid.UUID]
) -> None:
    tid, uid = tenant_y_usuario
    body = OnboardingSubmitRequest(
        weekly_sales_estimate_ars=Decimal("50000"),
        product_count_estimate=10,
        supplier_count_estimate=3,
        main_concern="MARGIN",
        initial_capital_ars=Decimal("500000.00"),
        initial_capital_date=today_ar() - timedelta(days=3),
    )

    async def submit() -> str:
        async with fabrica() as s:
            try:
                await OnboardingService(s).submit(tid, body, uid)
                await s.commit()
                return "ok"
            except AlreadyOnboardedError:
                await s.rollback()
                return "409"

    assert sorted(await asyncio.gather(submit(), submit())) == ["409", "ok"]
    async with fabrica() as s:
        capitales = await s.scalar(
            select(func.count()).select_from(OwnerCapitalMovement).where(
                OwnerCapitalMovement.tenant_id == tid,
                OwnerCapitalMovement.source_type == "onboarding_declarado",
            )
        )
        snapshots = await s.scalar(
            select(func.count()).select_from(BusinessSnapshot).where(
                BusinessSnapshot.tenant_id == tid
            )
        )
    assert (capitales, snapshots) == (1, 1)
