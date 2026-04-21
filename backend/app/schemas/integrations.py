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
    connected: bool
    connected_at: str | None
    last_error_code: str | None
    apps: list[GoogleAppStatus]
    message: str
