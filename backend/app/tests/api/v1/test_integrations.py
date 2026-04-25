"""Tests para los endpoints /integrations/google/*.

Usa SQLite in-memory + overrides de dependencias. Los métodos del MCP gateway
se mockean para no requerir infraestructura externa.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.google_mcp_connection import GoogleMcpConnection
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User

# ── Tests: GET /integrations/google/status ────────────────────────────────────


@pytest.mark.asyncio
async def test_status_returns_disconnected_when_no_record(
    client: AsyncClient,
    auth_headers: dict,
):
    """Sin registro en DB → estado DISCONNECTED, connected=False."""
    resp = await client.get("/api/v1/integrations/google/status", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["connection_state"] == "DISCONNECTED"
    assert data["connected"] is False
    assert data["scopes_granted"] == []


@pytest.mark.asyncio
async def test_status_returns_real_state_from_db(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
):
    """Con registro CONNECTED en DB → devuelve connected=True."""
    conn = GoogleMcpConnection(
        id=uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        user_id=sample_user.user_id,
        status="CONNECTED",
        scopes_granted=["gmail.readonly"],
    )
    db_session.add(conn)
    await db_session.commit()

    resp = await client.get("/api/v1/integrations/google/status", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["connection_state"] == "CONNECTED"
    assert data["connected"] is True
    assert "gmail.readonly" in data["scopes_granted"]


@pytest.mark.asyncio
async def test_status_promotes_mcp_callback_error_to_connection_error(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
):
    """Si el MCP informa fallo del callback, el backend deja de quedar en CONNECTING."""
    conn = GoogleMcpConnection(
        id=uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        user_id=sample_user.user_id,
        status="CONNECTING",
        state_token="abc123",
        scopes_granted=[],
    )
    db_session.add(conn)
    await db_session.commit()

    mock_gateway = AsyncMock()
    mock_gateway.get_auth_status.return_value = {
        "connected": False,
        "scopes_granted": [],
        "last_error": "token_exchange_failed:redirect_uri_mismatch",
    }

    with (
        patch("app.api.v1.integrations.get_settings", return_value=type("S", (), {
            "ENABLE_GOOGLE_MCP_TOOLS": True,
            "MCP_SERVER_URL": "http://mcp-server:8080",
        })()),
        patch(
            "app.integrations.mcp.http_gateway.HttpMcpGateway",
            return_value=mock_gateway,
        ),
    ):
        resp = await client.get("/api/v1/integrations/google/status", headers=auth_headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["connection_state"] == "ERROR"
    assert data["last_error_code"] == "token_exchange_failed:redirect_uri_mismatch"

    await db_session.refresh(conn)
    assert conn.status == "ERROR"
    assert conn.state_token is None


@pytest.mark.asyncio
async def test_status_expires_stale_connecting_connection(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
):
    """Un flujo OAuth abandonado no queda bloqueado indefinidamente en CONNECTING."""
    conn = GoogleMcpConnection(
        id=uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        user_id=sample_user.user_id,
        status="CONNECTING",
        state_token="stale-state",
        scopes_granted=[],
        updated_at=datetime.now(UTC) - timedelta(minutes=15),
    )
    db_session.add(conn)
    await db_session.commit()

    with patch("app.api.v1.integrations.get_settings", return_value=type("S", (), {
        "ENABLE_GOOGLE_MCP_TOOLS": True,
        "MCP_SERVER_URL": "http://mcp-server:8080",
    })()):
        resp = await client.get("/api/v1/integrations/google/status", headers=auth_headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["connection_state"] == "ERROR"
    assert data["last_error_code"] == "oauth_callback_timeout"

    await db_session.refresh(conn)
    assert conn.status == "ERROR"
    assert conn.state_token is None


# ── Tests: POST /integrations/google/connect/start ────────────────────────────


@pytest.mark.asyncio
async def test_connect_start_unavailable_when_mcp_disabled(
    client: AsyncClient,
    auth_headers: dict,
):
    """Cuando ENABLE_GOOGLE_MCP_TOOLS=False → 503."""
    with patch(
        "app.api.v1.integrations.get_settings",
        return_value=type("S", (), {
            "ENABLE_GOOGLE_MCP_TOOLS": False,
            "MCP_SERVER_URL": "",
        })(),
    ):
        resp = await client.post(
            "/api/v1/integrations/google/connect/start", headers=auth_headers
        )
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_connect_start_creates_connection_record(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
):
    """Con MCP habilitado → crea GoogleMcpConnection(status=CONNECTING)."""
    mock_gateway = AsyncMock()
    mock_gateway.start_auth.return_value = {
        "auth_url": "https://accounts.google.com/o/oauth2/auth?test=1",
        "state": "abc123",
        "expires_in": 300,
    }

    with (
        patch("app.api.v1.integrations.get_settings", return_value=type("S", (), {
            "ENABLE_GOOGLE_MCP_TOOLS": True,
            "MCP_SERVER_URL": "http://mcp-server:8080",
        })()),
        # El import de HttpMcpGateway es lazy (dentro del cuerpo de la función),
        # por eso patcheamos en el módulo de origen, no en integrations.
        patch(
            "app.integrations.mcp.http_gateway.HttpMcpGateway",
            return_value=mock_gateway,
        ),
    ):
        resp = await client.post(
            "/api/v1/integrations/google/connect/start", headers=auth_headers
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["auth_url"].startswith("https://")
    assert data["state"] == "abc123"
    assert len(data["required_scopes"]) > 0
    assert "drive.readonly" in data["required_scopes"]

    # Verificar registro en DB
    row = (await db_session.execute(
        select(GoogleMcpConnection).where(
            GoogleMcpConnection.tenant_id == sample_tenant.tenant_id,
            GoogleMcpConnection.user_id == sample_user.user_id,
        )
    )).scalar_one_or_none()
    assert row is not None
    assert row.status == "CONNECTING"
    assert row.state_token == "abc123"


# ── Tests: POST /integrations/google/disconnect ───────────────────────────────


@pytest.mark.asyncio
async def test_disconnect_updates_status_to_disconnected(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
):
    """Desconectar pone el registro en DISCONNECTED y limpia scopes."""
    conn = GoogleMcpConnection(
        id=uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        user_id=sample_user.user_id,
        status="CONNECTED",
        scopes_granted=["gmail.readonly", "calendar.events"],
    )
    db_session.add(conn)
    await db_session.commit()

    with patch("app.api.v1.integrations.get_settings", return_value=type("S", (), {
        "ENABLE_GOOGLE_MCP_TOOLS": False,
        "MCP_SERVER_URL": "",
    })()):
        resp = await client.post(
            "/api/v1/integrations/google/disconnect", headers=auth_headers
        )

    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    await db_session.refresh(conn)
    assert conn.status == "DISCONNECTED"
    assert conn.scopes_granted == []


@pytest.mark.asyncio
async def test_disconnect_without_existing_record(
    client: AsyncClient,
    auth_headers: dict,
):
    """Desconectar sin registro previo → 200 (idempotente)."""
    with patch("app.api.v1.integrations.get_settings", return_value=type("S", (), {
        "ENABLE_GOOGLE_MCP_TOOLS": False,
        "MCP_SERVER_URL": "",
    })()):
        resp = await client.post(
            "/api/v1/integrations/google/disconnect", headers=auth_headers
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
