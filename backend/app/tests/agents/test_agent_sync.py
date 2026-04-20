"""Tests unitarios para AgentSync — sin llamadas reales al LLM ni a Google."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.application.agents.shared.schemas import AgentRequest, RiskLevel


def _make_request(message: str = "exportar ventas a Google Sheets") -> AgentRequest:
    return AgentRequest(
        user_id="user-123",
        business_id="tenant-456",
        message=message,
    )


def _make_gateway(*, connected: bool = True) -> MagicMock:
    gateway = MagicMock()
    gateway.is_connected = AsyncMock(return_value=connected)
    gateway.get_oauth_url = AsyncMock(return_value="https://accounts.google.com/oauth?scope=sheets")
    return gateway


def _mock_llm_sync_response(sync_type: str) -> MagicMock:
    content_block = MagicMock()
    content_block.text = json.dumps({"sync_type": sync_type})
    response = MagicMock()
    response.content = [content_block]
    return response


@pytest.mark.asyncio
async def test_sync_not_connected_returns_auth_url():
    """Si gateway no está conectado → status requires_google_auth con auth_url."""
    from app.application.agents.sync.agent import AgentSync

    gateway = _make_gateway(connected=False)
    agent = AgentSync(gateway=gateway)

    result = await agent.process(_make_request())

    assert result.status == "requires_google_auth"
    assert result.risk_level == RiskLevel.LOW
    assert result.requires_approval is False
    assert result.result["auth_url"] == "https://accounts.google.com/oauth?scope=sheets"
    assert "Para sincronizar" in result.result["message"]


@pytest.mark.asyncio
async def test_sync_no_gateway_returns_auth_url_empty():
    """Sin gateway (None) → status requires_google_auth, auth_url vacío."""
    from app.application.agents.sync.agent import AgentSync

    agent = AgentSync(gateway=None)
    result = await agent.process(_make_request())

    assert result.status == "requires_google_auth"
    assert result.result["auth_url"] == ""


@pytest.mark.asyncio
async def test_sync_requires_approval_when_connected():
    """Cuando Google está conectado → requires_approval=True, risk MEDIUM, sync_type presente."""
    from app.application.agents.sync.agent import AgentSync

    gateway = _make_gateway(connected=True)
    agent = AgentSync(gateway=gateway)

    # Mock cliente LLM
    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(
        return_value=_mock_llm_sync_response("export_sales")
    )
    agent._client = mock_client

    result = await agent.process(_make_request("exportar ventas de esta semana"))

    assert result.status == "requires_approval"
    assert result.risk_level == RiskLevel.MEDIUM
    assert result.requires_approval is True
    assert result.result["structured_data"]["sync_type"] == "export_sales"
    assert "SYNC_TO_GOOGLE" in result.result["action_type"]


@pytest.mark.asyncio
async def test_sync_classify_export_expenses():
    """Mensaje sobre gastos → sync_type export_expenses."""
    from app.application.agents.sync.agent import AgentSync

    gateway = _make_gateway(connected=True)
    agent = AgentSync(gateway=gateway)

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(
        return_value=_mock_llm_sync_response("export_expenses")
    )
    agent._client = mock_client

    result = await agent.process(_make_request("subir mis gastos del mes a Sheets"))

    assert result.result["structured_data"]["sync_type"] == "export_expenses"


@pytest.mark.asyncio
async def test_sync_classify_fallback_on_llm_error():
    """Si el LLM falla, _classify_sync_type devuelve export_sales por defecto."""
    from app.application.agents.sync.agent import AgentSync

    gateway = _make_gateway(connected=True)
    agent = AgentSync(gateway=gateway)

    mock_client = MagicMock()
    mock_client.messages.create = AsyncMock(side_effect=RuntimeError("LLM unavailable"))
    agent._client = mock_client

    result = await agent.process(_make_request("sincronizar datos"))

    assert result.status == "requires_approval"
    assert result.result["structured_data"]["sync_type"] == "export_sales"
