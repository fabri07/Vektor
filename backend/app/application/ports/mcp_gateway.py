"""Port abstracto para el gateway MCP. Los agentes dependen solo de este protocolo."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class McpToolResult:
    tool_name: str
    result: dict[str, Any]
    duration_ms: int
    is_error: bool = False
    error_code: str | None = None


class McpToolGateway(ABC):
    @abstractmethod
    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        tenant_id: str,
        user_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> McpToolResult: ...

    @abstractmethod
    async def list_tools(self) -> list[str]: ...

    @abstractmethod
    async def health(self) -> dict[str, Any]: ...

    @abstractmethod
    async def start_auth(
        self,
        *,
        tenant_id: str,
        user_id: str,
        scopes: list[str],
    ) -> dict[str, Any]:
        """Inicia el flujo OAuth en el MCP server.

        Contrato esperado del MCP server:
          POST /auth/start
          Headers: X-Tenant-Id, X-User-Id
          Body: { scopes: list[str] }
          Response: { auth_url: str, state: str, expires_in: int }
        """
        ...

    @abstractmethod
    async def get_auth_status(
        self,
        *,
        tenant_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        """Consulta el estado de conexión OAuth en el MCP server.

        Contrato esperado del MCP server:
          GET /auth/status
          Headers: X-Tenant-Id, X-User-Id
          Response: { connected: bool, scopes_granted: list[str], last_error: str | None }
        """
        ...

    @abstractmethod
    async def revoke_auth(
        self,
        *,
        tenant_id: str,
        user_id: str,
    ) -> None:
        """Revoca la conexión OAuth en el MCP server.

        Contrato esperado del MCP server:
          DELETE /auth/revoke
          Headers: X-Tenant-Id, X-User-Id
          Response: { ok: bool }
        """
        ...
