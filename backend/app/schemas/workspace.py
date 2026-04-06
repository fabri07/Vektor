"""Schemas Pydantic para el flujo de Workspace Connect."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class WorkspaceConnectStartResponse(BaseModel):
    """Respuesta de POST /workspace/google/connect/start."""
    authorization_url: str


class WorkspaceConnectExchangeRequest(BaseModel):
    """Request de POST /workspace/google/connect/exchange."""
    exchange_session_id: str


class WorkspaceStatusResponse(BaseModel):
    """Respuesta de GET /workspace/google/status."""
    connected: bool
    google_account_email: str | None = None
    scopes_granted: list[str] = []
    connected_at: datetime | None = None
    last_error_code: str | None = None


class WorkspaceDisconnectResponse(BaseModel):
    """Respuesta de DELETE /workspace/google/disconnect."""
    disconnected: bool = True
