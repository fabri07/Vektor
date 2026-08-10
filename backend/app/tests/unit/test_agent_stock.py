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


async def test_stockout_detected():
    """stock=0 con threshold=0 → detect_stockout devuelve True."""
    with unittest.mock.patch("app.application.agents.stock.agent.anthropic.AsyncAnthropic"):
        from app.application.agents.stock.agent import AgentStock

        agent = AgentStock()
        result = await agent.detect_stockout("prod-1", current_qty=0, min_threshold=0)

    assert result is True


@pytest.mark.parametrize("vertical", [Vertical.KIOSCO_ALMACEN, Vertical.DECORACION_HOGAR])
async def test_overstock_usa_el_umbral_del_rubro(vertical: Vertical) -> None:
    """Sobrestock = más del doble del techo de rotación del rubro.

    El umbral se deriva del JSON en vez de fijarse en el test. Con días
    escritos a mano, recalibrar la rotación de un rubro contra su fuente
    sectorial rompía este test aunque la regla —el doble del techo— siguiera
    intacta. Y el rubro sigue importando: los dos casos se calculan contra SU
    propio umbral, así que un agente que ignorara el vertical fallaría igual.
    """
    from app.heuristics.verticals.loader import load_vertical_heuristics

    umbral = load_vertical_heuristics(vertical).inventory.rotation_days_max * 2

    with unittest.mock.patch("app.application.agents.stock.agent.anthropic.AsyncAnthropic"):
        from app.application.agents.stock.agent import AgentStock

        agent = AgentStock()
        arriba = await agent.detect_overstock(
            "prod-1", rotation_days=int(umbral + 10), business_type=vertical.value
        )
        abajo = await agent.detect_overstock(
            "prod-1", rotation_days=int(umbral - 10), business_type=vertical.value
        )

    assert arriba is True
    assert abajo is False


def _mock_intent_response(intent: str) -> MagicMock:
    content_block = MagicMock()
    content_block.text = json.dumps({"intent": intent})
    response = MagicMock()
    response.content = [content_block]
    return response


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
