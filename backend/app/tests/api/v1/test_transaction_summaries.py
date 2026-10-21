"""Contratos comunes de `/summary` y `/date-range` de ventas y gastos.

Los dos routers exponen la misma superficie de agregación y estos cuatro
comportamientos eran pares clonados en `test_sales.py` / `test_expenses.py`
(mismo cuerpo, distinta entidad). Acá corren parametrizados; lo específico de
cada entidad (bulk de ventas, categorías de gastos, RBAC, intradía) sigue en
su archivo.
"""

from typing import Any

import pytest
from httpx import AsyncClient

from app.domain.business_time import today_ar
from app.tests._respaldo import producto_con_compra_http

_TODAY = str(today_ar())


def _sale_payload(fecha: str) -> dict[str, Any]:
    return {
        "amount": "1500.00",
        "quantity": 3,
        "transaction_date": fecha,
        "payment_method": "cash",
    }


def _expense_payload(fecha: str) -> dict[str, Any]:
    return {
        "amount": "800.00",
        "category": "OTHER",
        "expense_date": fecha,
        "description": "test gasto",
    }


async def _con_respaldo(
    client: AsyncClient, headers: dict[str, Any], base: str, body: dict[str, Any]
) -> dict[str, Any]:
    """B·5e: una venta lleva producto con compra previa; un gasto, tal cual."""
    if base != "/api/v1/sales":
        return body
    return {**body, "product_id": await producto_con_compra_http(client, headers)}


#: (base del router, factory de payload con fecha, fecha vieja del caso date-range)
_ENTIDADES = [
    pytest.param("/api/v1/sales", _sale_payload, "2024-01-15", id="sales"),
    pytest.param("/api/v1/expenses", _expense_payload, "2024-02-10", id="expenses"),
]


@pytest.mark.usefixtures("mock_score_trigger")
@pytest.mark.parametrize(("base", "payload", "fecha_vieja"), _ENTIDADES)
class TestSummaryYDateRange:
    async def test_summary_empty(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        base: str,
        payload: Any,
        fecha_vieja: str,
    ) -> None:
        resp = await client.get(f"{base}/summary", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert float(data["total_ars"]) == 0.0
        assert data["entry_count"] == 0
        assert "period_covered" in data

    async def test_date_range_empty(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        base: str,
        payload: Any,
        fecha_vieja: str,
    ) -> None:
        resp = await client.get(f"{base}/date-range", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["min_date"] is None
        assert data["max_date"] is None

    async def test_date_range_with_data(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        base: str,
        payload: Any,
        fecha_vieja: str,
    ) -> None:
        for fecha in (fecha_vieja, _TODAY):
            resp = await client.post(
                base,
                json=await _con_respaldo(client, auth_headers, base, payload(fecha)),
                headers=auth_headers,
            )
            assert resp.status_code == 201, resp.text
        resp = await client.get(f"{base}/date-range", headers=auth_headers)
        data = resp.json()
        assert data["min_date"] == fecha_vieja
        assert data["max_date"] == _TODAY

    async def test_summary_isolates_tenants(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        second_auth_headers: dict[str, Any],
        base: str,
        payload: Any,
        fecha_vieja: str,
    ) -> None:
        resp = await client.post(
            base,
            json=await _con_respaldo(client, auth_headers, base, payload(_TODAY)),
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.text

        resp_b = await client.get(f"{base}/summary", headers=second_auth_headers)
        assert resp_b.json()["entry_count"] == 0
