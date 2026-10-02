"""Productos por peso o volumen de punta a punta (cierre de B3).

Contrato: el stock y las cantidades de VENTA viven en unidades base (g, ml); el
precio y el costo son por unidad de venta (kg, L). Lo que escribe una persona en
un documento —remito, compra, archivo— habla en la unidad de venta ("5" de queso
son 5 kg). Cada test mira un importe EXACTO: un factor olvidado da un número
1000 veces más grande o más chico, no uno "parecido".
"""

from __future__ import annotations

import unittest.mock
import uuid
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.shared.weight_guard import nombre_si_es_por_peso
from app.domain.business_time import today_ar
from app.domain.sale_unit import a_unidades_base, importe, validar_unidad
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant

_FECHA = f"{today_ar().isoformat()}T10:00:00"


@pytest.fixture(autouse=True)
def _sin_celery(mock_score_trigger):
    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        yield


async def _queso(
    client: AsyncClient, headers: dict[str, str], *, stock_g: int = 0, **extra: Any
) -> dict[str, Any]:
    resp = await client.post(
        "/api/v1/products",
        json={
            "name": f"Queso {uuid.uuid4().hex[:4]}",
            "sale_price_ars": "10000.00",
            "unit_cost_ars": "800.00",
            "stock_units": stock_g,
            "sale_unit": "gram",
            "base_units_per_sale_unit": 1000,
            **extra,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


class TestDominio:
    def test_importe_divide_por_el_factor(self) -> None:
        assert importe(Decimal("800"), 5000, 1000) == Decimal("4000.00")
        assert importe(Decimal("1234.56"), 750, 1000) == Decimal("925.92")
        assert importe(Decimal("150.50"), 3, 1) == Decimal("451.50")

    def test_a_unidades_base(self) -> None:
        assert a_unidades_base(5, 1000) == 5000
        assert a_unidades_base(5, 1) == 5

    @pytest.mark.parametrize(
        ("unidad", "factor"), [("unit", 1000), ("gram", 500), ("liter", 1), ("milliliter", 10)]
    )
    def test_solo_combinaciones_con_nombre(self, unidad: str, factor: int) -> None:
        with pytest.raises(ValueError):
            validar_unidad(unidad, factor)


class TestAltaYEdicion:
    async def test_alta_por_kilo_con_umbral_de_5_kg(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        p = await _queso(client, auth_headers, stock_g=2000)
        assert p["sale_unit"] == "gram"
        assert p["base_units_per_sale_unit"] == 1000
        assert p["stock_display"] == "2,000 kg"
        # 5 de la unidad de venta, no 5 gramos (que nunca alertaría).
        assert p["low_stock_threshold_units"] == 5000
        assert p["is_low_stock"] is True

    async def test_combinacion_sin_nombre_es_422(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        resp = await client.post(
            "/api/v1/products",
            json={"name": "Raro", "sale_price_ars": "1.00", "sale_unit": "unit",
                  "base_units_per_sale_unit": 1000},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    async def test_unidad_y_factor_van_juntos_al_editar(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        p = await _queso(client, auth_headers)
        resp = await client.patch(
            f"/api/v1/products/{p['id']}", json={"sale_unit": "unit"}, headers=auth_headers
        )
        assert resp.status_code == 422

    async def test_sin_historia_se_puede_cambiar_la_unidad(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        p = await _queso(client, auth_headers)
        resp = await client.patch(
            f"/api/v1/products/{p['id']}",
            json={"sale_unit": "unit", "base_units_per_sale_unit": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["sale_unit"] == "unit"

    async def test_con_stock_no_se_puede_cambiar_la_unidad(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        p = await _queso(client, auth_headers, stock_g=1500)
        resp = await client.patch(
            f"/api/v1/products/{p['id']}",
            json={"sale_unit": "unit", "base_units_per_sale_unit": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "SALE_UNIT_LOCKED"


class TestCompras:
    async def _proveedor(self, client: AsyncClient, headers: dict[str, str]) -> str:
        resp = await client.post(
            "/api/v1/suppliers", json={"name": f"Lácteos {uuid.uuid4().hex[:4]}"},
            headers=headers,
        )
        return str(resp.json()["id"])

    async def test_compra_manual_de_5_kg(
        self, client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
    ) -> None:
        p = await _queso(client, auth_headers)
        sid = await self._proveedor(client, auth_headers)
        resp = await client.post(
            "/api/v1/purchases/manual",
            json={
                "supplier_id": sid,
                "payment_method": "cash",
                "transaction_date": _FECHA,
                "lines": [{"product_id": p["id"], "unit_cost": "800.00", "quantity": 5,
                           "sale_price_ars": "10000.00"}],
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["results"][0]["new_stock_units"] == 5000
        assert resp.json()["total_cogs"] == 4000.0

    async def test_remito_de_5_kg(
        self, client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
    ) -> None:
        p = await _queso(client, auth_headers)
        sid = await self._proveedor(client, auth_headers)
        resp = await client.post(
            f"/api/v1/suppliers/{sid}/receipts",
            json={"currency": "ARS",
                  "lines": [{"product_name": p["name"], "qty": 5, "unit_price": "800.00"}]},
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["total_cogs_ars"] == "4000.00"
        producto = await db_session.get(Product, uuid.UUID(p["id"]))
        assert producto is not None
        await db_session.refresh(producto)
        assert producto.stock_units == 5000
        assert producto.unit_cost_ars == Decimal("800.00")

        # La ficha del proveedor: 5000 g a $800/kg son $4.000, con la unidad a la vista.
        filas = await client.get(f"/api/v1/suppliers/{sid}/products", headers=auth_headers)
        assert filas.status_code == 200, filas.text
        cuerpo = filas.json()
        productos = cuerpo if isinstance(cuerpo, list) else [
            pr for grupo in cuerpo.get("groups", []) for pr in grupo.get("products", [])
        ]
        [fila] = [f for f in productos if f["product_id"] == p["id"]]
        assert fila["quantity_display"] == "5,000 kg"


class TestVentasYValuacion:
    async def test_venta_manual_por_gramos(
        self, client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession
    ) -> None:
        p = await _queso(client, auth_headers, stock_g=2000, sale_price_ars="1234.56")
        resp = await client.post(
            "/api/v1/sales/manual-batch",
            json={"payment_method": "cash", "transaction_date": _FECHA,
                  "items": [{"product_id": p["id"], "quantity": 750,
                             "unit_price": "1234.56"}]},
            headers=auth_headers,
        )
        assert resp.status_code in (200, 201), resp.text
        montos = [Decimal(str(s["amount"])) for s in resp.json()["sales"]]
        assert montos == [Decimal("925.92")]

    async def test_valor_del_stock_en_el_desglose(
        self, client: AsyncClient, auth_headers: dict[str, str]
    ) -> None:
        await _queso(client, auth_headers, stock_g=5000)
        resp = await client.get("/api/v1/insights/breakdown?days=30", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["stock_value_total"] == pytest.approx(4000.0)


class TestChat:
    async def test_el_chat_no_mueve_un_producto_por_peso(
        self, client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        p = await _queso(client, auth_headers)
        nombre = await nombre_si_es_por_peso(db_session, str(sample_tenant.tenant_id), p["id"])
        assert nombre == p["name"]

    async def test_un_producto_por_unidad_si(
        self, client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
        sample_tenant: Tenant,
    ) -> None:
        resp = await client.post(
            "/api/v1/products", json={"name": "Yerba", "sale_price_ars": "100.00"},
            headers=auth_headers,
        )
        pid = resp.json()["id"]
        assert await nombre_si_es_por_peso(db_session, str(sample_tenant.tenant_id), pid) is None
