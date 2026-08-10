"""Tests de integración para HttpMcpGateway — mock del servidor HTTP."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.integrations.mcp.exceptions import (
    McpToolAuthError,
    McpToolNotAllowedError,
    McpToolTimeoutError,
    McpToolUnavailableError,
)
from app.integrations.mcp.google_mcp_service import GoogleMcpService
from app.integrations.mcp.http_gateway import HttpMcpGateway

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


async def test_auth_error_raises_mcp_auth_error():
    """Respuesta con errorCode=mcp_auth_required → McpToolAuthError."""
    gateway = _make_gateway()

    with (
        _patch_httpx(_jsonrpc_error("mcp_auth_required", "auth required")),
        pytest.raises(McpToolAuthError),
    ):
        await gateway.call_tool(
            "google.gmail.list_messages",
            {},
            tenant_id=TENANT_ID,
        )


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


# ── Retry logic ───────────────────────────────────────────────────────────────


def _patch_httpx_raises(exc: Exception):
    """Patch httpx.AsyncClient.post para que siempre levante la excepción dada."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = AsyncMock(side_effect=exc)
    return patch("httpx.AsyncClient", return_value=mock_client)


async def test_call_tool_retries_three_times_on_timeout():
    """call_tool reintenta exactamente 3 veces en timeout y luego levanta McpToolTimeoutError."""
    gateway = _make_gateway()
    attempt_count = 0

    async def mock_post(*args, **kwargs):
        nonlocal attempt_count
        attempt_count += 1
        raise httpx.TimeoutException("simulated timeout")

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.post = mock_post

    with (
        patch("httpx.AsyncClient", return_value=mock_client),
        patch("asyncio.sleep", new_callable=AsyncMock),
        pytest.raises(McpToolTimeoutError),
    ):
        await gateway.call_tool("google.gmail.list_messages", {}, tenant_id=TENANT_ID)

    assert attempt_count == 3, f"Esperaba 3 intentos, obtuvo {attempt_count}"


async def test_call_tool_does_not_retry_auth_error():
    """Auth errors no se reintentan — asyncio.sleep no debe llamarse."""
    gateway = _make_gateway()

    with (
        _patch_httpx(_jsonrpc_error("mcp_auth_required")),
        patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
        pytest.raises(McpToolAuthError),
    ):
        await gateway.call_tool("google.gmail.list_messages", {}, tenant_id=TENANT_ID)

    mock_sleep.assert_not_called()


async def test_list_tools_raises_unavailable_on_connect_error():
    """list_tools levanta McpToolUnavailableError cuando el server no responde."""
    gateway = _make_gateway()

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

    with (
        patch("httpx.AsyncClient", return_value=mock_client),
        pytest.raises(McpToolUnavailableError),
    ):
        await gateway.list_tools()


async def test_list_tools_raises_auth_error_on_401():
    """list_tools levanta McpToolAuthError cuando el server devuelve 401."""
    gateway = _make_gateway()

    mock_resp = MagicMock()
    mock_resp.status_code = 401

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_resp)

    with patch("httpx.AsyncClient", return_value=mock_client), pytest.raises(McpToolAuthError):
        await gateway.list_tools()


async def test_get_auth_status_retries_before_unavailable():
    """get_auth_status reintenta errores de red antes de devolver mcp_unavailable."""
    gateway = _make_gateway()
    attempt_count = 0

    async def mock_get(*args, **kwargs):
        nonlocal attempt_count
        attempt_count += 1
        raise httpx.ConnectError("connection refused")

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = mock_get

    with (
        patch("httpx.AsyncClient", return_value=mock_client),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        result = await gateway.get_auth_status(tenant_id=TENANT_ID, user_id="user-1")

    assert attempt_count == 3
    assert result["connected"] is False
    assert result["last_error"] == "mcp_unavailable"
