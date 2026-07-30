"""El rebuild semanal aísla el fallo de un tenant del resto de la cola.

A diferencia de los otros tests de jobs —que reimplementan el cuerpo de ``_run()``
contra la sesión de test— este llama a ``rebuild_all_tenants`` REAL. Es la razón
por la que el loop se extrajo a nivel de módulo: un test que reimplementa el
cuerpo no puede detectar que al loop productivo le falta el ``try/except``.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.jobs.score_worker import rebuild_all_tenants
from app.persistence.db.base import Base

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest_asyncio.fixture
async def engine() -> AsyncEngine:  # type: ignore[misc]
    eng = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest.mark.asyncio
async def test_un_tenant_roto_no_frena_a_los_demas(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El tenant del medio explota; los otros dos igual se recalculan."""
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    ok, roto, otro = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    recalculados: list[uuid.UUID] = []

    async def _fake_recalculate(self: object, tenant_id: uuid.UUID, triggered_by: str) -> None:
        if tenant_id == roto:
            raise RuntimeError("tenant con configuración rota")
        recalculados.append(tenant_id)

    from app.application.services.health_score_service import HealthScoreService

    monkeypatch.setattr(HealthScoreService, "recalculate_for_tenant", _fake_recalculate)

    fallidos = await rebuild_all_tenants(factory, [ok, roto, otro])

    assert fallidos == 1
    # `otro` viene DESPUÉS del que falla: es exactamente lo que se perdía antes.
    assert recalculados == [ok, otro]


@pytest.mark.asyncio
async def test_sin_fallos_devuelve_cero(
    engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    tenants = [uuid.uuid4(), uuid.uuid4()]

    async def _fake_recalculate(self: object, tenant_id: uuid.UUID, triggered_by: str) -> None:
        return None

    from app.application.services.health_score_service import HealthScoreService

    monkeypatch.setattr(HealthScoreService, "recalculate_for_tenant", _fake_recalculate)

    assert await rebuild_all_tenants(factory, tenants) == 0
