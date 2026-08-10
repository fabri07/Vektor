"""FASE 5: tests del endpoint de diagnóstico de Google MCP."""

from __future__ import annotations

import unittest.mock
import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import mcp_diagnostics_service
from app.config.settings import get_settings
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.utils.security import create_access_token, hash_password

_DIAG = "/api/v1/integrations/google/diagnostics"


async def _superadmin_headers(db: AsyncSession, tenant: Tenant) -> dict[str, str]:
    user = User(
        user_id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        email="super@vektor.app",
        full_name="Super Admin",
        password_hash=hash_password("Secure789"),
        role_code="SUPERADMIN",
        is_active=True,
    )
    db.add(user)
    await db.commit()
    token = create_access_token(
        {
            "sub": str(user.user_id),
            "tenant_id": str(tenant.tenant_id),
            "role_code": "SUPERADMIN",
        }
    )
    return {"Authorization": f"Bearer {token}"}


async def test_requires_superadmin(
    client: AsyncClient, auth_headers: dict[str, Any]
) -> None:
    # auth_headers es OWNER → 403.
    resp = await client.get(_DIAG, headers=auth_headers)
    assert resp.status_code == 403


async def test_flag_off_reports_disabled(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Forzar config apagada para no depender del .env del entorno (local apunta a prod).
    s = get_settings()
    monkeypatch.setattr(s, "ENABLE_GOOGLE_MCP_TOOLS", False)
    monkeypatch.setattr(s, "MCP_SERVER_URL", "")
    headers = await _superadmin_headers(db_session, sample_tenant)
    resp = await client.get(_DIAG, headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["overall_ok"] is False
    flag_check = next(c for c in data["checks"] if c["check"] == "flag_enabled")
    assert flag_check["ok"] is False
    assert flag_check["severity"] == "error"


async def test_all_checks_pass_when_configured(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = await _superadmin_headers(db_session, sample_tenant)
    s = get_settings()
    monkeypatch.setattr(s, "ENABLE_GOOGLE_MCP_TOOLS", True)
    monkeypatch.setattr(s, "MCP_SERVER_URL", "http://mcp.test")
    monkeypatch.setattr(s, "MCP_SERVER_SHARED_SECRET", "secret")

    with (
        unittest.mock.patch.object(
            mcp_diagnostics_service,
            "_check_reachable",
            new=AsyncMock(return_value=(True, "ok")),
        ),
        unittest.mock.patch.object(
            mcp_diagnostics_service,
            "_check_auth",
            new=AsyncMock(return_value=(True, "ok")),
        ),
    ):
        resp = await client.get(_DIAG, headers=headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["overall_ok"] is True
    # Todos los checks de severidad 'error' pasan.
    assert all(c["ok"] for c in data["checks"] if c["severity"] == "error")


async def test_empty_secret_is_error(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP_SERVER_SHARED_SECRET vacío bloquea la integración → error + overall_ok False."""
    headers = await _superadmin_headers(db_session, sample_tenant)
    s = get_settings()
    monkeypatch.setattr(s, "ENABLE_GOOGLE_MCP_TOOLS", True)
    monkeypatch.setattr(s, "MCP_SERVER_URL", "http://mcp.test")
    monkeypatch.setattr(s, "MCP_SERVER_SHARED_SECRET", "")

    with (
        unittest.mock.patch.object(
            mcp_diagnostics_service,
            "_check_reachable",
            new=AsyncMock(return_value=(True, "ok")),
        ),
        unittest.mock.patch.object(
            mcp_diagnostics_service,
            "_check_auth",
            new=AsyncMock(return_value=(True, "ok")),
        ),
    ):
        resp = await client.get(_DIAG, headers=headers)

    assert resp.status_code == 200
    data = resp.json()
    secret_check = next(c for c in data["checks"] if c["check"] == "shared_secret_configured")
    assert secret_check["ok"] is False
    assert secret_check["severity"] == "error"
    assert data["overall_ok"] is False


async def test_secret_mismatch_reported(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = await _superadmin_headers(db_session, sample_tenant)
    s = get_settings()
    monkeypatch.setattr(s, "ENABLE_GOOGLE_MCP_TOOLS", True)
    monkeypatch.setattr(s, "MCP_SERVER_URL", "http://mcp.test")
    monkeypatch.setattr(s, "MCP_SERVER_SHARED_SECRET", "secret")

    with (
        unittest.mock.patch.object(
            mcp_diagnostics_service,
            "_check_reachable",
            new=AsyncMock(return_value=(True, "ok")),
        ),
        unittest.mock.patch.object(
            mcp_diagnostics_service,
            "_check_auth",
            new=AsyncMock(return_value=(False, "401: secret no coincide")),
        ),
    ):
        resp = await client.get(_DIAG, headers=headers)

    data = resp.json()
    assert data["overall_ok"] is False
    auth_check = next(c for c in data["checks"] if c["check"] == "mcp_auth")
    assert auth_check["ok"] is False
