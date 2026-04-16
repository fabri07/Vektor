"""Schemas Pydantic para el flujo de Workspace Connect."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class WorkspaceConnectStartResponse(BaseModel):
    """Respuesta de POST /workspace/google/connect/start."""
    authorization_url: str


class WorkspaceConnectStartRequest(BaseModel):
    """Request opcional de POST /workspace/google/connect/start."""
    app_ids: list[str] | None = None


class WorkspaceConnectExchangeRequest(BaseModel):
    """Request de POST /workspace/google/connect/exchange."""
    exchange_session_id: str


class WorkspaceAppStatus(BaseModel):
    """Estado calculado por app Google dentro de la conexión Workspace."""
    id: str
    label: str
    description: str
    available: bool
    connected: bool
    needs_reconnect: bool = False
    required_scopes: list[str] = Field(default_factory=list)


class WorkspaceStatusResponse(BaseModel):
    """Respuesta de GET /workspace/google/status."""
    connected: bool
    google_account_email: str | None = None
    scopes_granted: list[str] = Field(default_factory=list)
    apps: list[WorkspaceAppStatus] = Field(default_factory=list)
    connected_at: datetime | None = None
    last_error_code: str | None = None


class WorkspaceDisconnectResponse(BaseModel):
    """Respuesta de DELETE /workspace/google/disconnect."""
    disconnected: bool = True
