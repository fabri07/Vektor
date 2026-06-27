"""Tests del gate de confianza en ChatOrchestrator (Task 1 — F0.1).

Verifica:
- confidence_float < 0.72 con intent válido → requires_clarification sin despachar sub-agente
- confidence_float >= 0.72 → despacha normalmente
- confidence_float ausente (None) → despacha normalmente (backward compat)
- La LLMCall del CEO se acumula en el usage aunque se corte temprano
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.application.agents.shared.schemas import (
    ActionType,
    AgentRequest,
    AgentResponse,
    Confidence,
    LLMCall,
    RiskLevel,
    UsageSummary,
)
from app.application.services.chat_orchestrator import ChatOrchestrator


def _make_request() -> AgentRequest:
    return AgentRequest(
        user_id=str(uuid.uuid4()),
        business_id=str(uuid.uuid4()),
        message="quiero algo",
    )


def _make_ceo_response(
    request_id: str,
    intent: str = "ingresar_venta",
    confidence_float: float | None = None,
    ambiguous_with: list[str] | None = None,
) -> AgentResponse:
    """AgentResponse simulado del CEO con plan válido y campo confidence_float."""
    plan_dict = {
        "plan_id": str(uuid.uuid4()),
        "intent": intent,
        "tasks": [
            {
                "task_id": str(uuid.uuid4()),
                "agent": "agent_income",
                "action_type": "REGISTER_SALE",
                "entities": {},
                "depends_on": [],
                "approval_group": None,
            }
        ],
        "requires_synthesis": False,
        "fallback_message": None,
    }
    result: dict = {
        "intent": intent,
        "action_type": "REGISTER_SALE",
        "target_agent": "agent_income",
        "plan": plan_dict,
    }
    if confidence_float is not None:
        result["confidence_float"] = confidence_float
    if ambiguous_with is not None:
        result["ambiguous_with"] = ambiguous_with

    return AgentResponse(
        request_id=request_id,
        agent_name="agent_ceo",
        status="success",
        risk_level=RiskLevel.LOW,
        requires_approval=False,
        confidence=Confidence.LOW if (confidence_float is not None and confidence_float < 0.72) else Confidence.HIGH,
        result=result,
        usage=UsageSummary(calls=[
            LLMCall(source="ceo", model="claude-sonnet-4-6", input_tokens=50, output_tokens=20)
        ]),
    )


def _make_agent_response(request_id: str) -> AgentResponse:
    return AgentResponse(
        request_id=request_id,
        agent_name="agent_income",
        status="success",
        risk_level=RiskLevel.LOW,
        requires_approval=False,
        confidence=Confidence.HIGH,
        result={"summary": "Venta registrada.", "action_type": "REGISTER_SALE"},
    )


@pytest.fixture
def mock_db():
    db = AsyncMock()
    tenant = MagicMock()
    tenant.display_name = "Kiosco Test"
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


ORCHESTRATOR = "app.application.services.chat_orchestrator"


@pytest.mark.asyncio
async def test_gate_low_confidence_returns_clarification(mock_db, mock_redis):
    """confidence_float < 0.72 con intent válido → requires_clarification (no dispatch)."""
    request = _make_request()

    with (
        patch(f"{ORCHESTRATOR}.AgentCEO") as mock_ceo_cls,
        patch(f"{ORCHESTRATOR}.TeamPlanExecutor") as mock_executor,
        patch(f"{ORCHESTRATOR}.get_anthropic_async_client"),
        patch(f"{ORCHESTRATOR}.AgentChat"),
    ):
        mock_ceo_cls.return_value.process = AsyncMock(
            side_effect=lambda req: _make_ceo_response(
                req.request_id,
                intent="ingresar_venta",
                confidence_float=0.5,
                ambiguous_with=["ingresar_gasto"],
            )
        )
        mock_executor_instance = mock_executor.return_value
        mock_executor_instance.execute = AsyncMock(return_value=[])

        orchestrator = ChatOrchestrator()
        response = await orchestrator.handle(
            request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4()
        )

    assert response.status == "requires_clarification"
    assert response.confidence == Confidence.LOW
    assert response.requires_approval is False
    # Sub-agente NO fue invocado
    mock_executor_instance.execute.assert_not_called()


@pytest.mark.asyncio
async def test_gate_low_confidence_question_mentions_alternatives(mock_db, mock_redis):
    """El gate usa las descripciones amigables del INTENT_CATALOG, no los keys técnicos."""
    request = _make_request()

    with (
        patch(f"{ORCHESTRATOR}.AgentCEO") as mock_ceo_cls,
        patch(f"{ORCHESTRATOR}.TeamPlanExecutor"),
        patch(f"{ORCHESTRATOR}.get_anthropic_async_client"),
        patch(f"{ORCHESTRATOR}.AgentChat"),
    ):
        mock_ceo_cls.return_value.process = AsyncMock(
            side_effect=lambda req: _make_ceo_response(
                req.request_id,
                intent="ingresar_venta",
                confidence_float=0.4,
                ambiguous_with=["ingresar_gasto"],
            )
        )

        orchestrator = ChatOrchestrator()
        response = await orchestrator.handle(
            request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4()
        )

    assert response.status == "requires_clarification"
    assert response.question is not None
    q = response.question
    # Las descripciones amigables deben estar en la pregunta
    assert "venta" in q.lower(), f"La pregunta debería mencionar 'venta': {q!r}"
    assert "gasto" in q.lower(), f"La pregunta debería mencionar 'gasto': {q!r}"
    # Los keys técnicos NO deben aparecer en la pregunta
    assert "ingresar_venta" not in q, f"No debe exponer el key técnico: {q!r}"
    assert "ingresar_gasto" not in q, f"No debe exponer el key técnico: {q!r}"


@pytest.mark.asyncio
async def test_gate_low_confidence_no_alternatives_generic_question(mock_db, mock_redis):
    """El gate con ambiguous_with=[] devuelve una pregunta genérica pidiendo más detalle."""
    request = _make_request()

    with (
        patch(f"{ORCHESTRATOR}.AgentCEO") as mock_ceo_cls,
        patch(f"{ORCHESTRATOR}.TeamPlanExecutor"),
        patch(f"{ORCHESTRATOR}.get_anthropic_async_client"),
        patch(f"{ORCHESTRATOR}.AgentChat"),
    ):
        mock_ceo_cls.return_value.process = AsyncMock(
            side_effect=lambda req: _make_ceo_response(
                req.request_id,
                intent="ingresar_venta",
                confidence_float=0.4,
                ambiguous_with=[],
            )
        )

        orchestrator = ChatOrchestrator()
        response = await orchestrator.handle(
            request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4()
        )

    assert response.status == "requires_clarification"
    assert response.question is not None
    q = response.question
    # Sin alternativas: pregunta genérica que pide más detalle
    assert len(q) > 10
    # Debe pedir más información al usuario (alguna variante de "qué", "detalle", "entend")
    q_lower = q.lower()
    assert any(kw in q_lower for kw in ("qué", "que", "detalle", "entend", "necesit", "podés")), (
        f"La pregunta genérica debería pedir más información: {q!r}"
    )
    # No debe exponer keys técnicos
    assert "ingresar_venta" not in q, f"No debe exponer el key técnico: {q!r}"


@pytest.mark.asyncio
async def test_gate_more_than_two_alternatives(mock_db, mock_redis):
    """Con >2 alternativas la pregunta usa join de descripciones amigables (no keys técnicos)."""
    request = _make_request()

    with (
        patch(f"{ORCHESTRATOR}.AgentCEO") as mock_ceo_cls,
        patch(f"{ORCHESTRATOR}.TeamPlanExecutor"),
        patch(f"{ORCHESTRATOR}.get_anthropic_async_client"),
        patch(f"{ORCHESTRATOR}.AgentChat"),
    ):
        mock_ceo_cls.return_value.process = AsyncMock(
            side_effect=lambda req: _make_ceo_response(
                req.request_id,
                intent="ingresar_venta",
                confidence_float=0.35,
                ambiguous_with=["ingresar_gasto", "actualizar_stock"],
            )
        )

        orchestrator = ChatOrchestrator()
        response = await orchestrator.handle(
            request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4()
        )

    assert response.status == "requires_clarification"
    assert response.question is not None
    q = response.question
    # Las tres descripciones deben estar presentes (la del intent detectado + las 2 alternativas)
    assert "venta" in q.lower(), f"Debe mencionar 'venta': {q!r}"
    assert "gasto" in q.lower(), f"Debe mencionar 'gasto': {q!r}"
    assert "stock" in q.lower(), f"Debe mencionar 'stock': {q!r}"
    # Los keys técnicos NO deben aparecer
    assert "ingresar_venta" not in q
    assert "ingresar_gasto" not in q
    assert "actualizar_stock" not in q


@pytest.mark.asyncio
async def test_gate_low_confidence_preserves_ceo_llm_call(mock_db, mock_redis):
    """La LLMCall del CEO se incluye en usage aunque el gate corte temprano."""
    request = _make_request()

    with (
        patch(f"{ORCHESTRATOR}.AgentCEO") as mock_ceo_cls,
        patch(f"{ORCHESTRATOR}.TeamPlanExecutor"),
        patch(f"{ORCHESTRATOR}.get_anthropic_async_client"),
        patch(f"{ORCHESTRATOR}.AgentChat"),
    ):
        mock_ceo_cls.return_value.process = AsyncMock(
            side_effect=lambda req: _make_ceo_response(
                req.request_id,
                intent="ingresar_venta",
                confidence_float=0.3,
                ambiguous_with=[],
            )
        )

        orchestrator = ChatOrchestrator()
        response = await orchestrator.handle(
            request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4()
        )

    assert response.usage is not None
    assert len(response.usage.calls) >= 1
    ceo_call = response.usage.calls[0]
    assert ceo_call.source == "ceo"


@pytest.mark.asyncio
async def test_gate_high_confidence_dispatches_normally(mock_db, mock_redis):
    """confidence_float >= 0.72 → despacha al sub-agente normalmente."""
    request = _make_request()
    _mock_llm_call = LLMCall(
        source="agent_chat", model="claude-sonnet-4-6", input_tokens=100, output_tokens=200
    )

    with (
        patch(f"{ORCHESTRATOR}.AgentCEO") as mock_ceo_cls,
        patch(f"{ORCHESTRATOR}.TeamPlanExecutor") as mock_executor,
        patch(f"{ORCHESTRATOR}.get_anthropic_async_client"),
        patch(f"{ORCHESTRATOR}.AgentChat") as mock_agent_chat_cls,
        patch(f"{ORCHESTRATOR}.ConversationService") as mock_conv_svc_cls,
    ):
        mock_ceo_cls.return_value.process = AsyncMock(
            side_effect=lambda req: _make_ceo_response(
                req.request_id,
                intent="ingresar_venta",
                confidence_float=0.90,  # alta confianza
            )
        )
        sub_resp = _make_agent_response(request.request_id)
        mock_executor.return_value.execute = AsyncMock(return_value=[sub_resp])
        mock_agent_chat_cls.return_value.generate_response = AsyncMock(
            return_value=("Registré la venta.", _mock_llm_call)
        )
        conv_svc = AsyncMock()
        conv_svc.get_context = AsyncMock(return_value={"turns": [], "summary": None})
        conv_svc.add_turn = AsyncMock()
        conv_svc.persist = AsyncMock()
        mock_conv_svc_cls.return_value = conv_svc

        orchestrator = ChatOrchestrator()
        response = await orchestrator.handle(
            request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4()
        )

    # Se ejecutó el sub-agente
    mock_executor.return_value.execute.assert_awaited_once()
    assert response.status == "success"


@pytest.mark.asyncio
async def test_gate_confidence_float_absent_dispatches_normally(mock_db, mock_redis):
    """Si confidence_float no está en el result del CEO (backward compat), despacha normalmente."""
    request = _make_request()
    _mock_llm_call = LLMCall(
        source="agent_chat", model="claude-sonnet-4-6", input_tokens=100, output_tokens=200
    )

    with (
        patch(f"{ORCHESTRATOR}.AgentCEO") as mock_ceo_cls,
        patch(f"{ORCHESTRATOR}.TeamPlanExecutor") as mock_executor,
        patch(f"{ORCHESTRATOR}.get_anthropic_async_client"),
        patch(f"{ORCHESTRATOR}.AgentChat") as mock_agent_chat_cls,
        patch(f"{ORCHESTRATOR}.ConversationService") as mock_conv_svc_cls,
    ):
        # CEO sin confidence_float en el result (mock legacy)
        mock_ceo_cls.return_value.process = AsyncMock(
            side_effect=lambda req: _make_ceo_response(
                req.request_id,
                intent="ingresar_venta",
                confidence_float=None,  # ausente
            )
        )
        sub_resp = _make_agent_response(request.request_id)
        mock_executor.return_value.execute = AsyncMock(return_value=[sub_resp])
        mock_agent_chat_cls.return_value.generate_response = AsyncMock(
            return_value=("Venta registrada.", _mock_llm_call)
        )
        conv_svc = AsyncMock()
        conv_svc.get_context = AsyncMock(return_value={"turns": [], "summary": None})
        conv_svc.add_turn = AsyncMock()
        conv_svc.persist = AsyncMock()
        mock_conv_svc_cls.return_value = conv_svc

        orchestrator = ChatOrchestrator()
        response = await orchestrator.handle(
            request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4()
        )

    mock_executor.return_value.execute.assert_awaited_once()
    assert response.status == "success"


@pytest.mark.asyncio
async def test_gate_exact_threshold_dispatches(mock_db, mock_redis):
    """confidence_float == 0.72 (umbral exacto) → despacha (no activa el gate)."""
    request = _make_request()
    _mock_llm_call = LLMCall(
        source="agent_chat", model="claude-sonnet-4-6", input_tokens=100, output_tokens=200
    )

    with (
        patch(f"{ORCHESTRATOR}.AgentCEO") as mock_ceo_cls,
        patch(f"{ORCHESTRATOR}.TeamPlanExecutor") as mock_executor,
        patch(f"{ORCHESTRATOR}.get_anthropic_async_client"),
        patch(f"{ORCHESTRATOR}.AgentChat") as mock_agent_chat_cls,
        patch(f"{ORCHESTRATOR}.ConversationService") as mock_conv_svc_cls,
    ):
        mock_ceo_cls.return_value.process = AsyncMock(
            side_effect=lambda req: _make_ceo_response(
                req.request_id,
                intent="ingresar_venta",
                confidence_float=0.72,  # exactamente el umbral
            )
        )
        sub_resp = _make_agent_response(request.request_id)
        mock_executor.return_value.execute = AsyncMock(return_value=[sub_resp])
        mock_agent_chat_cls.return_value.generate_response = AsyncMock(
            return_value=("OK.", _mock_llm_call)
        )
        conv_svc = AsyncMock()
        conv_svc.get_context = AsyncMock(return_value={"turns": [], "summary": None})
        conv_svc.add_turn = AsyncMock()
        conv_svc.persist = AsyncMock()
        mock_conv_svc_cls.return_value = conv_svc

        orchestrator = ChatOrchestrator()
        response = await orchestrator.handle(
            request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4()
        )

    mock_executor.return_value.execute.assert_awaited_once()
    assert response.status == "success"
