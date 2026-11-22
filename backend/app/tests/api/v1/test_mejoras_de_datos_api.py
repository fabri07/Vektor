"""Endpoints de «mejoras de datos»: panel, aplicar (PIN), deshacer y disparo automático."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.tests._mejora_juguete import VALOR, MejoraJuguete, MejoraQueCambia, registro_con
from app.tests.conftest import FakeRedis

pytestmark = pytest.mark.usefixtures("mock_score_trigger")

BASE = "/api/v1/data-improvements"


@pytest.fixture(autouse=True)
def _registro() -> Iterator[None]:
    with registro_con(MejoraJuguete(), MejoraQueCambia()):
        yield


async def _producto(db: AsyncSession, tenant: Tenant, nombre: str) -> Product:
    p = Product(
        id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        name=nombre,
        sale_price_ars=Decimal("1000"),
        stock_units=1,
        provenance="REAL",
    )
    db.add(p)
    await db.flush()
    return p


async def test_panel_lista_pendientes(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    await _producto(db_session, sample_tenant, "A")
    r = await client.get(BASE, headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    ids = {p["mejora_id"]: p for p in body["pendientes"]}
    assert ids["juguete"]["cantidad"] == 1
    assert ids["juguete"]["clase"] == "completa_vacios"
    assert body["aplicadas"] == []


async def test_aplicar_mejora_desconocida_404(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    r = await client.post(f"{BASE}/no_existe/apply", json={}, headers=auth_headers)
    assert r.status_code == 404


async def test_aplicar_sin_pin_pide_pin(
    client: AsyncClient,
    sample_user: Any,
    sample_tenant: Tenant,
    db_session: AsyncSession,
) -> None:
    from app.utils.security import create_access_token  # noqa: PLC0415

    token = create_access_token(
        {
            "sub": str(sample_user.user_id),
            "tenant_id": str(sample_tenant.tenant_id),
            "role_code": "OWNER",
        }
    )
    await _producto(db_session, sample_tenant, "A")
    r = await client.post(
        f"{BASE}/juguete/apply", json={}, headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status_code == 428


async def test_aplicar_y_deshacer(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    p = await _producto(db_session, sample_tenant, "A")
    r = await client.post(f"{BASE}/juguete/apply", json={}, headers=auth_headers)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["aplicadas"] == 1 and len(out["run_ids"]) == 1
    await db_session.refresh(p)
    assert p.description == VALOR

    panel = (await client.get(BASE, headers=auth_headers)).json()
    assert [a["run_id"] for a in panel["aplicadas"]] == out["run_ids"]

    rv = await client.post(f"{BASE}/runs/{out['run_ids'][0]}/revert", headers=auth_headers)
    assert rv.status_code == 200, rv.text
    assert rv.json()["completa"] is True
    await db_session.refresh(p)
    assert p.description is None

    rr = await client.post(f"{BASE}/runs/{uuid.uuid4()}/revert", headers=auth_headers)
    assert rr.status_code == 404


async def test_auto_encola_una_vez(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    fake_redis: FakeRedis,
) -> None:
    await _producto(db_session, sample_tenant, "A")
    tarea = MagicMock()
    with patch("app.jobs.mejoras_de_datos.aplicar_mejoras_de_datos", tarea):
        r1 = await client.post(f"{BASE}/auto", headers=auth_headers)
        r2 = await client.post(f"{BASE}/auto", headers=auth_headers)
    assert r1.json() == {"queued": True}
    assert r2.json() == {"queued": False}
    tarea.delay.assert_called_once_with(str(sample_tenant.tenant_id))


async def test_auto_sin_candidatos_no_encola(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    tarea = MagicMock()
    with patch("app.jobs.mejoras_de_datos.aplicar_mejoras_de_datos", tarea):
        r = await client.post(f"{BASE}/auto", headers=auth_headers)
    assert r.json() == {"queued": False}
    tarea.delay.assert_not_called()
