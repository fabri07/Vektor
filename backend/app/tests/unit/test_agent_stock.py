"""Unit tests for AgentStock — no real LLM calls, no DB."""

import json
import unittest.mock
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.agents.shared.schemas import AgentRequest, RiskLevel
from app.domain.verticals import Vertical


def _make_request(message: str = "test") -> AgentRequest:
    return AgentRequest(
        user_id="user-123",
        business_id="tenant-456",
        message=message,
    )


def _mock_llm_response(entities: dict[str, Any]) -> MagicMock:
    content_block = MagicMock()
    content_block.text = json.dumps(entities)
    response = MagicMock()
    response.content = [content_block]
    return response


@pytest.mark.asyncio
async def test_stockout_detected():
    """stock=0 con threshold=0 → detect_stockout devuelve True."""
    with unittest.mock.patch("app.application.agents.stock.agent.anthropic.AsyncAnthropic"):
        from app.application.agents.stock.agent import AgentStock

        agent = AgentStock()
        result = await agent.detect_stockout("prod-1", current_qty=0, min_threshold=0)

    assert result is True


@pytest.mark.asyncio
async def test_overstock_kiosco():
    """rotation_days=50 > threshold 42 (kiosco max=21 × 2) → overstock."""
    with unittest.mock.patch("app.application.agents.stock.agent.anthropic.AsyncAnthropic"):
        from app.application.agents.stock.agent import AgentStock

        agent = AgentStock()
        result = await agent.detect_overstock(
            "prod-1", rotation_days=50, business_type=Vertical.KIOSCO_ALMACEN.value
        )

    assert result is True


@pytest.mark.asyncio
async def test_overstock_decoracion():
    """rotation_days=350 < threshold 360 (decoracion max=180 × 2) → NOT overstock."""
    with unittest.mock.patch("app.application.agents.stock.agent.anthropic.AsyncAnthropic"):
        from app.application.agents.stock.agent import AgentStock

        agent = AgentStock()
        result = await agent.detect_overstock(
            "prod-1", rotation_days=350, business_type=Vertical.DECORACION_HOGAR.value
        )

    assert result is False


def _mock_intent_response(intent: str) -> MagicMock:
    content_block = MagicMock()
    content_block.text = json.dumps({"intent": intent})
    response = MagicMock()
    response.content = [content_block]
    return response


@pytest.mark.asyncio
async def test_stock_loss_is_high_risk():
    """Mensaje con 'merma' → risk_level=HIGH, requires_approval=True."""
    mock_entities = {
        "product_name": "Leche",
        "sku": None,
        "qty_change": -3,
        "reason": "merma",
        "confidence": "HIGH",
    }
    with unittest.mock.patch(
        "app.application.agents.stock.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(
            side_effect=[
                _mock_intent_response("STOCK_LOSS"),
                _mock_llm_response(mock_entities),
            ]
        )
        mock_cls.return_value = mock_client

        from app.application.agents.stock.agent import AgentStock

        agent = AgentStock()
        agent.client = mock_client
        with unittest.mock.patch.object(agent, "_resolve_product_id", return_value=("prod-1", [])):
            result = await agent.process(_make_request("merma de 3 unidades de leche"))

    assert result.risk_level == RiskLevel.HIGH
    assert result.requires_approval is True
    assert result.result["action_type"] == "REGISTER_STOCK_LOSS"


@pytest.mark.asyncio
async def test_stock_adjustment_is_medium_risk():
    """Mensaje con 'ajuste' → risk_level=MEDIUM, requires_approval=True."""
    mock_entities = {
        "product_name": "Gaseosa",
        "sku": None,
        "qty_change": 10,
        "reason": "ajuste",
        "confidence": "HIGH",
    }
    with unittest.mock.patch(
        "app.application.agents.stock.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(
            side_effect=[
                _mock_intent_response("STOCK_ADJUSTMENT"),
                _mock_llm_response(mock_entities),
            ]
        )
        mock_cls.return_value = mock_client

        from app.application.agents.stock.agent import AgentStock

        agent = AgentStock()
        agent.client = mock_client
        with unittest.mock.patch.object(agent, "_resolve_product_id", return_value=("prod-1", [])):
            result = await agent.process(_make_request("ajuste de inventario gaseosa +10"))

    assert result.risk_level == RiskLevel.MEDIUM
    assert result.requires_approval is True
    assert result.result["action_type"] == "UPDATE_STOCK"


@pytest.mark.asyncio
async def test_extraction_returns_negative_qty_for_loss():
    """LLM retorna qty_change=-5 → se preserva el valor negativo en structured_data."""
    mock_entities = {
        "product_name": "Yogur",
        "sku": None,
        "qty_change": -5,
        "reason": "merma",
        "confidence": "HIGH",
    }
    with unittest.mock.patch(
        "app.application.agents.stock.agent.anthropic.AsyncAnthropic"
    ) as mock_cls:
        mock_client = MagicMock()
        mock_client.messages.create = AsyncMock(
            side_effect=[
                _mock_intent_response("STOCK_LOSS"),
                _mock_llm_response(mock_entities),
            ]
        )
        mock_cls.return_value = mock_client

        from app.application.agents.stock.agent import AgentStock

        agent = AgentStock()
        agent.client = mock_client
        with unittest.mock.patch.object(agent, "_resolve_product_id", return_value=("prod-1", [])):
            result = await agent.process(_make_request("merma de 5 unidades de yogur"))

    assert result.result["structured_data"]["qty_change"] == -5
