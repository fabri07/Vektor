"""Tests de observabilidad de contexto degradado (Task 3 — F0.5).

Verifica:
- Cuando BusinessMemoryService lanza excepción, result["context_health"]["business_memory"]
  queda "degraded" y el resto de las claves quedan "ok".  El request NO falla.
- Cuando business_memory falla, AgentChat.generate_response recibe degraded_notice != None.
- Caso feliz: todas las capas ok → context_health todo "ok" y degraded_notice=None.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.application.agents.shared.schemas import (
    AgentRequest,
    AgentResponse,
    Confidence,
    LLMCall,
    RiskLevel,
)
from app.application.services.chat_orchestrator import ChatOrchestrator

ORCHESTRATOR = "app.application.services.chat_orchestrator"


# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_request() -> AgentRequest:
    return AgentRequest(
        user_id=str(uuid.uuid4()),
        business_id=str(uuid.uuid4()),
        message="¿cómo van las ventas?",
    )


def _make_ceo_plan_response(request_id: str) -> AgentResponse:
    """CEO response con plan single-task válido."""
    plan_dict = {
        "plan_id": str(uuid.uuid4()),
        "intent": "analizar_ventas",
        "tasks": [
            {
                "task_id": str(uuid.uuid4()),
                "agent": "agent_income",
                "action_type": "ANALYZE_SALES_DATA",
                "entities": {},
                "depends_on": [],
                "approval_group": None,
            }
        ],
        "requires_synthesis": False,
        "fallback_message": None,
    }
    return AgentResponse(
        request_id=request_id,
        agent_name="agent_ceo",
        status="success",
        risk_level=RiskLevel.LOW,
        requires_approval=False,
        confidence=Confidence.HIGH,
        result={
            "intent": "analizar_ventas",
            "action_type": "ANALYZE_SALES_DATA",
            "target_agent": "agent_income",
            "plan": plan_dict,
        },
    )


def _make_sub_agent_response(request_id: str) -> AgentResponse:
    return AgentResponse(
        request_id=request_id,
        agent_name="agent_income",
        status="success",
        risk_level=RiskLevel.LOW,
        requires_approval=False,
        confidence=Confidence.HIGH,
        result={"summary": "Ventas del día: $50.000", "action_type": "ANALYZE_SALES_DATA"},
    )


_MOCK_LLM_CALL = LLMCall(
    source="agent_chat",
    model="claude-sonnet-4-6",
    input_tokens=100,
    output_tokens=50,
)


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_db() -> AsyncMock:
    db = AsyncMock()
    tenant = MagicMock()
    tenant.display_name = "Kiosco Test"
    db.get = AsyncMock(return_value=tenant)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = MagicMock(vertical_code="kiosco_almacen")
    db.execute = AsyncMock(return_value=result_mock)
    return db


@pytest.fixture
def mock_redis() -> AsyncMock:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()
    return redis


# ── Tests ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_business_memory_degraded_in_result(
    mock_db: AsyncMock, mock_redis: AsyncMock
) -> None:
    """Cuando BusinessMemoryService lanza excepción, context_health refleja 'degraded'
    para esa capa y 'ok' para las demás.  El request NO debe fallar.
    """
    request = _make_request()

    with (
        patch(f"{ORCHESTRATOR}.AgentCEO") as mock_ceo_cls,
        patch(f"{ORCHESTRATOR}.AgentChat") as mock_chat_cls,
        patch(f"{ORCHESTRATOR}.TeamPlanExecutor") as mock_executor,
        patch(f"{ORCHESTRATOR}.get_anthropic_async_client"),
        patch(f"{ORCHESTRATOR}.BusinessMemoryService") as mock_bm_cls,
        patch(f"{ORCHESTRATOR}.AgentMemoryService") as mock_am_cls,
        patch(f"{ORCHESTRATOR}.ConversationService") as mock_conv_cls,
    ):
        mock_ceo_cls.return_value.process = AsyncMock(
            side_effect=lambda req: _make_ceo_plan_response(req.request_id)
        )
        mock_executor.return_value.execute = AsyncMock(
            return_value=[_make_sub_agent_response(request.request_id)]
        )
        mock_chat_cls.return_value.generate_response = AsyncMock(
            return_value=("Las ventas del día fueron $50.000.", _MOCK_LLM_CALL)
        )

        # BusinessMemory falla
        mock_bm_cls.return_value.get = AsyncMock(side_effect=RuntimeError("DB timeout"))

        # AgentMemory OK
        mock_am_cls.return_value.get_context_fragment = AsyncMock(return_value="")

        conv_svc = AsyncMock()
        conv_svc.get_context = AsyncMock(return_value={"turns": [], "summary": None})
        mock_conv_cls.return_value = conv_svc

        orchestrator = ChatOrchestrator()
        response = await orchestrator.handle(
            request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4()
        )

    # El request NO falla
    assert response.status in ("success", "requires_approval", "requires_clarification")

    # context_health presente y con business_memory degraded
    ctx_health = response.result.get("context_health")
    assert ctx_health is not None, "Debe haber 'context_health' en result"
    assert ctx_health["business_memory"] == "degraded"
    assert ctx_health["agent_memory"] == "ok"
    assert ctx_health["file_context"] == "ok"


@pytest.mark.asyncio
async def test_business_memory_degraded_sends_notice_to_agent_chat(
    mock_db: AsyncMock, mock_redis: AsyncMock
) -> None:
    """Cuando business_memory falla, AgentChat.generate_response recibe un degraded_notice."""
    request = _make_request()

    with (
        patch(f"{ORCHESTRATOR}.AgentCEO") as mock_ceo_cls,
        patch(f"{ORCHESTRATOR}.AgentChat") as mock_chat_cls,
        patch(f"{ORCHESTRATOR}.TeamPlanExecutor") as mock_executor,
        patch(f"{ORCHESTRATOR}.get_anthropic_async_client"),
        patch(f"{ORCHESTRATOR}.BusinessMemoryService") as mock_bm_cls,
        patch(f"{ORCHESTRATOR}.AgentMemoryService") as mock_am_cls,
        patch(f"{ORCHESTRATOR}.ConversationService") as mock_conv_cls,
    ):
        mock_ceo_cls.return_value.process = AsyncMock(
            side_effect=lambda req: _make_ceo_plan_response(req.request_id)
        )
        mock_executor.return_value.execute = AsyncMock(
            return_value=[_make_sub_agent_response(request.request_id)]
        )
        generate_response_mock = AsyncMock(
            return_value=("Respuesta de prueba.", _MOCK_LLM_CALL)
        )
        mock_chat_cls.return_value.generate_response = generate_response_mock

        # BusinessMemory falla
        mock_bm_cls.return_value.get = AsyncMock(side_effect=RuntimeError("timeout"))

        # AgentMemory OK
        mock_am_cls.return_value.get_context_fragment = AsyncMock(return_value="")

        conv_svc = AsyncMock()
        conv_svc.get_context = AsyncMock(return_value={"turns": []})
        mock_conv_cls.return_value = conv_svc

        orchestrator = ChatOrchestrator()
        await orchestrator.handle(request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4())

    generate_response_mock.assert_awaited_once()
    call_kwargs = generate_response_mock.call_args.kwargs
    notice = call_kwargs.get("degraded_notice")
    assert notice is not None, "degraded_notice debe pasarse cuando business_memory falla"
    assert isinstance(notice, str) and len(notice) > 10, (
        f"La nota debe ser un string descriptivo, got: {notice!r}"
    )


@pytest.mark.asyncio
async def test_all_context_ok_no_degraded_notice(
    mock_db: AsyncMock, mock_redis: AsyncMock
) -> None:
    """Caso feliz: todas las capas funcionan → context_health todo 'ok', degraded_notice=None."""
    request = _make_request()

    with (
        patch(f"{ORCHESTRATOR}.AgentCEO") as mock_ceo_cls,
        patch(f"{ORCHESTRATOR}.AgentChat") as mock_chat_cls,
        patch(f"{ORCHESTRATOR}.TeamPlanExecutor") as mock_executor,
        patch(f"{ORCHESTRATOR}.get_anthropic_async_client"),
        patch(f"{ORCHESTRATOR}.BusinessMemoryService") as mock_bm_cls,
        patch(f"{ORCHESTRATOR}.AgentMemoryService") as mock_am_cls,
        patch(f"{ORCHESTRATOR}.ConversationService") as mock_conv_cls,
    ):
        mock_ceo_cls.return_value.process = AsyncMock(
            side_effect=lambda req: _make_ceo_plan_response(req.request_id)
        )
        mock_executor.return_value.execute = AsyncMock(
            return_value=[_make_sub_agent_response(request.request_id)]
        )
        generate_response_mock = AsyncMock(
            return_value=("Todo bien con las ventas.", _MOCK_LLM_CALL)
        )
        mock_chat_cls.return_value.generate_response = generate_response_mock

        # Todas las capas OK
        mock_bm_cls.return_value.get = AsyncMock(return_value={"ventas": 1000})
        mock_am_cls.return_value.get_context_fragment = AsyncMock(return_value="patron: efectivo")

        conv_svc = AsyncMock()
        conv_svc.get_context = AsyncMock(return_value={"turns": [], "summary": None})
        mock_conv_cls.return_value = conv_svc

        orchestrator = ChatOrchestrator()
        response = await orchestrator.handle(
            request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4()
        )

    # context_health: todo ok
    ctx_health = response.result.get("context_health")
    assert ctx_health is not None, "Debe haber 'context_health' en result"
    assert ctx_health["business_memory"] == "ok"
    assert ctx_health["agent_memory"] == "ok"
    assert ctx_health["file_context"] == "ok"

    # degraded_notice NO se pasa (o viene None)
    generate_response_mock.assert_awaited_once()
    call_kwargs = generate_response_mock.call_args.kwargs
    notice = call_kwargs.get("degraded_notice")
    assert notice is None, (
        f"En el caso feliz degraded_notice debe ser None, got: {notice!r}"
    )
