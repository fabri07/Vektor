"""Cableado end-to-end del advisory (F1+F3): gate de domain + handler income.

El crux de calidad (condición 1 del plan aprobado): si `pedir_consejo` llega
sin domain o con un domain no reconocido, el orchestrator NUNCA despacha con
un default silencioso — pide aclaración con las opciones reales.
"""

from __future__ import annotations

import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.application.agents.shared.schemas import (
    ActionType,
    AgentRequest,
    AgentResponse,
    Confidence,
    LLMCall,
    RiskLevel,
)
from app.application.services.chat_orchestrator import ChatOrchestrator

ORCHESTRATOR = "app.application.services.chat_orchestrator"
GAP_SERVICE = "app.application.services.coverage_gap_service"


def _make_request(message: str = "dame una idea para las ventas") -> AgentRequest:
    return AgentRequest(
        user_id=str(uuid.uuid4()),
        business_id=str(uuid.uuid4()),
        message=message,
        attachments=[],
        conversation_id=None,
    )


def _ceo_plan_response(
    request_id: str, intent: str, domain: str | None, agent: str = "agent_income"
) -> AgentResponse:
    plan_dict = {
        "plan_id": str(uuid.uuid4()),
        "intent": intent,
        "tasks": [
            {
                "task_id": str(uuid.uuid4()),
                "agent": agent,
                "action_type": "ANSWER_DATA_QUERY",
                "entities": {"domain": domain, "_intent": "consejo"},
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
            "intent": intent,
            "target_agent": agent,
            "plan": plan_dict,
            "confidence_float": 0.9,
        },
    )


@pytest.fixture
def mock_db():
    db = AsyncMock()
    tenant = MagicMock()
    tenant.display_name = "Kiosco El Rápido"
    db.get = AsyncMock(return_value=tenant)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = MagicMock(vertical_code="kiosco_almacen")
    db.execute = AsyncMock(return_value=result_mock)
    return db


@pytest.fixture
def mock_redis():
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()
    return redis


def _enter_common_patches(stack: ExitStack, ceo_intent: str, domain: str | None) -> None:
    stack.enter_context(
        patch(
            f"{ORCHESTRATOR}.AgentCEO",
            return_value=MagicMock(
                process=AsyncMock(
                    side_effect=lambda req: _ceo_plan_response(
                        req.request_id, ceo_intent, domain
                    )
                )
            ),
        )
    )
    stack.enter_context(patch(f"{ORCHESTRATOR}.get_anthropic_async_client"))
    stack.enter_context(patch(f"{ORCHESTRATOR}.AgentChat"))
    stack.enter_context(patch(f"{ORCHESTRATOR}.BusinessMemoryService"))
    stack.enter_context(patch(f"{ORCHESTRATOR}.AgentMemoryService"))


@pytest.mark.asyncio
async def test_missing_domain_asks_clarification_never_defaults(mock_db, mock_redis) -> None:
    """El crux: sin domain, NUNCA se despacha con default silencioso a 'ventas'."""
    request = _make_request("dame una idea")
    with ExitStack() as stack:
        _enter_common_patches(stack, "pedir_consejo", domain=None)
        executor_cls = stack.enter_context(patch(f"{ORCHESTRATOR}.TeamPlanExecutor"))
        gap_cls = stack.enter_context(patch(f"{GAP_SERVICE}.CoverageGapService"))
        gap_cls.return_value.log_gap = AsyncMock()
        response = await ChatOrchestrator().handle(
            request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4()
        )

    executor_cls.return_value.execute.assert_not_called()
    assert response.status == "requires_clarification"
    assert response.question is not None
    # Las opciones reales están en la pregunta, no un default oculto.
    assert "ventas" in response.question
    assert "stock" in response.question
    gap_cls.return_value.log_gap.assert_awaited_once()
    await_args = gap_cls.return_value.log_gap.await_args
    assert await_args is not None
    assert await_args.kwargs["fallback_reason"] == "baja_confianza"


@pytest.mark.asyncio
async def test_unrecognized_domain_asks_clarification(mock_db, mock_redis) -> None:
    """Domain que no está en DOMAIN_TO_AGENT (typo/alucinación del CEO) → mismo gate."""
    request = _make_request("dame una idea para mejorar")
    with ExitStack() as stack:
        _enter_common_patches(stack, "pedir_consejo", domain="rentabilidad_general")
        stack.enter_context(patch(f"{ORCHESTRATOR}.TeamPlanExecutor"))
        gap_cls = stack.enter_context(patch(f"{GAP_SERVICE}.CoverageGapService"))
        gap_cls.return_value.log_gap = AsyncMock()
        response = await ChatOrchestrator().handle(
            request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4()
        )

    assert response.status == "requires_clarification"


@pytest.mark.asyncio
async def test_valid_domain_dispatches_to_agent(mock_db, mock_redis) -> None:
    """Domain reconocido → despacha normalmente (no interviene el gate) y el
    consejo del agente llega intacto en la respuesta final."""
    request = _make_request("dame una idea para las ventas")
    advice_text = "De cada $100 que entran, te quedan $14..."
    fake_agent_response = AgentResponse(
        request_id=request.request_id,
        agent_name="agent_income",
        status="success",
        risk_level=RiskLevel.LOW,
        requires_approval=False,
        confidence=Confidence.HIGH,
        message=advice_text,
        result={"summary": "Consejo de negocio.", "advisory": True},
    )
    with ExitStack() as stack:
        _enter_common_patches(stack, "pedir_consejo", domain="ventas")
        executor_cls = stack.enter_context(patch(f"{ORCHESTRATOR}.TeamPlanExecutor"))
        executor_cls.return_value.execute = AsyncMock(
            return_value=[fake_agent_response]
        )
        agent_chat_cls = stack.enter_context(patch(f"{ORCHESTRATOR}.AgentChat"))
        agent_chat_cls.return_value.generate_response = AsyncMock(
            return_value=(
                advice_text,
                LLMCall(
                    source="agent_chat",
                    model="claude-sonnet-4-6",
                    input_tokens=10,
                    output_tokens=20,
                ),
            )
        )
        response = await ChatOrchestrator().handle(
            request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4()
        )

    assert response.status != "requires_clarification"
    executor_cls.return_value.execute.assert_awaited_once()
    assert response.message == advice_text


# ── Handler income: branch de consejo vs regresión de consulta normal ────────


@pytest.mark.asyncio
async def test_income_handler_routes_advisory_intent_to_handle_advice() -> None:
    from app.application.agents.income.agent import AgentIncome
    from app.application.agents.shared.schemas import AgentTask

    agent = AgentIncome()
    agent._db = MagicMock()
    agent.client = MagicMock()
    agent._tenant_uuid = AsyncMock(return_value=uuid.uuid4())  # type: ignore[method-assign]

    task = AgentTask(
        task_id=str(uuid.uuid4()),
        agent="agent_income",
        action_type=ActionType.ANSWER_DATA_QUERY,
        entities={"domain": "ventas", "_intent": "consejo"},
    )
    request = _make_request("dame una idea para las ventas")

    fake_response = AgentResponse(
        request_id=request.request_id,
        agent_name="agent_income",
        status="success",
        risk_level=RiskLevel.LOW,
        requires_approval=False,
        confidence=Confidence.HIGH,
        message="consejo",
        result={"advisory": True},
    )
    with patch(
        "app.application.agents.shared.advisory.handle_advice",
        AsyncMock(return_value=fake_response),
    ) as handle_advice_mock:
        response = await agent._handle_data_query(request, task)

    handle_advice_mock.assert_awaited_once()
    assert response.result["advisory"] is True


@pytest.mark.asyncio
async def test_income_handler_regular_query_does_not_use_advisory() -> None:
    """Sin el marcador _intent='consejo', el camino de consulta normal sigue
    intacto (regresión) — no debe llamar a handle_advice."""
    from app.application.agents.income.agent import AgentIncome
    from app.application.agents.shared.schemas import AgentTask

    agent = AgentIncome()
    agent._db = MagicMock()
    agent.client = MagicMock()
    agent._tenant_uuid = AsyncMock(return_value=uuid.uuid4())  # type: ignore[method-assign]

    task = AgentTask(
        task_id=str(uuid.uuid4()),
        agent="agent_income",
        action_type=ActionType.ANSWER_DATA_QUERY,
        entities={"domain": "ventas"},
    )
    request = _make_request("cuánto vendí ayer")

    with (
        patch(
            "app.application.agents.shared.advisory.handle_advice", AsyncMock()
        ) as handle_advice_mock,
        patch(
            "app.persistence.repositories.transaction_repository.SaleRepository"
        ) as repo_cls,
    ):
        repo_cls.return_value.get_sales_by_customer = AsyncMock(return_value=[])
        repo_cls.return_value.get_sales_by_product = AsyncMock(return_value=[])
        await agent._handle_data_query(request, task)

    handle_advice_mock.assert_not_called()
