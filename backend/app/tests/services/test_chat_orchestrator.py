"""Tests del ChatOrchestrator.

Usan mocks para AgentCEO, sub-agentes, ConversationService y el cliente Anthropic.
No requieren ANTHROPIC_API_KEY ni acceso a red.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.application.agents.shared.schemas import (
    ActionType,
    AgentRequest,
    AgentResponse,
    Confidence,
    RiskLevel,
)
from app.application.services.chat_orchestrator import ChatOrchestrator
from app.integrations.anthropic_client import AnthropicConfigurationError


def _make_request(
    conversation_id: str | None = None,
    attachments: list[dict[str, str]] | None = None,
) -> AgentRequest:
    return AgentRequest(
        user_id=str(uuid.uuid4()),
        business_id=str(uuid.uuid4()),
        message="vendí 80 mil hoy",
        attachments=attachments or [],
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
        ) as mock_ceo_cls,
        patch(
            "app.application.services.chat_orchestrator.get_sub_agent"
        ) as mock_registry,
        patch(
            "app.application.services.chat_orchestrator.get_anthropic_async_client"
        ) as mock_client_factory,
        patch(
            "app.application.services.chat_orchestrator.ConversationService"
        ) as mock_conv_svc_cls,
    ):
        mock_ceo_cls.return_value.process = AsyncMock(return_value=ceo_resp)
        mock_registry.return_value.process = AsyncMock(return_value=_make_agent_response())

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_llm_response)
        mock_client_factory.return_value = mock_client

        conv_svc_instance = AsyncMock()
        conv_svc_instance.get_context = AsyncMock(return_value={"turns": [], "summary": None})
        conv_svc_instance.add_turn = AsyncMock()
        conv_svc_instance.persist = AsyncMock()
        mock_conv_svc_cls.return_value = conv_svc_instance

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
        ) as mock_ceo_cls,
        patch(
            "app.application.services.chat_orchestrator.get_sub_agent"
        ) as mock_registry,
        patch(
            "app.application.services.chat_orchestrator.get_anthropic_async_client"
        ) as mock_client_factory,
    ):
        mock_ceo_cls.return_value.process = AsyncMock(return_value=approval_resp)
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
async def test_orchestrator_passes_agent_task_from_plan(mock_db, mock_redis):
    """El orchestrator reconstruye AgentTask desde result['plan'] y lo pasa al sub-agente."""
    request = _make_request()
    task_id = str(uuid.uuid4())
    ceo_resp = AgentResponse(
        request_id=request.request_id,
        agent_name="agent_ceo",
        status="requires_approval",
        risk_level=RiskLevel.MEDIUM,
        requires_approval=True,
        confidence=Confidence.HIGH,
        result={
            "intent": "ingresar_cobro",
            "target_agent": "agent_income",
            "action_type": str(ActionType.REGISTER_CASH_INFLOW),
            "plan": {
                "plan_id": str(uuid.uuid4()),
                "intent": "ingresar_cobro",
                "tasks": [
                    {
                        "task_id": task_id,
                        "agent": "agent_income",
                        "action_type": str(ActionType.REGISTER_CASH_INFLOW),
                        "entities": {
                            "amount": "15000",
                            "payment_method": "transfer",
                        },
                        "depends_on": [],
                        "approval_group": None,
                    }
                ],
                "requires_synthesis": False,
                "fallback_message": None,
            },
        },
    )
    sub_response = AgentResponse(
        request_id=request.request_id,
        agent_name="agent_income",
        status="requires_approval",
        risk_level=RiskLevel.MEDIUM,
        requires_approval=True,
        confidence=Confidence.HIGH,
        result={
            "summary": "Registrar cobro por $15000",
            "action_type": ActionType.REGISTER_CASH_INFLOW,
        },
    )

    with (
        patch("app.application.services.chat_orchestrator.AgentCEO") as mock_ceo_cls,
        patch("app.application.services.chat_orchestrator.get_sub_agent") as mock_registry,
        patch("app.application.services.chat_orchestrator.get_anthropic_async_client"),
    ):
        mock_ceo_cls.return_value.process = AsyncMock(return_value=ceo_resp)
        sub_agent = MagicMock()
        sub_agent.process = AsyncMock(return_value=sub_response)
        mock_registry.return_value = sub_agent

        response = await ChatOrchestrator().handle(
            request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4()
        )

    sub_agent.process.assert_awaited_once()
    _, kwargs = sub_agent.process.await_args
    assert kwargs["task"].task_id == task_id
    assert kwargs["task"].agent == "agent_income"
    assert kwargs["task"].action_type == ActionType.REGISTER_CASH_INFLOW
    assert kwargs["task"].entities["amount"] == "15000"
    assert response.result["plan"] == ceo_resp.result["plan"]


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
        ) as mock_ceo_cls,
        patch(
            "app.application.services.chat_orchestrator.get_sub_agent"
        ) as mock_registry,
        patch(
            "app.application.services.chat_orchestrator.get_anthropic_async_client"
        ) as mock_client_factory,
        patch(
            "app.application.services.chat_orchestrator.ConversationService"
        ) as mock_conv_svc_cls,
    ):
        mock_ceo_cls.return_value.process = AsyncMock(return_value=ceo_resp)
        mock_registry.return_value.process = AsyncMock(return_value=_make_agent_response())

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_llm_response)
        mock_client_factory.return_value = mock_client

        conv_svc_instance = AsyncMock()
        conv_svc_instance.get_context = AsyncMock(return_value={"turns": [], "summary": None})
        conv_svc_instance.add_turn = AsyncMock()
        conv_svc_instance.persist = AsyncMock()
        mock_conv_svc_cls.return_value = conv_svc_instance

        orchestrator = ChatOrchestrator()
        await orchestrator.handle(request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4())

    conv_svc_instance.get_context.assert_called_once_with(conv_id)


@pytest.mark.asyncio
async def test_orchestrator_saves_turn_after_success(mock_db, mock_redis):
    """El orchestrator guarda el turno tras una respuesta exitosa."""
    conv_id = str(uuid.uuid4())
    request = _make_request(
        conversation_id=conv_id,
        attachments=[{"file_id": str(uuid.uuid4()), "filename": "ventas.csv"}],
    )
    ceo_resp = _make_agent_response()
    ceo_resp.result["target_agent"] = "agent_cash"

    mock_content = MagicMock()
    mock_content.text = "Todo guardado."
    mock_llm_response = MagicMock()
    mock_llm_response.content = [mock_content]

    with (
        patch(
            "app.application.services.chat_orchestrator.AgentCEO"
        ) as mock_ceo_cls,
        patch(
            "app.application.services.chat_orchestrator.get_sub_agent"
        ) as mock_registry,
        patch(
            "app.application.services.chat_orchestrator.get_anthropic_async_client"
        ) as mock_client_factory,
        patch(
            "app.application.services.chat_orchestrator.ConversationService"
        ) as mock_conv_svc_cls,
    ):
        mock_ceo_cls.return_value.process = AsyncMock(return_value=ceo_resp)
        mock_registry.return_value.process = AsyncMock(return_value=_make_agent_response())

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(return_value=mock_llm_response)
        mock_client_factory.return_value = mock_client

        conv_svc_instance = AsyncMock()
        conv_svc_instance.get_context = AsyncMock(return_value={"turns": [], "summary": None})
        conv_svc_instance.add_turn = AsyncMock()
        conv_svc_instance.persist = AsyncMock()
        mock_conv_svc_cls.return_value = conv_svc_instance

        orchestrator = ChatOrchestrator()
        await orchestrator.handle(request, mock_db, mock_redis, uuid.uuid4(), uuid.uuid4())

    assert conv_svc_instance.add_turn.call_count == 2  # user + assistant
    assert conv_svc_instance.add_turn.await_args_list[0].kwargs["metadata"] == {
        "attachments": request.attachments
    }
    conv_svc_instance.persist.assert_called_once()


@pytest.mark.asyncio
async def test_orchestrator_config_error_preserves_metadata_and_saves_turn(
    mock_db,
    mock_redis,
):
    """Si falla el LLM narrativo, conserva metadata CEO y guarda el turno."""
    request = _make_request(conversation_id=str(uuid.uuid4()))
    ceo_resp = _make_agent_response(summary="Consulta de stock.")
    ceo_resp.result.update({"intent": "ask_stock_status", "target_agent": "agent_stock"})

    with (
        patch("app.application.services.chat_orchestrator.AgentCEO") as mock_ceo_cls,
        patch("app.application.services.chat_orchestrator.get_sub_agent") as mock_registry,
        patch(
            "app.application.services.chat_orchestrator.get_anthropic_async_client"
        ) as mock_client_factory,
        patch("app.application.services.chat_orchestrator.ConversationService") as mock_conv_svc_cls,
    ):
        mock_ceo_cls.return_value.process = AsyncMock(return_value=ceo_resp)
        mock_registry.return_value.process = AsyncMock(return_value=_make_agent_response())

        mock_client = AsyncMock()
        mock_client.messages.create = AsyncMock(
            side_effect=AnthropicConfigurationError("missing api key")
        )
        mock_client_factory.return_value = mock_client

        conv_svc_instance = AsyncMock()
        conv_svc_instance.get_context = AsyncMock(return_value={"turns": [], "summary": None})
        conv_svc_instance.add_turn = AsyncMock()
        conv_svc_instance.persist = AsyncMock()
        mock_conv_svc_cls.return_value = conv_svc_instance

        response = await ChatOrchestrator().handle(
            request,
            mock_db,
            mock_redis,
            uuid.uuid4(),
            uuid.uuid4(),
        )

    assert response.status == "error"
    assert response.result["intent"] == "ask_stock_status"
    assert response.result["target_agent"] == "agent_stock"
    assert "IA no está disponible" in (response.message or "")
    assert conv_svc_instance.add_turn.call_count == 2
    conv_svc_instance.persist.assert_called_once()


@pytest.mark.asyncio
async def test_load_file_context_prioritizes_current_attachments():
    """Los adjuntos del request aparecen primero y los archivos viejos quedan como fallback."""
    tenant_id = uuid.uuid4()
    current_file_id = uuid.uuid4()

    current_file = MagicMock()
    current_file.id = current_file_id
    current_file.tenant_id = tenant_id  # necesario para el check de aislamiento
    current_file.original_filename = "ventas_actuales.csv"
    current_file.content_type = "text/csv"
    current_file.purpose = "chat"
    current_file.created_at = datetime(2026, 4, 21, tzinfo=UTC)
    current_file.parsed_summary_json = {
        "file_type": "spreadsheet",
        "rows_processed": 2,
        "headers": ["fecha", "monto"],
        "ventas_detectadas": [{"fecha": "2024-01-15", "monto": "50000"}],
    }

    old_file = MagicMock()
    old_file.id = uuid.uuid4()
    old_file.tenant_id = tenant_id  # necesario para el check de aislamiento
    old_file.original_filename = "ventas_ayer.csv"
    old_file.content_type = "text/csv"
    old_file.purpose = "chat"
    old_file.created_at = datetime(2026, 4, 20, tzinfo=UTC)
    old_file.parsed_summary_json = {
        "file_type": "spreadsheet",
        "source_format": "csv",
        "row_count": 4,
        "columns": ["fecha", "monto"],
        "preview_rows": [{"fecha": "2024-01-14", "monto": "45000"}],
    }

    current_result = MagicMock()
    current_result.scalars.return_value.all.return_value = [current_file]
    recent_result = MagicMock()
    recent_result.scalars.return_value.all.return_value = [current_file, old_file]

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[current_result, recent_result])

    with patch(
        "app.application.services.chat_orchestrator.get_anthropic_async_client"
    ):
        orchestrator = ChatOrchestrator()

    context = await orchestrator._load_file_context(
        tenant_id,
        db,
        attachments=[{"file_id": str(current_file_id), "filename": "ventas_actuales.csv"}],
    )

    assert "Adjuntos del mensaje actual:" in context
    assert "ventas_actuales.csv" in context
    assert "Archivos recientes del usuario:" in context
    assert "ventas_ayer.csv" in context
    assert context.index("Adjuntos del mensaje actual:") < context.index(
        "Archivos recientes del usuario:"
    )
