"""Tests de integración del step-up PIN: gating, permisos finos, borrado-con-historial."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.pin_service import PinService
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.user import User
from app.utils.security import create_access_token, hash_password

pytestmark = pytest.mark.asyncio

_CUSTOMER_PAYLOAD = {
    "name": "Cliente Gate",
    "customer_type": "person",
    "last_name": "Pérez",
    "dni": "30123456",
    "phone": "+54 11 1234-5678",
}


def _window_key(tenant_id: uuid.UUID, user_id: uuid.UUID) -> str:
    return PinService._window_key(tenant_id, user_id)


async def _create_customer(client: AsyncClient, headers: dict[str, str]) -> str:
    resp = await client.post("/api/v1/customers", json=_CUSTOMER_PAYLOAD, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest_asyncio.fixture
async def subaccount(
    db_session: AsyncSession, sample_tenant: Tenant, fake_redis
) -> dict[str, Any]:
    """Sub-cuenta con permiso de modificar + su propio PIN + ventana abierta."""
    user = User(
        user_id=uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        email="sub@kiosco.com",
        full_name="Sub Cuenta",
        password_hash=hash_password("Secure123"),
        role_code="VIEWER",
        is_active=True,
        can_modify_sensitive=True,
        pin_hash=hash_password("1234"),
    )
    db_session.add(user)
    await db_session.commit()
    await fake_redis.set(_window_key(sample_tenant.tenant_id, user.user_id), "1", ex=600)
    token = create_access_token(
        {"sub": str(user.user_id), "tenant_id": str(sample_tenant.tenant_id), "role_code": "VIEWER"}
    )
    return {"user": user, "headers": {"Authorization": f"Bearer {token}"}}


# ── Gate de PIN ──────────────────────────────────────────────────────────────


async def test_patch_without_window_returns_428(
    client: AsyncClient, auth_headers, sample_user, sample_tenant, fake_redis
) -> None:
    cid = await _create_customer(client, auth_headers)
    # Cerrar la ventana de PIN.
    await fake_redis.delete(_window_key(sample_tenant.tenant_id, sample_user.user_id))
    resp = await client.patch(
        f"/api/v1/customers/{cid}", json={"name": "Nuevo"}, headers=auth_headers
    )
    assert resp.status_code == 428
    assert resp.json()["detail"] == "PIN_REQUIRED"


async def test_patch_with_window_succeeds(
    client: AsyncClient, auth_headers
) -> None:
    cid = await _create_customer(client, auth_headers)
    resp = await client.patch(
        f"/api/v1/customers/{cid}", json={"name": "Renombrado"}, headers=auth_headers
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Renombrado"


async def test_verify_reopens_window(
    client: AsyncClient, auth_headers, sample_user, sample_tenant, fake_redis
) -> None:
    cid = await _create_customer(client, auth_headers)
    await fake_redis.delete(_window_key(sample_tenant.tenant_id, sample_user.user_id))
    # Verificar el PIN (TEST_PIN del conftest = "1234") reabre la ventana.
    verify = await client.post(
        "/api/v1/auth/pin/verify", json={"pin": "1234"}, headers=auth_headers
    )
    assert verify.status_code == 200
    resp = await client.patch(
        f"/api/v1/customers/{cid}", json={"name": "OK"}, headers=auth_headers
    )
    assert resp.status_code == 200


# ── Permisos finos: sub-cuenta ───────────────────────────────────────────────


async def test_subaccount_can_edit(
    client: AsyncClient, auth_headers, subaccount
) -> None:
    cid = await _create_customer(client, auth_headers)
    resp = await client.patch(
        f"/api/v1/customers/{cid}", json={"name": "X"}, headers=subaccount["headers"]
    )
    assert resp.status_code == 200


async def test_subaccount_without_permission_403(
    client: AsyncClient, auth_headers, db_session, sample_tenant, fake_redis
) -> None:
    cid = await _create_customer(client, auth_headers)
    user = User(
        user_id=uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        email="noperm@kiosco.com",
        full_name="Sin Permiso",
        password_hash=hash_password("Secure123"),
        role_code="VIEWER",
        is_active=True,
        can_modify_sensitive=False,
        pin_hash=hash_password("1234"),
    )
    db_session.add(user)
    await db_session.commit()
    # Ventana abierta, pero sin permiso → 403 (no 428).
    await fake_redis.set(_window_key(sample_tenant.tenant_id, user.user_id), "1", ex=600)
    token = create_access_token(
        {"sub": str(user.user_id), "tenant_id": str(sample_tenant.tenant_id), "role_code": "VIEWER"}
    )
    resp = await client.patch(
        f"/api/v1/customers/{cid}",
        json={"name": "X"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_subaccount_cannot_force_delete(
    client: AsyncClient, auth_headers, subaccount, db_session, sample_tenant
) -> None:
    cid = await _create_customer(client, auth_headers)
    # Darle historial al cliente.
    db_session.add(
        SaleEntry(
            tenant_id=sample_tenant.tenant_id,
            customer_id=uuid.UUID(cid),
            amount=Decimal("100.00"),
            transaction_date=datetime.now(UTC),
        )
    )
    await db_session.commit()
    resp = await client.delete(
        f"/api/v1/customers/{cid}?force=true", headers=subaccount["headers"]
    )
    assert resp.status_code == 403


# ── Borrado con historial ────────────────────────────────────────────────────


async def test_delete_with_history_blocks_then_owner_forces(
    client: AsyncClient, auth_headers, db_session, sample_tenant
) -> None:
    cid = await _create_customer(client, auth_headers)
    db_session.add(
        SaleEntry(
            tenant_id=sample_tenant.tenant_id,
            customer_id=uuid.UUID(cid),
            amount=Decimal("250.00"),
            transaction_date=datetime.now(UTC),
        )
    )
    await db_session.commit()
    # Sin force → 409 HAS_HISTORY.
    blocked = await client.delete(f"/api/v1/customers/{cid}", headers=auth_headers)
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "HAS_HISTORY"
    # OWNER + force → baja; aparece como inactivo con include_inactive.
    forced = await client.delete(f"/api/v1/customers/{cid}?force=true", headers=auth_headers)
    assert forced.status_code == 200
    listed = await client.get("/api/v1/customers?include_inactive=true", headers=auth_headers)
    assert cid in {c["id"] for c in listed.json()}


async def test_pin_change_is_throttled_by_lockout(
    client: AsyncClient, auth_headers
) -> None:
    # 5 intentos con current_pin incorrecto bloquean la cuenta (anti-brute-force).
    for _ in range(5):
        r = await client.post(
            "/api/v1/auth/pin/change",
            json={"current_pin": "0000", "new_pin": "5678", "new_pin_confirm": "5678"},
            headers=auth_headers,
        )
        assert r.status_code == 400
    # Incluso con el current_pin correcto, queda bloqueado por el lockout.
    locked = await client.post(
        "/api/v1/auth/pin/change",
        json={"current_pin": "1234", "new_pin": "5678", "new_pin_confirm": "5678"},
        headers=auth_headers,
    )
    assert locked.status_code == 400
    assert "intento" in locked.json()["detail"].lower()


async def test_reactivate_customer(
    client: AsyncClient, auth_headers
) -> None:
    cid = await _create_customer(client, auth_headers)
    await client.delete(f"/api/v1/customers/{cid}", headers=auth_headers)
    resp = await client.post(f"/api/v1/customers/{cid}/reactivate", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True


# ── Retiro de ganancias ──────────────────────────────────────────────────────


async def test_profit_withdrawal_creates_payroll(
    client: AsyncClient, auth_headers, mock_score_trigger
) -> None:
    resp = await client.post(
        "/api/v1/expenses/profit-withdrawal",
        json={"amount": "50000.00", "withdrawal_date": datetime.now(UTC).date().isoformat()},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["category"] == "PAYROLL"
    assert body["expense_type"] == "OPEX"


# ── Permisos de equipo ───────────────────────────────────────────────────────


async def test_team_list_and_toggle(
    client: AsyncClient, auth_headers, subaccount
) -> None:
    listed = await client.get("/api/v1/settings/team", headers=auth_headers)
    assert listed.status_code == 200
    members = {m["email"]: m for m in listed.json()}
    assert "sub@kiosco.com" in members
    sub_id = members["sub@kiosco.com"]["user_id"]
    patched = await client.patch(
        f"/api/v1/settings/team/{sub_id}",
        json={"can_modify_sensitive": False},
        headers=auth_headers,
    )
    assert patched.status_code == 200
    assert patched.json()["can_modify_sensitive"] is False
