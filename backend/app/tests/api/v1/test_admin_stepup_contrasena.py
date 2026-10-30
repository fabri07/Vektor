"""Step-up de contraseña del SUPERADMIN en las acciones de alto impacto (F7)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.utils.security import create_access_token, hash_password

_CLAVE = "Secure789"


def _codigo(r: Any) -> str | None:
    d = r.json().get("detail")
    return d.get("code") if isinstance(d, dict) else None


@pytest.fixture
async def token_sa(db_session: AsyncSession, sample_tenant: Tenant) -> dict[str, str]:
    user = User(
        user_id=uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        email="super@vektor.app",
        full_name="Soporte",
        password_hash=hash_password(_CLAVE),
        role_code="SUPERADMIN",
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    token = create_access_token(
        {
            "sub": str(user.user_id),
            "tenant_id": str(sample_tenant.tenant_id),
            "role_code": "SUPERADMIN",
        }
    )
    return {"Authorization": f"Bearer {token}"}


def _rutas(tenant: Tenant) -> list[tuple[str, dict[str, Any]]]:
    base = f"/api/v1/admin/tenants/{tenant.tenant_id}"
    return [
        (f"{base}/status", {"status": "READ_ONLY", "reason": "prueba"}),
        (f"{base}/subscription/change-plan", {"plan_code": "control"}),
        (f"{base}/subscription/cancel", {}),
        (
            f"{base}/subscription/payments",
            {
                "expected": "activate",
                "reference": "TRF-1",
                "amount_ars": "40500.00",
                "exchange_rate": "1500.00",
                "plan_code": "control",
            },
        ),
    ]


async def test_sin_contrasena_pide_428_en_cada_accion(
    client: AsyncClient, sample_tenant: Tenant, token_sa: dict[str, str]
) -> None:
    for url, cuerpo in _rutas(sample_tenant):
        r = await client.post(url, headers=token_sa, json=cuerpo)
        assert r.status_code == 428, url
        assert _codigo(r) == "ADMIN_PASSWORD_REQUIRED"


async def test_contrasena_equivocada_da_403_y_no_ejecuta(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    token_sa: dict[str, str],
) -> None:
    url, cuerpo = _rutas(sample_tenant)[0]
    r = await client.post(url, headers={**token_sa, "X-Admin-Password": "Mala12345"}, json=cuerpo)
    assert r.status_code == 403
    assert _codigo(r) == "ADMIN_PASSWORD_INVALID"
    await db_session.refresh(sample_tenant)
    assert sample_tenant.status != "READ_ONLY"


async def test_contrasena_correcta_ejecuta(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    token_sa: dict[str, str],
) -> None:
    url, cuerpo = _rutas(sample_tenant)[0]
    r = await client.post(url, headers={**token_sa, "X-Admin-Password": _CLAVE}, json=cuerpo)
    assert r.status_code == 200, r.text
    await db_session.refresh(sample_tenant)
    assert sample_tenant.status == "READ_ONLY"


async def test_cinco_fallos_bloquean_aun_con_la_contrasena_correcta(
    client: AsyncClient, sample_tenant: Tenant, token_sa: dict[str, str]
) -> None:
    url, cuerpo = _rutas(sample_tenant)[0]
    for _ in range(5):
        r = await client.post(
            url, headers={**token_sa, "X-Admin-Password": "Mala12345"}, json=cuerpo
        )
        assert r.status_code == 403
    r = await client.post(url, headers={**token_sa, "X-Admin-Password": _CLAVE}, json=cuerpo)
    assert r.status_code == 429
    assert _codigo(r) == "ADMIN_PASSWORD_LOCKED"


async def test_otro_rol_sigue_recibiendo_403_antes_de_la_contrasena(
    client: AsyncClient, sample_tenant: Tenant, auth_headers: dict[str, str]
) -> None:
    url, cuerpo = _rutas(sample_tenant)[0]
    r = await client.post(url, headers={**auth_headers, "X-Admin-Password": _CLAVE}, json=cuerpo)
    assert r.status_code == 403
    assert _codigo(r) != "ADMIN_PASSWORD_INVALID"


async def test_las_lecturas_no_piden_contrasena(
    client: AsyncClient, sample_tenant: Tenant, token_sa: dict[str, str]
) -> None:
    r = await client.get(f"/api/v1/admin/tenants/{sample_tenant.tenant_id}", headers=token_sa)
    assert r.status_code == 200, r.text
