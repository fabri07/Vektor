"""El 403 de `require_role` se lee en castellano y no expone el rol (R-8).

Revisión profunda 2026-10-24: `require_role` respondía
`Role 'VIEWER' is not allowed to perform this action.` y la pantalla lo mostraba
tal cual. Ahora el detalle es `ROL_SIN_PERMISO_DETAIL`, en el tono de
`require_modify_access`, y el rechazo sigue sin escribir nada.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import ROL_SIN_PERMISO_DETAIL
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.user import User
from app.tests.api.v1.test_sales import _con_producto
from app.utils.security import create_access_token


@pytest.fixture(autouse=True)
def _sin_celery(mock_score_trigger: Any) -> None:
    pass


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
) -> tuple[str, dict[str, str]]:
    rol = str(request.param)
    return rol, await _headers_de(db_session, sample_tenant, rol)


async def _ventas(db: AsyncSession, tenant: Tenant) -> int:
    return int(
        (
            await db.execute(
                select(func.count(SaleEntry.id)).where(
                    SaleEntry.tenant_id == tenant.tenant_id
                )
            )
        ).scalar_one()
    )


async def test_el_403_por_rol_dice_en_castellano_que_no_tiene_permiso(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    auth_headers: dict[str, str],
    sin_permiso: tuple[str, dict[str, str]],
) -> None:
    rol, headers = sin_permiso
    payload = await _con_producto(client, auth_headers)
    antes = await _ventas(db_session, sample_tenant)

    resp = await client.post("/api/v1/sales", json=payload, headers=headers)

    assert resp.status_code == 403, resp.text
    detail = resp.json()["detail"]
    assert detail == "No tenés permiso para hacer esto. Pedíselo al dueño de la cuenta."
    assert detail == ROL_SIN_PERMISO_DETAIL
    assert rol not in detail
    assert await _ventas(db_session, sample_tenant) == antes


async def test_con_el_rol_permitido_la_venta_se_crea(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    auth_headers: dict[str, str],
) -> None:
    payload = await _con_producto(client, auth_headers)
    antes = await _ventas(db_session, sample_tenant)

    resp = await client.post("/api/v1/sales", json=payload, headers=auth_headers)

    assert resp.status_code == 201, resp.text
    assert await _ventas(db_session, sample_tenant) == antes + 1
