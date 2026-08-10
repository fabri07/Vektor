"""Unit tests — AgentMemoryService sin DB real ni Redis real."""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock

from app.application.services.agent_memory_service import AgentMemoryService

TENANT_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _make_service(db_rows=None, redis_hit=None):
    """Factory que retorna AgentMemoryService con DB y Redis mockeados."""
    db = AsyncMock()
    redis = AsyncMock()

    # Redis: si redis_hit es un dict, devuelve ese JSON; si None, devuelve None
    redis.get = AsyncMock(
        return_value=json.dumps(redis_hit).encode() if redis_hit is not None else None
    )
    redis.setex = AsyncMock()
    redis.delete = AsyncMock()

    # DB: scalar_one_or_none devuelve None por defecto (sin filas existentes)
    # scalars().all() devuelve la lista de rows proporcionada
    result_mock = MagicMock()
    result_mock.scalar_one_or_none = MagicMock(return_value=None)
    result_mock.scalars.return_value.all.return_value = db_rows or []
    db.execute = AsyncMock(return_value=result_mock)
    db.flush = AsyncMock()
    db.rollback = AsyncMock()
    db.add = MagicMock()

    return AgentMemoryService(db=db, redis=redis)


# ── Tests: get() ─────────────────────────────────────────────────────────────


async def test_get_returns_redis_cache_when_available():
    """Si Redis tiene datos, no consulta DB."""
    cached = {
        "preferred_payment_method": {
            "method": "efectivo",
            "pct": 80.0,
            "counts": {"efectivo": 8, "transferencia": 2},
        }
    }
    svc = _make_service(redis_hit=cached)
    result = await svc.get(TENANT_ID)
    assert result["preferred_payment_method"]["method"] == "efectivo"
    svc._db.execute.assert_not_called()


async def test_get_falls_back_to_db_on_redis_miss():
    """Sin cache Redis, carga desde DB y devuelve dict vacío cuando no hay filas."""
    svc = _make_service(redis_hit=None, db_rows=[])
    result = await svc.get(TENANT_ID)
    assert result == {}
    svc._db.execute.assert_called()


async def test_get_builds_dict_from_db_rows():
    """_load_from_db construye el dict correctamente a partir de filas ORM."""
    row1 = MagicMock()
    row1.key = "avg_sale_amount"
    row1.value = {"value": 5000.0, "n": 3}
    row2 = MagicMock()
    row2.key = "top_action_types"
    row2.value = {"top": ["REGISTER_SALE"], "counts": {"REGISTER_SALE": 3}}

    svc = _make_service(redis_hit=None, db_rows=[row1, row2])
    result = await svc.get(TENANT_ID)
    assert result["avg_sale_amount"]["value"] == 5000.0
    assert "REGISTER_SALE" in result["top_action_types"]["top"]


# ── Tests: record_action() ────────────────────────────────────────────────────


async def test_record_sale_updates_payment_method():
    """record_action con REGISTER_SALE actualiza preferred_payment_method."""
    svc = _make_service()
    await svc.record_action(
        TENANT_ID,
        "REGISTER_SALE",
        {"amount": 5000, "payment_method": "efectivo"},
    )
    # Verifica que flush fue llamado (persistió algo)
    svc._db.flush.assert_called()
    # Verifica que Redis fue invalidado
    svc._redis.delete.assert_called()


async def test_record_sale_updates_avg_amount():
    """record_action con REGISTER_SALE actualiza avg_sale_amount."""
    svc = _make_service()
    await svc.record_action(
        TENANT_ID,
        "REGISTER_SALE",
        {"amount": 10000, "payment_method": "transferencia"},
    )
    svc._db.flush.assert_called()


async def test_record_expense_updates_categories():
    """record_action con REGISTER_EXPENSE actualiza common_expense_categories."""
    svc = _make_service()
    await svc.record_action(
        TENANT_ID,
        "REGISTER_EXPENSE",
        {"amount": 3000, "category": "alquiler"},
    )
    svc._db.flush.assert_called()


async def test_record_purchase_updates_expense_categories():
    """REGISTER_PURCHASE también actualiza common_expense_categories."""
    svc = _make_service()
    await svc.record_action(
        TENANT_ID,
        "REGISTER_PURCHASE",
        {"amount": 8000, "category": "mercaderia"},
    )
    svc._db.flush.assert_called()
    svc._redis.delete.assert_called()


async def test_record_action_always_updates_top_action_types():
    """Cualquier action_type actualiza top_action_types."""
    svc = _make_service()
    await svc.record_action(
        TENANT_ID,
        "GENERATE_HEALTH_REPORT",
        {},
    )
    # flush llamado al menos una vez para top_action_types
    svc._db.flush.assert_called()
    svc._redis.delete.assert_called()


# ── Tests: get_context_fragment() ────────────────────────────────────────────


async def test_get_context_fragment_returns_string():
    """get_context_fragment retorna string no vacío cuando hay patrones."""
    cached = {
        "preferred_payment_method": {"method": "efectivo", "pct": 75.0, "counts": {}},
        "avg_sale_amount": {"value": 8500.0, "n": 10},
        "common_expense_categories": {"top": ["alquiler", "suministros"], "counts": {}},
        "top_action_types": {"top": ["REGISTER_SALE", "REGISTER_EXPENSE"], "counts": {}},
    }
    svc = _make_service(redis_hit=cached)
    fragment = await svc.get_context_fragment(TENANT_ID)
    assert "efectivo" in fragment
    assert "8500" in fragment
    assert "alquiler" in fragment


async def test_get_context_fragment_empty_when_no_patterns():
    """get_context_fragment retorna string vacío si no hay patrones."""
    svc = _make_service(redis_hit={}, db_rows=[])
    fragment = await svc.get_context_fragment(TENANT_ID)
    assert fragment == ""


async def test_get_context_fragment_partial_patterns():
    """get_context_fragment funciona aunque falten algunos patrones."""
    cached = {
        "avg_sale_amount": {"value": 3200.0, "n": 5},
    }
    svc = _make_service(redis_hit=cached)
    fragment = await svc.get_context_fragment(TENANT_ID)
    assert "3200" in fragment
    # No debe reventar aunque no haya payment_method ni categories


async def test_get_context_fragment_includes_header():
    """El fragmento siempre empieza con la línea de cabecera cuando hay datos."""
    cached = {
        "preferred_payment_method": {"method": "tarjeta", "pct": 60.0, "counts": {}},
    }
    svc = _make_service(redis_hit=cached)
    fragment = await svc.get_context_fragment(TENANT_ID)
    assert fragment.startswith("Patrones aprendidos del negocio:")


# ── Tests: fail-silencioso ────────────────────────────────────────────────────


async def test_record_action_is_fail_silent():
    """Si la DB falla, record_action no lanza excepción."""
    svc = _make_service()
    svc._db.execute = AsyncMock(side_effect=Exception("DB down"))
    # No debe lanzar
    await svc.record_action(TENANT_ID, "REGISTER_SALE", {"amount": 1000})


async def test_get_returns_empty_dict_on_redis_and_db_failure():
    """Si tanto Redis como DB fallan, get() retorna dict vacío sin lanzar."""
    svc = _make_service()
    svc._redis.get = AsyncMock(side_effect=Exception("Redis down"))
    svc._db.execute = AsyncMock(side_effect=Exception("DB down"))
    result = await svc.get(TENANT_ID)
    assert result == {}


async def test_get_context_fragment_returns_empty_on_exception():
    """Si get() falla internamente, get_context_fragment devuelve string vacío."""
    svc = _make_service()
    svc._redis.get = AsyncMock(side_effect=Exception("Redis down"))
    svc._db.execute = AsyncMock(side_effect=Exception("DB down"))
    fragment = await svc.get_context_fragment(TENANT_ID)
    assert fragment == ""
