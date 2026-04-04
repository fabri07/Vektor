"""Tests unitarios para AgentCEO — sin llamadas reales al LLM ni a la DB."""

import json
import unittest.mock
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.agents.shared.schemas import AgentRequest


def _make_request(message: str = "test") -> AgentRequest:
    return AgentRequest(
        user_id="user-123",
        business_id="tenant-456",
        message=message,
    )


def _mock_llm_response(intent: str, entities: dict | None = None) -> MagicMock:
    """Construye un mock del response del cliente Anthropic."""
    content_block = MagicMock()
    content_block.text = json.dumps({"intent": intent, "entities": entities or {}})
    response = MagicMock()
    response.content = [content_block]
    return response


@pytest.mark.asyncio
async def test_ceo_classifies_sale_intent():
    """"vendí 100 pesos" debe clasificar como record_sale."""
    with unittest.mock.patch(
        "app.application.agents.ceo.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(
            return_value=_mock_llm_response("record_sale", {"amount": 100})
        )
        mock_cls.return_value = mock_client

        from app.application.agents.ceo.agent import AgentCEO

        agent = AgentCEO()
        agent.client = mock_client

        result = await agent.process(_make_request("vendí 100 pesos"))

    assert result.result["intent"] == "record_sale"
    assert result.result["action_type"] == "REGISTER_SALE"


@pytest.mark.asyncio
async def test_ceo_routes_to_correct_agent():
    """record_sale debe enrutar a agent_cash."""
    with unittest.mock.patch(
        "app.application.agents.ceo.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(
            return_value=_mock_llm_response("record_sale")
        )
        mock_cls.return_value = mock_client

        from app.application.agents.ceo.agent import AgentCEO

        agent = AgentCEO()
        agent.client = mock_client
        result = await agent.process(_make_request("vendí algo"))

    assert result.result["target_agent"] == "agent_cash"


@pytest.mark.asyncio
async def test_ceo_unknown_intent_goes_to_helper():
    """Intent inválido del LLM debe caer en ask_platform_help."""
    with unittest.mock.patch(
        "app.application.agents.ceo.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(
            return_value=_mock_llm_response("intent_que_no_existe")
        )
        mock_cls.return_value = mock_client

        from app.application.agents.ceo.agent import AgentCEO

        agent = AgentCEO()
        agent.client = mock_client
        result = await agent.process(_make_request("bla bla xyz"))

    assert result.result["intent"] == "ask_platform_help"
    assert result.result["target_agent"] == "agent_helper"


def test_ceo_forbidden_imports():
    """El módulo AgentCEO no debe importar los módulos de datos prohibidos."""
    import app.application.agents.ceo.agent as ceo_module

    forbidden = ["db.sales", "db.inventory", "db.cash_movements", "db.purchase_orders"]
    module_names = set(dir(ceo_module))
    for f in forbidden:
        assert f not in module_names, f"AgentCEO importó módulo prohibido: {f}"


@pytest.mark.asyncio
async def test_prompt_injection_wrapped():
    """El mensaje del usuario debe estar envuelto en <user_message>...</user_message>."""
    captured_call: dict = {}

    async def capture_create(**kwargs):
        captured_call.update(kwargs)
        content_block = MagicMock()
        content_block.text = json.dumps({"intent": "ask_platform_help", "entities": {}})
        response = MagicMock()
        response.content = [content_block]
        return response

    with unittest.mock.patch(
        "app.application.agents.ceo.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = capture_create
        mock_cls.return_value = mock_client

        from app.application.agents.ceo.agent import AgentCEO

        agent = AgentCEO()
        agent.client = mock_client
        await agent.process(_make_request("mensaje de prueba"))

    messages = captured_call.get("messages", [])
    assert len(messages) == 1
    user_content = messages[0]["content"]
    assert "<user_message>" in user_content
    assert "</user_message>" in user_content
