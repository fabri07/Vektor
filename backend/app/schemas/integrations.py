"""Schemas para exponer estado de integraciones externas."""

from pydantic import BaseModel


class GoogleAppStatus(BaseModel):
    id: str
    label: str
    description: str
    available: bool
    connected: bool
    needs_reconnect: bool
    required_scopes: list[str]


class GoogleIntegrationStatusResponse(BaseModel):
    provider: str = "google"
    mcp_enabled: bool
    mcp_server_configured: bool
    connection_flow_available: bool
    # DISCONNECTED | CONNECTING | CONNECTED | INSUFFICIENT_SCOPE | RECONNECT_REQUIRED | ERROR
    connection_state: str
    connected: bool
    scopes_granted: list[str]
    connected_at: str | None
    last_error_code: str | None
    apps: list[GoogleAppStatus]
    message: str


class ConnectStartResponse(BaseModel):
    auth_url: str
    required_scopes: list[str]
    state: str


# FASE 5: diagnóstico de la integración Google MCP (SUPERADMIN).


class McpDiagnosticCheck(BaseModel):
    check: str
    ok: bool
    severity: str  # "error" | "warning" | "info"
    detail: str


class McpDiagnosticTenantConnection(BaseModel):
    status: str
    last_error_code: str | None
    scopes_granted: list[str]


class GoogleMcpDiagnosticsResponse(BaseModel):
    overall_ok: bool
    checks: list[McpDiagnosticCheck]
    tenant_connection: McpDiagnosticTenantConnection | None
