"""HttpMcpGateway — cliente HTTP hacia el MCP server.

Protocolo: JSON-RPC 2.0
Endpoint: POST {MCP_SERVER_URL}/tools/call
"""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx

from app.application.ports.mcp_gateway import McpToolGateway, McpToolResult
from app.integrations.mcp.exceptions import (
    McpError,
    McpToolAuthError,
    McpToolNotFoundError,
    McpToolTimeoutError,
    McpToolUnavailableError,
    McpToolValidationError,
)
from app.observability.logger import get_logger

logger = get_logger(__name__)

# Timeouts por herramienta (segundos). Lecturas menores, escrituras mayores.
TOOL_TIMEOUTS: dict[str, float] = {
    "google.gmail.list_messages": 8.0,
    "google.gmail.get_message": 8.0,
    "google.gmail.create_draft": 20.0,
    "google.calendar.list_events": 8.0,
    "google.calendar.create_event": 20.0,
    "google.sheets.read_range": 10.0,
    "google.sheets.append_rows": 20.0,
    "google.drive.upload_file": 30.0,
    "google.drive.list_files": 8.0,
    "google.docs.create_document": 20.0,
    "google.docs.append_content": 20.0,
    "google.photos.list_media_items": 8.0,
}
DEFAULT_TIMEOUT = 15.0

# Códigos de error del MCP server que mapean a McpToolAuthError
_AUTH_ERROR_CODES = frozenset({
    "mcp_auth_required",
    "not_connected",
    "insufficient_scope",
    "refresh_failed",
})


class HttpMcpGateway(McpToolGateway):

    def __init__(self, settings: Any) -> None:
        self._base_url = settings.MCP_SERVER_URL.rstrip("/")
        self._default_timeout = settings.MCP_HTTP_TIMEOUT

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        tenant_id: str,
        idempotency_key: str | None = None,
    ) -> McpToolResult:
        timeout = TOOL_TIMEOUTS.get(tool_name, self._default_timeout)
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "tools/call",
            "params": {
                "name": tool_name,
                "arguments": arguments,
            },
        }
        if idempotency_key:
            payload["params"]["idempotency_key"] = idempotency_key

        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self._base_url}/tools/call",
                    json=payload,
                    timeout=timeout,
                    headers={"X-Tenant-Id": tenant_id},
                )
        except httpx.TimeoutException as exc:
            duration_ms = int((time.monotonic() - t0) * 1000)
            logger.warning(
                "mcp_gateway.timeout",
                tool_name=tool_name,
                duration_ms=duration_ms,
                tenant_id=tenant_id,
            )
            raise McpToolTimeoutError(
                f"Timeout llamando {tool_name} después de {timeout}s"
            ) from exc
        except httpx.ConnectError as exc:
            raise McpToolUnavailableError(
                f"MCP server no disponible en {self._base_url}"
            ) from exc

        duration_ms = int((time.monotonic() - t0) * 1000)

        if resp.status_code == 404:
            raise McpToolNotFoundError(
                f"Herramienta '{tool_name}' no encontrada en el MCP server",
                code="tool_not_found",
            )
        if resp.status_code >= 500:
            raise McpToolUnavailableError(
                f"MCP server retornó {resp.status_code}"
            )

        try:
            body = resp.json()
        except Exception as exc:
            raise McpToolUnavailableError(
                f"Respuesta inválida del MCP server: {resp.text[:200]}"
            ) from exc

        result = body.get("result", {})
        is_error = result.get("isError", False)
        error_code = result.get("errorCode", "unknown")

        logger.info(
            "mcp_gateway.call",
            tool_name=tool_name,
            duration_ms=duration_ms,
            is_error=is_error,
            error_code=error_code if is_error else None,
            tenant_id=tenant_id,
        )

        if is_error:
            if error_code in _AUTH_ERROR_CODES:
                raise McpToolAuthError(
                    f"Error de autenticación Google: {error_code}",
                    reason=error_code,
                )
            if error_code == "validation_error":
                raise McpToolValidationError(
                    result.get("message", f"Argumentos inválidos para {tool_name}")
                )
            raise McpError(
                result.get("message", f"Error en {tool_name}"),
                code=error_code,
            )

        return McpToolResult(
            tool_name=tool_name,
            result=result,
            duration_ms=duration_ms,
            is_error=False,
        )

    async def list_tools(self) -> list[str]:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{self._base_url}/tools/list",
                    timeout=8.0,
                )
            body = resp.json()
            return [t["name"] for t in body.get("tools", [])]
        except Exception:
            return list(TOOL_TIMEOUTS.keys())

    async def health(self) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{self._base_url}/health",
                    timeout=5.0,
                )
            return {"status": "ok", "http_status": resp.status_code}
        except Exception as exc:
            return {"status": "error", "error": str(exc)}
