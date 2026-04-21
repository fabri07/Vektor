"""Tests de integración para HttpMcpGateway — mock del servidor HTTP."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.integrations.mcp.exceptions import (
    McpToolAuthError,
    McpToolNotAllowedError,
)
from app.integrations.mcp.http_gateway import HttpMcpGateway
from app.integrations.mcp.google_mcp_service import GoogleMcpService

TENANT_ID = "00000000-0000-0000-0000-000000000001"


def _make_settings(*, url: str = "http://mock-mcp-server", timeout: float = 5.0) -> MagicMock:
    """Retorna un objeto settings mínimo que satisface HttpMcpGateway."""
    s = MagicMock()
    s.MCP_SERVER_URL = url
    s.MCP_HTTP_TIMEOUT = timeout
    return s


def _make_gateway(**kwargs) -> HttpMcpGateway:
    return HttpMcpGateway(settings=_make_settings(**kwargs))


def _jsonrpc_ok(content_text: str = '{"data": "ok"}') -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "isError": False,
            "content": [{"type": "text", "text": content_text}],
        },
    }
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


def _jsonrpc_error(error_code: str, message: str = "error") -> MagicMock:
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "isError": True,
            "errorCode": error_code,
            "content": [{"type": "text", "text": message}],
        },
    }
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


def _patch_httpx(mock_resp: MagicMock):
    """Context manager que reemplaza httpx.AsyncClient con un mock que retorna mock_resp."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(return_value=mock_resp)
    return patch("httpx.AsyncClient", return_value=mock_client)


@pytest.mark.asyncio
async def test_successful_tool_call():
    """Tool call exitoso retorna McpToolResult con is_error=False."""
    gateway = _make_gateway()

    with _patch_httpx(_jsonrpc_ok()):
        result = await gateway.call_tool(
            "google.gmail.list_messages",
            {"query": "from:proveedor"},
            tenant_id=TENANT_ID,
        )

    assert result.is_error is False
    assert result.tool_name == "google.gmail.list_messages"


@pytest.mark.asyncio
async def test_auth_error_raises_mcp_auth_error():
    """Respuesta con errorCode=mcp_auth_required → McpToolAuthError."""
    gateway = _make_gateway()

    with _patch_httpx(_jsonrpc_error("mcp_auth_required", "auth required")):
        with pytest.raises(McpToolAuthError):
            await gateway.call_tool(
                "google.gmail.list_messages",
                {},
                tenant_id=TENANT_ID,
            )


@pytest.mark.asyncio
async def test_allowlist_blocks_unauthorized_tool():
    """GoogleMcpService bloquea herramientas fuera de la allowlist del agente."""
    s = _make_settings()
    s.ENABLE_GOOGLE_MCP_TOOLS = True

    mock_gateway = AsyncMock()
    svc = GoogleMcpService(
        gateway=mock_gateway,
        agent_name="agent_helper",
        tenant_id=TENANT_ID,
        settings=s,
    )

    with pytest.raises(McpToolNotAllowedError):
        await svc.list_gmail_messages(query="test")

    mock_gateway.call_tool.assert_not_called()


@pytest.mark.asyncio
async def test_disabled_flag_skips_gateway():
    """Con ENABLE_GOOGLE_MCP_TOOLS=False, GoogleMcpService no llama al gateway HTTP.

    list_gmail_messages extrae "messages" del dict de disabled → retorna [],
    pero lo importante es que call_tool nunca se invocó.
    """
    s = _make_settings()
    s.ENABLE_GOOGLE_MCP_TOOLS = False

    mock_gateway = AsyncMock()
    svc = GoogleMcpService(
        gateway=mock_gateway,
        agent_name="agent_supplier",
        tenant_id=TENANT_ID,
        settings=s,
    )

    result = await svc.list_gmail_messages(query="test")

    # El wrapper devuelve [] porque el dict de disabled no tiene "messages".
    assert result == []
    # El gateway real nunca fue invocado.
    mock_gateway.call_tool.assert_not_called()
