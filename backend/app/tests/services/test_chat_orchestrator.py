"""Tests del ChatOrchestrator.

Usan mocks para AgentCEO, sub-agentes, ConversationService y el cliente Anthropic.
No requieren ANTHROPIC_API_KEY ni acceso a red.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.application.agents.shared.schemas import (
    AgentRequest,
    AgentResponse,
    Confidence,
    RiskLevel,
)
from app.application.services.chat_orchestrator import ChatOrchestrator


def _make_request(conversation_id: str | None = None) -> AgentRequest:
    return AgentRequest(
        user_id=str(uuid.uuid4()),
        business_id=str(uuid.uuid4()),
        message="vendí 80 mil hoy",
        conversation_id=conversation_id,
    )


def _make_agent_response(
    *,
    status: str = "success",
    requires_approval: bool = False,
    summary: str = "Venta registrada correctamente.",
) -> AgentResponse:
    return AgentResponse(
        request_id=str(uuid.uuid4()),
        agent_name="agent_cash",
        status=status,
        risk_level=RiskLevel.LOW,
        requires_approval=requires_approval,
        confidence=Confidence.HIGH,
        result={"summary": summary, "action_type": "REGISTER_SALE"},
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


@pytest.mark.asyncio
async def test_orchestrator_returns_rich_message(mock_db, mock_redis):
    """El orchestrator no debe devolver 'Listo.' sino texto rico del LLM."""
    rich_text = "Registré la venta de $80.000. Tu caja del día suma $130.000. ¡Buen ritmo!"
    request = _make_request(conversation_id=str(uuid.uuid4()))
    ceo_resp = _make_agent_response(summary="Venta registrada.")
    ceo_resp.result["target_agent"] = "agent_cash"

    mock_content = MagicMock()
    mock_content.text = rich_text
    mock_llm_response = MagicMock()
    mock_llm_response.content = [mock_content]

    with (
        patch(
            "app.application.services.chat_orchestrator.AgentCEO"
        ) as MockCEO,
        patch(
            "app.application.services.chat_orchestrator.get_sub_agent"
        ) as mock_registry,
        patch(
            "app.application.services.chat_orchestrator.get_anthropic_async_client"
        ) as mock_client_factory,
        patch(
            "app.application.services.chat_orchestrator.ConversationService"
        ) as MockConvSvc,
    ):
        MockCEO.return_value.process = AsyncMock(return_value=ceo_resp)
        mock_registry.return_value.process = AsyncMock(return_value=_make_agent_response())

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_llm_response)
        mock_client_factory.return_value = mock_client

        conv_svc_instance = AsyncMock()
        conv_svc_instance.get_context = AsyncMock(return_value={"turns": [], "summary": None})
        conv_svc_instance.add_turn = AsyncMock()
        conv_svc_instance.persist = AsyncMock()
        MockConvSvc.return_value = conv_svc_instance

        orchestrator = ChatOrchestrator()
        response = await orchestrator.handle(
            request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4()
        )

    assert response.message == rich_text
    assert "Listo." not in (response.message or "")
    assert len(response.message or "") > 20


@pytest.mark.asyncio
async def test_orchestrator_skips_llm_for_approval(mock_db, mock_redis):
    """requires_approval no debe llamar al LLM final."""
    request = _make_request()
    approval_resp = _make_agent_response(
        status="requires_approval",
        requires_approval=True,
        summary="¿Confirmás la venta de $80.000?",
    )
    approval_resp.result["target_agent"] = "agent_cash"

    with (
        patch(
            "app.application.services.chat_orchestrator.AgentCEO"
        ) as MockCEO,
        patch(
            "app.application.services.chat_orchestrator.get_sub_agent"
        ) as mock_registry,
        patch(
            "app.application.services.chat_orchestrator.get_anthropic_async_client"
        ) as mock_client_factory,
    ):
        MockCEO.return_value.process = AsyncMock(return_value=approval_resp)
        mock_registry.return_value.process = AsyncMock(return_value=approval_resp)

        mock_client = AsyncMock()
        mock_client_factory.return_value = mock_client

        orchestrator = ChatOrchestrator()
        response = await orchestrator.handle(
            request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4()
        )

    mock_client.messages.create.assert_not_called()
    assert response.message == "¿Confirmás la venta de $80.000?"


@pytest.mark.asyncio
async def test_orchestrator_loads_conversation_context(mock_db, mock_redis):
    """El orchestrator llama a ConversationService.get_context cuando hay conversation_id."""
    conv_id = str(uuid.uuid4())
    request = _make_request(conversation_id=conv_id)
    ceo_resp = _make_agent_response()
    ceo_resp.result["target_agent"] = "agent_helper"

    mock_content = MagicMock()
    mock_content.text = "Hola, ¿en qué te puedo ayudar?"
    mock_llm_response = MagicMock()
    mock_llm_response.content = [mock_content]

    with (
        patch(
            "app.application.services.chat_orchestrator.AgentCEO"
        ) as MockCEO,
        patch(
            "app.application.services.chat_orchestrator.get_sub_agent"
        ) as mock_registry,
        patch(
            "app.application.services.chat_orchestrator.get_anthropic_async_client"
        ) as mock_client_factory,
        patch(
            "app.application.services.chat_orchestrator.ConversationService"
        ) as MockConvSvc,
    ):
        MockCEO.return_value.process = AsyncMock(return_value=ceo_resp)
        mock_registry.return_value.process = AsyncMock(return_value=_make_agent_response())

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_llm_response)
        mock_client_factory.return_value = mock_client

        conv_svc_instance = AsyncMock()
        conv_svc_instance.get_context = AsyncMock(return_value={"turns": [], "summary": None})
        conv_svc_instance.add_turn = AsyncMock()
        conv_svc_instance.persist = AsyncMock()
        MockConvSvc.return_value = conv_svc_instance

        orchestrator = ChatOrchestrator()
        await orchestrator.handle(request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4())

    conv_svc_instance.get_context.assert_called_once_with(conv_id)


@pytest.mark.asyncio
async def test_orchestrator_saves_turn_after_success(mock_db, mock_redis):
    """El orchestrator guarda el turno tras una respuesta exitosa."""
    conv_id = str(uuid.uuid4())
    request = _make_request(conversation_id=conv_id)
    ceo_resp = _make_agent_response()
    ceo_resp.result["target_agent"] = "agent_cash"

    mock_content = MagicMock()
    mock_content.text = "Todo guardado."
    mock_llm_response = MagicMock()
    mock_llm_response.content = [mock_content]

    with (
        patch(
            "app.application.services.chat_orchestrator.AgentCEO"
        ) as MockCEO,
        patch(
            "app.application.services.chat_orchestrator.get_sub_agent"
        ) as mock_registry,
        patch(
            "app.application.services.chat_orchestrator.get_anthropic_async_client"
        ) as mock_client_factory,
        patch(
            "app.application.services.chat_orchestrator.ConversationService"
        ) as MockConvSvc,
    ):
        MockCEO.return_value.process = AsyncMock(return_value=ceo_resp)
        mock_registry.return_value.process = AsyncMock(return_value=_make_agent_response())

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_llm_response)
        mock_client_factory.return_value = mock_client

        conv_svc_instance = AsyncMock()
        conv_svc_instance.get_context = AsyncMock(return_value={"turns": [], "summary": None})
        conv_svc_instance.add_turn = AsyncMock()
        conv_svc_instance.persist = AsyncMock()
        MockConvSvc.return_value = conv_svc_instance

        orchestrator = ChatOrchestrator()
        await orchestrator.handle(request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4())

    assert conv_svc_instance.add_turn.call_count == 2  # user + assistant
    conv_svc_instance.persist.assert_called_once()
