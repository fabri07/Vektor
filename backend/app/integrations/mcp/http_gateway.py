"""HttpMcpGateway — cliente HTTP hacia el MCP server.

Protocolo: JSON-RPC 2.0
Endpoint: POST {MCP_SERVER_URL}/tools/call
"""

from __future__ import annotations

import asyncio
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
    "google.gmail.send_message": 20.0,
    "google.gmail.reply_message": 20.0,
    "google.calendar.list_events": 8.0,
    "google.calendar.create_event": 20.0,
    "google.sheets.read_range": 10.0,
    "google.sheets.append_rows": 20.0,
    "google.drive.upload_file": 30.0,
    "google.drive.list_files": 8.0,
    "google.drive.read_file": 20.0,
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

    def __init__(
        self,
        settings: Any | None = None,
        *,
        base_url: str | None = None,
        timeout: float | None = None,
        user_id: str | None = None,
    ) -> None:
        self._user_id = user_id
        self._shared_secret = ""

        if settings is not None:
            self._base_url = settings.MCP_SERVER_URL.rstrip("/")
            self._default_timeout = settings.MCP_HTTP_TIMEOUT
            self._shared_secret = getattr(settings, "MCP_SERVER_SHARED_SECRET", "")
            return

        if base_url is None:
            raise ValueError("HttpMcpGateway requiere settings o base_url.")

        self._base_url = base_url.rstrip("/")
        self._default_timeout = timeout or DEFAULT_TIMEOUT

    def _build_headers(
        self,
        *,
        tenant_id: str,
        user_id: str | None = None,
    ) -> dict[str, str]:
        effective_user_id = user_id or self._user_id
        headers: dict[str, str] = {"X-Tenant-Id": tenant_id}
        if effective_user_id:
            headers["X-User-Id"] = effective_user_id
        if self._shared_secret:
            headers["X-MCP-Server-Secret"] = self._shared_secret
        return headers

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        tenant_id: str,
        user_id: str | None = None,
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

        headers = self._build_headers(tenant_id=tenant_id, user_id=user_id)

        last_exc: BaseException | None = None
        for attempt in range(3):
            try:
                t0 = time.monotonic()
                try:
                    async with httpx.AsyncClient() as client:
                        resp = await client.post(
                            f"{self._base_url}/tools/call",
                            json=payload,
                            timeout=timeout,
                            headers=headers,
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

            except (McpToolTimeoutError, McpToolUnavailableError) as exc:
                last_exc = exc
                if attempt < 2:
                    logger.warning(
                        "mcp_gateway.retry",
                        tool_name=tool_name,
                        attempt=attempt + 1,
                        error=str(exc),
                        tenant_id=tenant_id,
                    )
                    await asyncio.sleep(2 ** attempt)

        raise last_exc  # type: ignore[misc]

    async def list_tools(self) -> list[str]:
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(
                    f"{self._base_url}/tools/list",
                    timeout=8.0,
                )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise McpToolUnavailableError("mcp_server_unreachable") from exc

        if resp.status_code in (401, 403):
            raise McpToolAuthError(
                "MCP server rechazó tools/list",
                reason="mcp_auth_required",
            )
        if resp.status_code >= 500:
            raise McpToolUnavailableError(
                f"MCP server retornó {resp.status_code} en /tools/list"
            )
        try:
            body = resp.json()
            return [t["name"] for t in body.get("tools", [])]
        except Exception as exc:
            raise McpToolUnavailableError(
                f"Respuesta inválida de /tools/list: {resp.text[:200]}"
            ) from exc

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

    async def start_auth(
        self,
        *,
        tenant_id: str,
        user_id: str,
        scopes: list[str],
    ) -> dict[str, Any]:
        last_exc: BaseException | None = None
        for attempt in range(3):
            try:
                try:
                    async with httpx.AsyncClient() as client:
                        resp = await client.post(
                            f"{self._base_url}/auth/start",
                            json={"scopes": scopes},
                            timeout=10.0,
                            headers=self._build_headers(tenant_id=tenant_id, user_id=user_id),
                        )
                except httpx.ConnectError as exc:
                    raise McpToolUnavailableError(
                        f"MCP server no disponible en {self._base_url}"
                    ) from exc
                except httpx.TimeoutException as exc:
                    raise McpToolTimeoutError("Timeout iniciando auth Google") from exc

                if resp.status_code >= 500:
                    raise McpToolUnavailableError(
                        f"MCP server retornó {resp.status_code} en /auth/start"
                    )
                if resp.status_code == 401:
                    raise McpToolAuthError(
                        "MCP server rechazó la solicitud de auth",
                        reason="mcp_auth_required",
                    )

                try:
                    return resp.json()
                except Exception as exc:
                    raise McpToolUnavailableError(
                        f"Respuesta inválida de /auth/start: {resp.text[:200]}"
                    ) from exc

            except (McpToolTimeoutError, McpToolUnavailableError) as exc:
                last_exc = exc
                if attempt < 2:
                    logger.warning(
                        "mcp_gateway.start_auth_retry",
                        attempt=attempt + 1,
                        error=str(exc),
                        tenant_id=tenant_id,
                    )
                    await asyncio.sleep(2 ** attempt)

        raise last_exc  # type: ignore[misc]

    async def get_auth_status(
        self,
        *,
        tenant_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        for attempt in range(3):
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.get(
                        f"{self._base_url}/auth/status",
                        timeout=8.0,
                        headers=self._build_headers(tenant_id=tenant_id, user_id=user_id),
                    )
            except (httpx.ConnectError, httpx.TimeoutException) as exc:
                if attempt < 2:
                    logger.warning(
                        "mcp_gateway.auth_status_retry",
                        attempt=attempt + 1,
                        error=str(exc),
                        tenant_id=tenant_id,
                    )
                    await asyncio.sleep(2 ** attempt)
                    continue
                return {"connected": False, "scopes_granted": [], "last_error": "mcp_unavailable"}

            if resp.status_code >= 400:
                return {
                    "connected": False,
                    "scopes_granted": [],
                    "last_error": f"http_{resp.status_code}",
                }

            try:
                return resp.json()
            except Exception:
                return {"connected": False, "scopes_granted": [], "last_error": "invalid_response"}

        return {"connected": False, "scopes_granted": [], "last_error": "mcp_unavailable"}

    async def revoke_auth(
        self,
        *,
        tenant_id: str,
        user_id: str,
    ) -> None:
        try:
            async with httpx.AsyncClient() as client:
                await client.request(
                    "DELETE",
                    f"{self._base_url}/auth/revoke",
                    timeout=8.0,
                    headers=self._build_headers(tenant_id=tenant_id, user_id=user_id),
                )
        except Exception as exc:
            logger.warning(
                "mcp_gateway.revoke_auth_failed",
                tenant_id=tenant_id,
                user_id=user_id,
                error=str(exc),
            )
