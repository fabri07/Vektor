"""B·3 (paso 3 del cierre del #48) — la categoría inferida es SÓLO sugerencia.

Decisión del dueño (2026-10-07): escribir la inferida en ``products.category``
hacía que un catálogo posterior —que completa sólo vacíos— no pudiera
corregirla. Ahora:

- el producto nacido de una compra nace SIN categoría (igual que en main);
- el listado y el detalle devuelven ``category_suggestion`` calculada al leer,
  sólo con categoría vacía y confianza ``high``; si no, ``null``;
- aceptar la sugerencia es el PATCH de siempre con ``category=<code>``.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.ingestion_import_service import build_incomplete_product
from app.domain.verticals import Vertical
from app.persistence.models.business import BusinessProfile
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant


@pytest.fixture(autouse=True)
async def _rubro_deco(
    db_session: AsyncSession, sample_business_profile: BusinessProfile, mock_score_trigger: Any
) -> None:
    # Sólo decoración tiene vocabulario de inferencia hoy.
    sample_business_profile.vertical_code = Vertical.DECORACION_HOGAR.value
    await db_session.commit()


async def _crear(client: AsyncClient, headers: dict[str, Any], **campos: Any) -> dict[str, Any]:
    body = {"sale_price_ars": "100.00", "unit_cost_ars": "50.00", "stock_units": 0, **campos}
    resp = await client.post("/api/v1/products", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    data: dict[str, Any] = resp.json()
    return data


def _por_id(listado: list[dict[str, Any]], product_id: str) -> dict[str, Any]:
    return next(p for p in listado if p["id"] == product_id)


async def test_producto_desde_compra_nace_sin_categoria(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Aunque el nombre sea inequívoco, la compra no escribe la categoría."""
    new_id, created = await build_incomplete_product(
        db_session, sample_tenant.tenant_id, "Lampara de pie", None, None
    )
    await db_session.commit()
    assert created and new_id is not None
    product = await db_session.get(Product, new_id)
    assert product is not None
    assert product.category is None
    assert product.custom_fields in (None, {})


async def test_listado_sugiere_con_nombre_claro_y_null_si_ambiguo_o_con_categoria(
    client: AsyncClient, auth_headers: dict[str, Any]
) -> None:
    claro = await _crear(client, auth_headers, name="Lampara de pie")
    ambiguo = await _crear(client, auth_headers, name="Lampara mesa")  # ILUMINACION y MUEBLES
    con_categoria = await _crear(client, auth_headers, name="Velador", category="MUEBLES")

    resp = await client.get("/api/v1/products", headers=auth_headers)
    assert resp.status_code == 200
    listado = resp.json()

    assert _por_id(listado, claro["id"])["category_suggestion"] == {
        "code": "ILUMINACION",
        "label": "Iluminación",
    }
    assert _por_id(listado, ambiguo["id"])["category_suggestion"] is None
    assert _por_id(listado, con_categoria["id"])["category"] == "MUEBLES"
    assert _por_id(listado, con_categoria["id"])["category_suggestion"] is None

    # El detalle devuelve lo mismo, y la sugerencia no se persistió.
    detalle = await client.get(f"/api/v1/products/{claro['id']}", headers=auth_headers)
    assert detalle.json()["category"] is None
    assert detalle.json()["category_suggestion"]["code"] == "ILUMINACION"


async def test_aceptar_la_sugerencia_fija_la_categoria(
    client: AsyncClient, auth_headers: dict[str, Any]
) -> None:
    claro = await _crear(client, auth_headers, name="Lampara de pie")
    sugerida = (await client.get(f"/api/v1/products/{claro['id']}", headers=auth_headers)).json()[
        "category_suggestion"
    ]

    patch = await client.patch(
        f"/api/v1/products/{claro['id']}",
        json={"category": sugerida["code"]},
        headers=auth_headers,
    )
    assert patch.status_code == 200, patch.text
    assert patch.json()["category"] == "ILUMINACION"

    detalle = await client.get(f"/api/v1/products/{claro['id']}", headers=auth_headers)
    assert detalle.json()["category"] == "ILUMINACION"
    assert detalle.json()["category_suggestion"] is None
