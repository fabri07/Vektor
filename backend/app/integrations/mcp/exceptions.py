"""Excepciones del subsistema MCP. Sin dependencias del proyecto."""


class McpError(Exception):
    def __init__(self, message: str, *, code: str = "unknown") -> None:
        super().__init__(message)
        self.code = code


class McpToolAuthError(McpError):
    """Reemplaza WorkspaceTokenError en todo el sistema.

    Reasons: not_connected | insufficient_scope | refresh_failed | mcp_auth_required
    """

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message, code=reason)
        self.reason = reason


class McpToolNotFoundError(McpError):
    pass


class McpToolTimeoutError(McpError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="timeout")


class McpToolUnavailableError(McpError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="unavailable")


class McpToolValidationError(McpError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="validation_error")


class McpToolNotAllowedError(McpError):
    def __init__(self, message: str) -> None:
        super().__init__(message, code="not_allowed")
