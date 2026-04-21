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
        idempotency_key: str | None = None,
    ) -> McpToolResult: ...

    @abstractmethod
    async def list_tools(self) -> list[str]: ...

    @abstractmethod
    async def health(self) -> dict[str, Any]: ...
