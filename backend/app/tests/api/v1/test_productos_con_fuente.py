"""`GET /products?con_fuente=true` trae el primer día con una fuente con costo de cada
producto (lo que «Otros» usa para ordenar y marcar los que respaldan una venta)."""

from __future__ import annotations

from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.tests._respaldo import producto_con_compra

pytestmark = pytest.mark.asyncio


async def test_con_fuente_trae_el_primer_dia_y_sin_el_parametro_no(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    con = await producto_con_compra(db_session, tid, nombre="Con fuente", dia=date(2026, 10, 3))
    sin = Product(tenant_id=tid, name="Sin fuente", sale_price_ars=100, stock_units=0)
    db_session.add(sin)
    await db_session.commit()

    r = await client.get("/api/v1/products", params={"con_fuente": True}, headers=auth_headers)
    assert r.status_code == 200, r.text
    por_id = {p["id"]: p for p in r.json()}
    assert por_id[str(con.id)]["primer_dia_de_fuente"] == "2026-10-03"
    assert por_id[str(sin.id)]["primer_dia_de_fuente"] is None

    r = await client.get("/api/v1/products", headers=auth_headers)
    assert all(p["primer_dia_de_fuente"] is None for p in r.json())
