"""Tests de PATCH /users/me (perfil propio: nombre + teléfono, sin PIN ni rol).

Cubre:
- Actualización de phone/full_name propios con auth común (sin step-up).
- Funciona para roles no-OWNER y sin ventana de PIN abierta.
- ``role_code`` en el body se ignora (no está en el schema — sin auto-escalada).
- ``phone: null`` explícito borra el número; omitido no lo toca.
- ``PATCH /users/{id}`` sigue siendo OWNER-only (no se relajó).
"""

import uuid
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.utils.security import create_access_token, hash_password

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def viewer_headers(
    db_session: AsyncSession, sample_tenant: Tenant
) -> dict[str, Any]:
    """Sub-cuenta VIEWER sin PIN configurado ni ventana abierta."""
    user = User(
        user_id=uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        email="viewer@kiosco.com",
        full_name="Solo Lectura",
        password_hash=hash_password("Secure123"),
        role_code="VIEWER",
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    token = create_access_token(
        {
            "sub": str(user.user_id),
            "tenant_id": str(sample_tenant.tenant_id),
            "role_code": "VIEWER",
        }
    )
    return {"Authorization": f"Bearer {token}"}


async def test_get_me_exposes_phone(
    client: AsyncClient, auth_headers: dict[str, Any]
) -> None:
    resp = await client.get("/api/v1/users/me", headers=auth_headers)
    assert resp.status_code == 200
    assert "phone" in resp.json()


async def test_update_me_phone_and_name(
    client: AsyncClient, auth_headers: dict[str, Any]
) -> None:
    resp = await client.patch(
        "/api/v1/users/me",
        json={"full_name": "Nombre Nuevo", "phone": "+54 9 11 5555 1234"},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["full_name"] == "Nombre Nuevo"
    assert body["phone"] == "+54 9 11 5555 1234"


async def test_update_me_works_for_viewer_without_pin(
    client: AsyncClient, viewer_headers: dict[str, Any]
) -> None:
    resp = await client.patch(
        "/api/v1/users/me", json={"phone": "+54 9 11 4444 0000"}, headers=viewer_headers
    )
    assert resp.status_code == 200
    assert resp.json()["phone"] == "+54 9 11 4444 0000"


async def test_update_me_ignores_role_code(
    client: AsyncClient, viewer_headers: dict[str, Any]
) -> None:
    resp = await client.patch(
        "/api/v1/users/me",
        json={"full_name": "Intento Escalada", "role_code": "OWNER"},
        headers=viewer_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["role_code"] == "VIEWER"


async def test_update_me_rejects_blank_full_name(
    client: AsyncClient, auth_headers: dict[str, Any]
) -> None:
    resp = await client.patch(
        "/api/v1/users/me", json={"full_name": "   "}, headers=auth_headers
    )
    assert resp.status_code == 422


async def test_update_me_null_phone_clears(
    client: AsyncClient, auth_headers: dict[str, Any]
) -> None:
    await client.patch(
        "/api/v1/users/me", json={"phone": "+54 9 11 5555 1234"}, headers=auth_headers
    )
    resp = await client.patch(
        "/api/v1/users/me", json={"phone": None}, headers=auth_headers
    )
    assert resp.status_code == 200
    assert resp.json()["phone"] is None


async def test_update_me_omitted_phone_untouched(
    client: AsyncClient, auth_headers: dict[str, Any]
) -> None:
    await client.patch(
        "/api/v1/users/me", json={"phone": "+54 9 11 5555 1234"}, headers=auth_headers
    )
    resp = await client.patch(
        "/api/v1/users/me", json={"full_name": "Solo Nombre"}, headers=auth_headers
    )
    assert resp.status_code == 200
    assert resp.json()["phone"] == "+54 9 11 5555 1234"


async def test_patch_other_user_still_owner_only(
    client: AsyncClient, viewer_headers: dict[str, Any], sample_user: User
) -> None:
    resp = await client.patch(
        f"/api/v1/users/{sample_user.user_id}",
        json={"full_name": "Hackeado"},
        headers=viewer_headers,
    )
    assert resp.status_code == 403
