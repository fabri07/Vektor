"""Tests for /api/v1/purchases/manual — compra de mercadería transaccional."""

import unittest.mock
from datetime import date
from typing import Any

import pytest
from httpx import AsyncClient

_TODAY = str(date.today())


async def _create_supplier(client: AsyncClient, headers: dict[str, Any], name: str) -> str:
    resp = await client.post("/api/v1/suppliers", json={"name": name}, headers=headers)
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _create_product(
    client: AsyncClient, headers: dict[str, Any], name: str, *, stock: int, price: str = "100.00"
) -> str:
    resp = await client.post(
        "/api/v1/products",
        json={"name": name, "sale_price_ars": price, "stock_units": stock},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


@pytest.mark.asyncio
class TestManualPurchase:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
            yield

    async def test_new_product_creates_stock_and_cogs(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        sup = await _create_supplier(client, auth_headers, "Distribuidora Norte")
        payload = {
            "supplier_id": sup,
            "payment_method": "cash",
            "transaction_date": _TODAY,
            "lines": [
                {
                    "name": "Yerba Nueva",
                    "category": "Bebidas",
                    "unit_cost": "1000.00",
                    "quantity": 12,
                    "sale_price_ars": "1500.00",
                }
            ],
        }
        resp = await client.post(
            "/api/v1/purchases/manual", json=payload, headers=auth_headers
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert data["lines"] == 1
        assert len(data["products_created"]) == 1
        assert len(data["expense_ids"]) == 1
        assert data["total_cogs"] == 12000.0
        r = data["results"][0]
        assert r["created"] is True
        assert r["new_stock_units"] == 12
        assert r["margin_pct"] == pytest.approx(33.3, abs=0.2)
        # el gasto es COGS/INVENTORY
        exp = await client.get(
            f"/api/v1/expenses/{data['expense_ids'][0]}", headers=auth_headers
        )
        assert exp.json()["expense_type"] == "COGS"
        assert exp.json()["category"] == "INVENTORY"

    async def test_existing_product_restock_without_price_update(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        pid = await _create_product(client, auth_headers, "Fideos", stock=5, price="500.00")
        sup = await _create_supplier(client, auth_headers, "Proveedor X")
        payload = {
            "supplier_id": sup,
            "payment_method": "transfer",
            "transaction_date": _TODAY,
            "lines": [
                {
                    "product_id": pid,
                    "unit_cost": "300.00",
                    "quantity": 10,
                    "sale_price_ars": "999.00",
                    "update_price": False,
                }
            ],
        }
        resp = await client.post(
            "/api/v1/purchases/manual", json=payload, headers=auth_headers
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["results"][0]["new_stock_units"] == 15
        # sin update_price → el precio de venta NO cambia
        prod = await client.get(f"/api/v1/products/{pid}", headers=auth_headers)
        assert float(prod.json()["sale_price_ars"]) == 500.0

    async def test_duplicate_existing_product_rejected(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        pid = await _create_product(client, auth_headers, "Galleta", stock=2, price="50.00")
        sup = await _create_supplier(client, auth_headers, "Prov")
        payload = {
            "supplier_id": sup,
            "payment_method": "cash",
            "transaction_date": _TODAY,
            "lines": [
                {"product_id": pid, "unit_cost": "10.00", "quantity": 1, "sale_price_ars": "20.00"},
                {"product_id": pid, "unit_cost": "10.00", "quantity": 3, "sale_price_ars": "20.00"},
            ],
        }
        resp = await client.post(
            "/api/v1/purchases/manual", json=payload, headers=auth_headers
        )
        assert resp.status_code == 400
        prod = await client.get(f"/api/v1/products/{pid}", headers=auth_headers)
        assert prod.json()["stock_units"] == 2  # sin cambios

    async def test_unknown_supplier_rejected(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        payload = {
            "supplier_id": "00000000-0000-0000-0000-000000000000",
            "payment_method": "cash",
            "transaction_date": _TODAY,
            "lines": [
                {
                    "name": "X",
                    "category": "Bebidas",
                    "unit_cost": "10.00",
                    "quantity": 1,
                    "sale_price_ars": "20.00",
                }
            ],
        }
        resp = await client.post(
            "/api/v1/purchases/manual", json=payload, headers=auth_headers
        )
        assert resp.status_code == 400
