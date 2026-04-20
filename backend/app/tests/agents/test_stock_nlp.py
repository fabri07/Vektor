"""Tests NLP robusto para AgentStock — clasificación de intent con Haiku y fallback."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.agents.shared.schemas import AgentRequest, RiskLevel


def _make_request(message: str) -> AgentRequest:
    return AgentRequest(
        user_id="user-123",
        business_id="tenant-456",
        message=message,
    )


def _mock_llm_intent_response(intent: str) -> MagicMock:
    content_block = MagicMock()
    content_block.text = json.dumps({"intent": intent})
    response = MagicMock()
    response.content = [content_block]
    return response


def _mock_llm_entities_response(entities: dict) -> MagicMock:
    content_block = MagicMock()
    content_block.text = json.dumps(entities)
    response = MagicMock()
    response.content = [content_block]
    return response


@pytest.mark.asyncio
async def test_stock_loss_intent_classified_correctly():
    """Mock Haiku devuelve STOCK_LOSS para 'se cayó una caja de vino' → _handle_stock_loss."""
    from app.application.agents.stock.agent import AgentStock

    mock_entities = {
        "product_name": "Vino",
        "sku": None,
        "qty_change": -12,
        "reason": "merma",
        "confidence": "HIGH",
    }

    mock_client = MagicMock()
    # Primera llamada: clasificación de intent → STOCK_LOSS
    # Segunda llamada: extracción de entidades
    mock_client.messages.create = AsyncMock(
        side_effect=[
            _mock_llm_intent_response("STOCK_LOSS"),
            _mock_llm_entities_response(mock_entities),
        ]
    )

    agent = AgentStock()
    agent.client = mock_client

    result = await agent.process(_make_request("se cayó una caja de vino"))

    assert result.risk_level == RiskLevel.HIGH
    assert result.requires_approval is True
    assert result.result["action_type"] == "REGISTER_STOCK_LOSS"
    assert result.result["structured_data"]["product_name"] == "Vino"


@pytest.mark.asyncio
async def test_stock_adjustment_intent_classified_correctly():
    """Mock Haiku devuelve STOCK_ADJUSTMENT → _handle_stock_adjustment (risk MEDIUM)."""
    from app.application.agents.stock.agent import AgentStock

    mock_entities = {
        "product_name": "Gaseosa",
        "sku": None,
        "qty_change": 5,
        "reason": "ajuste",
        "confidence": "HIGH",
    }

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(
        side_effect=[
            _mock_llm_intent_response("STOCK_ADJUSTMENT"),
            _mock_llm_entities_response(mock_entities),
        ]
    )

    agent = AgentStock()
    agent.client = mock_client

    result = await agent.process(_make_request("hice un conteo y hay 5 gaseosas más"))

    assert result.risk_level == RiskLevel.MEDIUM
    assert result.requires_approval is True
    assert result.result["action_type"] == "UPDATE_STOCK"


@pytest.mark.asyncio
async def test_stock_query_fallback_on_llm_error():
    """Si el LLM falla, el fallback por keywords devuelve STOCK_QUERY para mensaje neutro."""
    from app.application.agents.stock.agent import AgentStock

    mock_client = MagicMock()
    # LLM falla en la clasificación de intent (primera llamada)
    mock_client.messages.create = AsyncMock(
        side_effect=RuntimeError("LLM unavailable")
    )

    agent = AgentStock()
    agent.client = mock_client

    result = await agent.process(_make_request("cuánto stock tengo disponible"))

    # Sin keywords de merma ni ajuste → fallback a STOCK_QUERY → _handle_query
    assert result.status == "success"
    assert result.risk_level == RiskLevel.LOW
    assert result.requires_approval is False


@pytest.mark.asyncio
async def test_stock_loss_fallback_keywords():
    """Si el LLM falla, el fallback detecta 'merma' por keywords → STOCK_LOSS."""
    from app.application.agents.stock.agent import AgentStock

    mock_entities = {
        "product_name": "Leche",
        "sku": None,
        "qty_change": -3,
        "reason": "merma",
        "confidence": "HIGH",
    }

    call_count = 0

    async def side_effect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # Primera llamada (clasificación de intent) falla
            raise RuntimeError("LLM timeout")
        # Segunda llamada (extracción de entidades) retorna entities
        return _mock_llm_entities_response(mock_entities)

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=side_effect)

    agent = AgentStock()
    agent.client = mock_client

    result = await agent.process(_make_request("tengo merma de 3 unidades de leche"))

    assert result.risk_level == RiskLevel.HIGH
    assert result.requires_approval is True
    assert result.result["action_type"] == "REGISTER_STOCK_LOSS"


@pytest.mark.asyncio
async def test_stock_loss_invalid_intent_falls_to_query():
    """Si el LLM devuelve un intent inválido, se normaliza a STOCK_QUERY."""
    from app.application.agents.stock.agent import AgentStock

    mock_client = MagicMock()
    # LLM devuelve intent fuera del catálogo
    content_block = MagicMock()
    content_block.text = json.dumps({"intent": "UNKNOWN_INTENT"})
    invalid_response = MagicMock()
    invalid_response.content = [content_block]

    mock_client.messages.create = AsyncMock(return_value=invalid_response)

    agent = AgentStock()
    agent.client = mock_client

    result = await agent.process(_make_request("algo raro"))

    # UNKNOWN_INTENT → normalizado a STOCK_QUERY → _handle_query → success
    assert result.status == "success"
    assert result.risk_level == RiskLevel.LOW
