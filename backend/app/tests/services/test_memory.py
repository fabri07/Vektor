"""Tests del sistema de memoria global: BusinessMemoryService + ConversationService."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.application.services.business_memory_service import BusinessMemoryService
from app.application.services.conversation_service import ConversationService
from app.persistence.models.memory import BusinessMemory

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_redis() -> AsyncMock:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()
    redis.delete = AsyncMock()
    return redis


@pytest.fixture
def mock_db() -> AsyncMock:
    db = AsyncMock()
    scalar_result = AsyncMock()
    # scalar_one_or_none es síncrono en SQLAlchemy — usar MagicMock, no AsyncMock.
    # Si se usa AsyncMock, al llamarlo sin await retorna un coroutine (truthy),
    # rompiendo la rama `if mem is None`.
    scalar_result.scalar_one_or_none = MagicMock(return_value=None)
    db.execute = AsyncMock(return_value=scalar_result)
    db.flush = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.get = AsyncMock(return_value=None)
    return db


# ── BusinessMemoryService ─────────────────────────────────────────────────────


async def test_memory_get_returns_empty_when_no_record(
    mock_redis: AsyncMock, mock_db: AsyncMock
) -> None:
    svc = BusinessMemoryService(db=mock_db, redis=mock_redis)
    result = await svc.get(uuid.uuid4())
    assert result["sales_today"] == 0
    assert result["llm_context_summary"] is None


async def test_memory_uses_redis_cache(mock_redis: AsyncMock, mock_db: AsyncMock) -> None:
    mock_redis.get = AsyncMock(
        return_value=json.dumps({"sales_today": 500.0, "llm_context_summary": "Buen día"})
    )
    svc = BusinessMemoryService(db=mock_db, redis=mock_redis)
    result = await svc.get(uuid.uuid4())
    assert result["sales_today"] == 500.0
    mock_db.execute.assert_not_called()


async def test_memory_update_after_sale_creates_record(
    mock_redis: AsyncMock, mock_db: AsyncMock
) -> None:
    svc = BusinessMemoryService(db=mock_db, redis=mock_redis)
    await svc.update_after_sale(uuid.uuid4(), Decimal("1500.00"))
    mock_db.add.assert_called_once()
    mock_db.flush.assert_called_once()
    mock_redis.delete.assert_called_once()


async def test_memory_update_after_expense_creates_record(
    mock_redis: AsyncMock, mock_db: AsyncMock
) -> None:
    svc = BusinessMemoryService(db=mock_db, redis=mock_redis)
    await svc.update_after_expense(uuid.uuid4(), Decimal("300.00"), "alquiler")
    mock_db.add.assert_called_once()
    mock_db.flush.assert_called_once()


async def test_memory_update_increments_existing_record(
    mock_redis: AsyncMock, mock_db: AsyncMock
) -> None:
    existing = BusinessMemory(
        tenant_id=uuid.uuid4(),
        sales_today=Decimal("1000"),
        sales_this_week=Decimal("1000"),
        sales_this_month=Decimal("1000"),
    )
    scalar_result = AsyncMock()
    scalar_result.scalar_one_or_none = MagicMock(return_value=existing)
    mock_db.execute = AsyncMock(return_value=scalar_result)

    svc = BusinessMemoryService(db=mock_db, redis=mock_redis)
    await svc.update_after_sale(existing.tenant_id, Decimal("500"))

    assert existing.sales_today == Decimal("1500")
    assert existing.sales_this_week == Decimal("1500")
    assert existing.sales_this_month == Decimal("1500")


# ── ConversationService ───────────────────────────────────────────────────────


async def test_conversation_summarized_after_max_turns(
    mock_redis: AsyncMock, mock_db: AsyncMock
) -> None:
    # Simular 10 turnos existentes
    existing_turns = [{"role": "user", "content": f"msg {i}"} for i in range(10)]
    ctx_data = {"turns": existing_turns, "summary": None}
    mock_redis.get = AsyncMock(return_value=json.dumps(ctx_data))

    svc = ConversationService(redis_client=mock_redis, db_session=mock_db)

    with patch.object(
        svc, "_summarize_turns", AsyncMock(return_value="Resumen del chat")
    ) as mock_sum:
        result = await svc.add_turn_with_summary(
            conversation_id=str(uuid.uuid4()),
            role="user",
            content="mensaje 11",
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
        )
        mock_sum.assert_called_once()

    assert len(result["turns"]) <= 5
    assert result["summary"] == "Resumen del chat"


async def test_conversation_no_summary_under_max_turns(
    mock_redis: AsyncMock, mock_db: AsyncMock
) -> None:
    ctx_data = {"turns": [{"role": "user", "content": "hola"}], "summary": None}
    mock_redis.get = AsyncMock(return_value=json.dumps(ctx_data))

    svc = ConversationService(redis_client=mock_redis, db_session=mock_db)

    with patch.object(svc, "_summarize_turns", AsyncMock(return_value="")) as mock_sum:
        result = await svc.add_turn_with_summary(
            conversation_id=str(uuid.uuid4()),
            role="assistant",
            content="respuesta",
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
        )
        mock_sum.assert_not_called()

    assert len(result["turns"]) == 2
