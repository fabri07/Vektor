from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.chat_memory_service import ChatMemoryService
from app.persistence.models.chat_session_log import ChatSessionLog
from app.persistence.models.tenant import Tenant


@pytest.fixture
def redis_mock() -> AsyncMock:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()
    redis.delete = AsyncMock()
    return redis


@pytest.mark.asyncio
async def test_record_persiste_en_db(
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    svc = ChatMemoryService()
    await svc.record(
        db=db_session,
        tenant_id=sample_tenant.tenant_id,
        session_id="conv-1",
        entry_type="DATA_LOADED",
        description="Carga de ventas",
        entity_type="sale",
        entity_count=2,
    )
    result = await db_session.execute(select(ChatSessionLog))
    log = result.scalar_one()
    assert log.description == "Carga de ventas"
    assert log.entity_count == 2


@pytest.mark.asyncio
async def test_get_context_sin_historial(
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    ctx = await ChatMemoryService().get_session_context(db_session, sample_tenant.tenant_id)
    assert ctx["historial_disponible"] is False
    assert ctx["ultima_carga"] is None


@pytest.mark.asyncio
async def test_get_context_con_ultima_carga(
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    svc = ChatMemoryService()
    await svc.record(
        db_session,
        sample_tenant.tenant_id,
        "conv-1",
        "DATA_LOADED",
        "Importado ventas.csv",
        entity_type="ventas",
        entity_count=3,
    )
    ctx = await svc.get_session_context(db_session, sample_tenant.tenant_id)
    assert ctx["historial_disponible"] is True
    assert ctx["ultima_carga"]["descripcion"] == "Importado ventas.csv"
    assert ctx["total_cargas_registradas"] == 1


@pytest.mark.asyncio
async def test_tenant_isolation(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    second_tenant: Tenant,
) -> None:
    svc = ChatMemoryService()
    await svc.record(db_session, sample_tenant.tenant_id, "a", "DATA_LOADED", "Tenant A")
    await svc.record(db_session, second_tenant.tenant_id, "b", "DATA_LOADED", "Tenant B")
    ctx = await svc.get_session_context(db_session, sample_tenant.tenant_id)
    descriptions = [e["descripcion"] for e in ctx["eventos_recientes"]]
    assert descriptions == ["Tenant A"]


@pytest.mark.asyncio
async def test_cache_redis_invalida_al_hacer_record(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    redis_mock: AsyncMock,
) -> None:
    await ChatMemoryService().record(
        db_session,
        sample_tenant.tenant_id,
        "conv-1",
        "DATA_LOADED",
        "Carga",
        redis=redis_mock,
    )
    redis_mock.delete.assert_awaited_once_with(f"chat_ctx:{sample_tenant.tenant_id}")


@pytest.mark.asyncio
async def test_get_session_context_cached_usa_redis(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    redis_mock: AsyncMock,
) -> None:
    redis_mock.get = AsyncMock(
        return_value=(
            '{"historial_disponible": true, "ultima_carga": null, '
            '"total_cargas_registradas": 0, "eventos_recientes": []}'
        )
    )
    ctx = await ChatMemoryService().get_session_context_cached(
        db_session,
        redis_mock,
        sample_tenant.tenant_id,
    )
    assert ctx["historial_disponible"] is True
    redis_mock.setex.assert_not_called()
