"""Dar de alta un campo propio exige el rol que configura el negocio (R-4).

Revisión profunda 2026-10-24: `POST /fields/definitions` no pedía rol, así que
un VIEWER (o un ANALYST) agregaba campos a las ventas, los gastos o los
productos del negocio, mientras que el PATCH, el toggle y el undo del mismo
router ya pedían `require_modify_access`. Dar de alta no es editar un dato
cargado, así que pide rol (OWNER/ADMIN) y no PIN; el 403 sale antes de escribir.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.field_definitions import (
    TenantCustomFieldDefinition,
    TenantFieldChangeLog,
)
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.utils.security import create_access_token

_RUTA = "/api/v1/fields/definitions"


def _body(field_key: str) -> dict[str, Any]:
    return {"entity_type": "product", "field_key": field_key, "label": "Talle"}


async def _headers_de(db: AsyncSession, tenant: Tenant, rol: str) -> dict[str, str]:
    user = User(
        user_id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        email=f"{rol.lower()}-{uuid.uuid4().hex[:6]}@kiosco.com",
        full_name=f"Usuario {rol}",
        password_hash="x",
        role_code=rol,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    token = create_access_token(
        {"sub": str(user.user_id), "tenant_id": str(tenant.tenant_id), "role_code": rol}
    )
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture(params=["VIEWER", "ANALYST"])
async def sin_permiso(
    request: pytest.FixtureRequest, db_session: AsyncSession, sample_tenant: Tenant
) -> dict[str, str]:
    return await _headers_de(db_session, sample_tenant, str(request.param))


@pytest_asyncio.fixture(params=["OWNER", "ADMIN"])
async def con_permiso(
    request: pytest.FixtureRequest, db_session: AsyncSession, sample_tenant: Tenant
) -> dict[str, str]:
    return await _headers_de(db_session, sample_tenant, str(request.param))


async def _definiciones(db: AsyncSession, tenant: Tenant, field_key: str) -> int:
    return int(
        (
            await db.execute(
                select(func.count(TenantCustomFieldDefinition.id)).where(
                    TenantCustomFieldDefinition.tenant_id == tenant.tenant_id,
                    TenantCustomFieldDefinition.field_key == field_key,
                )
            )
        ).scalar_one()
    )


async def _cambios(db: AsyncSession, tenant: Tenant) -> int:
    return int(
        (
            await db.execute(
                select(func.count(TenantFieldChangeLog.id)).where(
                    TenantFieldChangeLog.tenant_id == tenant.tenant_id
                )
            )
        ).scalar_one()
    )


async def test_sin_rol_no_da_de_alta_ni_se_escribe_nada(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sin_permiso: dict[str, str],
) -> None:
    cambios_antes = await _cambios(db_session, sample_tenant)
    resp = await client.post(_RUTA, headers=sin_permiso, json=_body("talle"))
    assert resp.status_code == 403, resp.text
    assert await _definiciones(db_session, sample_tenant, "talle") == 0
    assert await _cambios(db_session, sample_tenant) == cambios_antes


async def test_owner_y_admin_dan_de_alta_sin_pin(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    con_permiso: dict[str, str],
) -> None:
    # Sin ventana de PIN abierta: dar de alta no es editar un dato cargado.
    resp = await client.post(_RUTA, headers=con_permiso, json=_body("talle"))
    assert resp.status_code == 201, resp.text
    assert resp.json()["field_key"] == "talle"
    assert await _definiciones(db_session, sample_tenant, "talle") == 1
