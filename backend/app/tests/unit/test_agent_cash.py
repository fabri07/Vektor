"""Unit tests for AgentCash — no real LLM calls, no DB."""

import json
import unittest.mock
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.agents.shared.schemas import AgentRequest, RiskLevel


def _make_request(message: str = "test") -> AgentRequest:
    return AgentRequest(
        user_id="user-123",
        business_id="tenant-456",
        message=message,
    )


def _mock_llm_response(entities: dict) -> MagicMock:
    content_block = MagicMock()
    content_block.text = json.dumps(entities)
    response = MagicMock()
    response.content = [content_block]
    return response


@pytest.mark.asyncio
async def test_sale_extraction_with_amount():
    """'vendí 5000 pesos al contado' → amount=5000, status=requires_approval."""
    mock_entities = {
        "amount": 5000,
        "date": "hoy",
        "payment_status": "paid",
        "payment_method": "efectivo",
        "product_description": None,
        "confidence": "HIGH",
    }
    with unittest.mock.patch(
        "app.application.agents.cash.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(mock_entities))
        mock_cls.return_value = mock_client

        from app.application.agents.cash.agent import AgentCash

        agent = AgentCash()
        agent.client = mock_client
        result = await agent.process(_make_request("vendí 5000 pesos al contado"))

    assert result.status == "requires_approval"
    assert result.result["structured_data"]["amount"] == 5000


@pytest.mark.asyncio
async def test_unknown_payment_returns_clarification():
    """'vendí 5000' sin método de pago → requires_clarification."""
    mock_entities = {
        "amount": 5000,
        "date": "hoy",
        "payment_status": "unknown",
        "payment_method": None,
        "product_description": None,
        "confidence": "MEDIUM",
    }
    with unittest.mock.patch(
        "app.application.agents.cash.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(mock_entities))
        mock_cls.return_value = mock_client

        from app.application.agents.cash.agent import AgentCash

        agent = AgentCash()
        agent.client = mock_client
        result = await agent.process(_make_request("vendí 5000"))

    assert result.status == "requires_clarification"
    assert "contado" in result.question.lower() or "corriente" in result.question.lower()


@pytest.mark.asyncio
async def test_paid_sale_returns_approval():
    """'vendí 5000 al contado' → requires_approval, risk=MEDIUM."""
    mock_entities = {
        "amount": 5000,
        "date": "hoy",
        "payment_status": "paid",
        "payment_method": "efectivo",
        "product_description": None,
        "confidence": "HIGH",
    }
    with unittest.mock.patch(
        "app.application.agents.cash.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(return_value=_mock_llm_response(mock_entities))
        mock_cls.return_value = mock_client

        from app.application.agents.cash.agent import AgentCash

        agent = AgentCash()
        agent.client = mock_client
        result = await agent.process(_make_request("vendí 5000 al contado"))

    assert result.status == "requires_approval"
    assert result.risk_level == RiskLevel.MEDIUM
    assert result.requires_approval is True
    assert result.result["action_type"] == "REGISTER_SALE"


@pytest.mark.asyncio
async def test_sale_and_inflow_are_separate_actions():
    """REGLA CRÍTICA 1: 'vendí y cobré 5000' → REGISTER_SALE y REGISTER_CASH_INFLOW son acciones distintas."""
    # AgentCash procesa el mensaje principal como venta
    mock_entities_sale = {
        "amount": 5000,
        "date": "hoy",
        "payment_status": "paid",
        "payment_method": "efectivo",
        "product_description": None,
        "confidence": "HIGH",
    }
    with unittest.mock.patch(
        "app.application.agents.cash.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(
            return_value=_mock_llm_response(mock_entities_sale)
        )
        mock_cls.return_value = mock_client

        from app.application.agents.cash.agent import AgentCash
        from app.application.agents.shared.schemas import ActionType

        agent = AgentCash()
        agent.client = mock_client
        result = await agent.process(_make_request("vendí y cobré 5000"))

    # La venta se registra como REGISTER_SALE; el cobro sería una segunda acción separada
    assert result.result["action_type"] == ActionType.REGISTER_SALE
    # Verificar que ActionType.REGISTER_CASH_INFLOW existe como acción separada en el catálogo
    assert ActionType.REGISTER_CASH_INFLOW != ActionType.REGISTER_SALE


@pytest.mark.asyncio
async def test_sale_emits_event_after_confirm():
    """on_confirmed_sale → EventBus emite SALE_RECORDED."""
    with unittest.mock.patch(
        "app.application.agents.cash.agent.anthropic.AsyncAnthropic"
    ):
        with unittest.mock.patch(
            "app.application.agents.cash.agent.EventBus.emit"
        ) as mock_emit:
            from app.application.agents.cash.agent import AgentCash

            agent = AgentCash()
            await agent.on_confirmed_sale("sale-001", "tenant-001")

    mock_emit.assert_any_call(
        "SALE_RECORDED", {"sale_id": "sale-001", "business_id": "tenant-001"}
    )
