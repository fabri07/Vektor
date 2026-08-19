"""Tests del endpoint de diagnóstico de salud del worker/cola de relectura."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import reread_diagnostics_service as diag
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.utils.security import create_access_token, hash_password

_HEALTH = "/api/v1/admin/ingestion/reread-health"


async def _superadmin_headers(db: AsyncSession, tenant: Tenant) -> dict[str, str]:
    user = User(
        user_id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        email="super-reread@vektor.app",
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
    resp = await client.get(_HEALTH, headers=auth_headers)
    assert resp.status_code == 403


async def test_reports_unhealthy_when_no_workers(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(diag, "_inspect_celery", lambda _timeout: ({}, {}, None))
    headers = await _superadmin_headers(db_session, sample_tenant)

    resp = await client.get(_HEALTH, headers=headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["overall_ok"] is False
    by_name = {c["check"]: c for c in data["checks"]}
    assert by_name["workers_responding"]["ok"] is False


async def test_reports_healthy_when_worker_listens_ingestion(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        diag,
        "_inspect_celery",
        lambda _timeout: (
            {"worker1@host": "pong"},
            {"worker1@host": [{"name": "ingestion"}]},
            None,
        ),
    )
    headers = await _superadmin_headers(db_session, sample_tenant)

    resp = await client.get(_HEALTH, headers=headers)

    assert resp.status_code == 200
    data = resp.json()
    assert data["overall_ok"] is True
