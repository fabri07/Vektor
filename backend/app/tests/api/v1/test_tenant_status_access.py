"""Estado de cuenta del tenant (panel SUPERADMIN, F1): quién entra y quién escribe.

Contrato: `docs/plans/superadmin-panel.md` y `app/domain/tenant_status.py`.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.utils.security import create_access_token, create_refresh_token, hash_password

_ME = "/api/v1/auth/me"
_LOGOUT = "/api/v1/auth/logout"
_ESCRITURA = "/api/v1/auth/change-password"  # POST que no está en la lista de solo lectura
_LOGIN = "/api/v1/auth/login"
_REFRESH = "/api/v1/auth/refresh"


async def _poner_estado(db: AsyncSession, tenant: Tenant, estado: str) -> None:
    tenant.status = estado
    await db.commit()


def _codigo(respuesta) -> str | None:
    detalle = respuesta.json().get("detail")
    return detalle.get("code") if isinstance(detalle, dict) else None


# ── Peticiones con sesión ya abierta ───────────────────────────────────────────


@pytest.mark.parametrize("estado", ["ACTIVE", "TRIAL"])
async def test_cuenta_operativa_lee_y_pasa_el_filtro_de_escritura(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    auth_headers: dict[str, str],
    estado: str,
) -> None:
    await _poner_estado(db_session, sample_tenant, estado)
    assert (await client.get(_ME, headers=auth_headers)).status_code == 200
    # No debe cortarla el estado de cuenta (sí puede fallar por validación).
    r = await client.post(_ESCRITURA, headers=auth_headers, json={})
    assert _codigo(r) not in {"TENANT_SUSPENDED", "TENANT_READ_ONLY"}


async def test_suspendida_corta_una_ruta_que_solo_depende_del_usuario(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    auth_headers: dict[str, str],
) -> None:
    """`/auth/me` no inyecta `get_current_tenant`: antes de F1 seguía andando."""
    await _poner_estado(db_session, sample_tenant, "SUSPENDED")
    r = await client.get(_ME, headers=auth_headers)
    assert r.status_code == 403
    assert _codigo(r) == "TENANT_SUSPENDED"
    assert r.json()["detail"]["message"]


async def test_suspendida_puede_cerrar_sesion(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    auth_headers: dict[str, str],
) -> None:
    await _poner_estado(db_session, sample_tenant, "SUSPENDED")
    assert (await client.post(_LOGOUT, headers=auth_headers)).status_code == 200


async def test_solo_lectura_deja_leer_y_bloquea_escribir(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    auth_headers: dict[str, str],
) -> None:
    await _poner_estado(db_session, sample_tenant, "READ_ONLY")
    assert (await client.get(_ME, headers=auth_headers)).status_code == 200
    r = await client.post(_ESCRITURA, headers=auth_headers, json={})
    assert r.status_code == 403
    assert _codigo(r) == "TENANT_READ_ONLY"


async def test_solo_lectura_bloquea_el_post_sin_clasificar_aunque_no_escriba(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    auth_headers: dict[str, str],
) -> None:
    """Denegar por defecto: un POST fuera de la lista cerrada se bloquea."""
    await _poner_estado(db_session, sample_tenant, "READ_ONLY")
    r = await client.post(_ESCRITURA, headers=auth_headers, json={})
    assert _codigo(r) == "TENANT_READ_ONLY"


@pytest.mark.parametrize("estado", ["BANANA", ""])
async def test_estado_desconocido_falla_cerrado(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    auth_headers: dict[str, str],
    estado: str,
) -> None:
    await _poner_estado(db_session, sample_tenant, estado)
    r = await client.get(_ME, headers=auth_headers)
    assert r.status_code == 403
    assert _codigo(r) == "TENANT_SUSPENDED"


async def test_superadmin_no_queda_afuera_de_su_propio_tenant_suspendido(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    user = User(
        user_id=uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        email="super@vektor.app",
        full_name="Soporte",
        password_hash=hash_password("Secure789"),
        role_code="SUPERADMIN",
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await _poner_estado(db_session, sample_tenant, "SUSPENDED")
    token = create_access_token(
        {
            "sub": str(user.user_id),
            "tenant_id": str(sample_tenant.tenant_id),
            "role_code": "SUPERADMIN",
        }
    )
    assert (
        await client.get(_ME, headers={"Authorization": f"Bearer {token}"})
    ).status_code == 200


async def test_el_rol_inmune_se_lee_de_la_base_no_del_jwt(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
) -> None:
    """Un OWNER con un token que dice SUPERADMIN no esquiva la suspensión."""
    await _poner_estado(db_session, sample_tenant, "SUSPENDED")
    token = create_access_token(
        {
            "sub": str(sample_user.user_id),
            "tenant_id": str(sample_tenant.tenant_id),
            "role_code": "SUPERADMIN",
        }
    )
    r = await client.get(_ME, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
    assert _codigo(r) == "TENANT_SUSPENDED"


# ── Puertas que emiten tokens ──────────────────────────────────────────────────


async def test_login_de_cuenta_suspendida_da_403_con_codigo(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
) -> None:
    await _poner_estado(db_session, sample_tenant, "SUSPENDED")
    r = await client.post(_LOGIN, json={"email": sample_user.email, "password": "Secure123"})
    assert r.status_code == 403
    assert _codigo(r) == "TENANT_SUSPENDED"


async def test_login_con_contrasena_mala_sigue_siendo_401_aunque_este_suspendida(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
) -> None:
    """El 403 distinto sólo lo ve quien ya probó ser el usuario."""
    await _poner_estado(db_session, sample_tenant, "SUSPENDED")
    r = await client.post(_LOGIN, json={"email": sample_user.email, "password": "Mala12345"})
    assert r.status_code == 401


async def test_login_de_solo_lectura_entra(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
) -> None:
    await _poner_estado(db_session, sample_tenant, "READ_ONLY")
    r = await client.post(_LOGIN, json={"email": sample_user.email, "password": "Secure123"})
    assert r.status_code == 200
    assert "access_token" in r.json()


async def test_refresh_de_cuenta_suspendida_se_rechaza(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
) -> None:
    """Antes de F1 una cuenta suspendida seguía renovando tokens."""
    refresh = create_refresh_token(
        {
            "sub": str(sample_user.user_id),
            "tenant_id": str(sample_tenant.tenant_id),
            "role_code": "OWNER",
        }
    )
    ok = await client.post(_REFRESH, json={"refresh_token": refresh})
    assert ok.status_code == 200

    await _poner_estado(db_session, sample_tenant, "SUSPENDED")
    r = await client.post(_REFRESH, json={"refresh_token": refresh})
    assert r.status_code == 403
    assert _codigo(r) == "TENANT_SUSPENDED"


async def test_refresh_de_solo_lectura_renueva(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
) -> None:
    await _poner_estado(db_session, sample_tenant, "READ_ONLY")
    refresh = create_refresh_token(
        {
            "sub": str(sample_user.user_id),
            "tenant_id": str(sample_tenant.tenant_id),
            "role_code": "OWNER",
        }
    )
    assert (await client.post(_REFRESH, json={"refresh_token": refresh})).status_code == 200
