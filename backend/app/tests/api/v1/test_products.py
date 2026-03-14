"""Tests for /api/v1/products endpoints."""

import pytest
from httpx import AsyncClient

_PRODUCT_PAYLOAD = {
    "name": "Coca-Cola 500ml",
    "category": "bebidas",
    "unit_cost_ars": "80.00",
    "sale_price_ars": "150.00",
    "stock_units": 50,
    "low_stock_threshold_units": 10,
}


@pytest.mark.asyncio
class TestProductsCRUD:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_create_product(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        resp = await client.post("/api/v1/products", json=_PRODUCT_PAYLOAD, headers=auth_headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Coca-Cola 500ml"
        assert data["sale_price_ars"] == "150.00"
        assert data["unit_cost_ars"] == "80.00"
        assert data["stock_units"] == 50
        assert data["is_active"] is True
        # Computed fields
        assert "margin_pct" in data
        assert abs(data["margin_pct"] - 46.67) < 0.1
        assert data["is_low_stock"] is False

    async def test_list_products(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        await client.post("/api/v1/products", json=_PRODUCT_PAYLOAD, headers=auth_headers)
        resp = await client.get("/api/v1/products", headers=auth_headers)
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

    async def test_list_products_filter_active(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        create_resp = await client.post(
            "/api/v1/products", json=_PRODUCT_PAYLOAD, headers=auth_headers
        )
        product_id = create_resp.json()["id"]
        # Soft-delete it
        await client.delete(f"/api/v1/products/{product_id}", headers=auth_headers)

        active = await client.get(
            "/api/v1/products", params={"is_active": "true"}, headers=auth_headers
        )
        inactive = await client.get(
            "/api/v1/products", params={"is_active": "false"}, headers=auth_headers
        )
        active_ids = [p["id"] for p in active.json()]
        inactive_ids = [p["id"] for p in inactive.json()]
        assert product_id not in active_ids
        assert product_id in inactive_ids

    async def test_get_product(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        create_resp = await client.post(
            "/api/v1/products", json=_PRODUCT_PAYLOAD, headers=auth_headers
        )
        product_id = create_resp.json()["id"]
        resp = await client.get(f"/api/v1/products/{product_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["id"] == product_id

    async def test_patch_product(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        create_resp = await client.post(
            "/api/v1/products", json=_PRODUCT_PAYLOAD, headers=auth_headers
        )
        product_id = create_resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/products/{product_id}",
            json={"stock_units": 5},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["stock_units"] == 5
        # stock=5 <= threshold=10 → is_low_stock=True
        assert data["is_low_stock"] is True

    async def test_soft_delete_product(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        create_resp = await client.post(
            "/api/v1/products", json=_PRODUCT_PAYLOAD, headers=auth_headers
        )
        product_id = create_resp.json()["id"]
        resp = await client.delete(f"/api/v1/products/{product_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert "deactivated" in resp.json()["message"].lower()

        # Product still exists but is inactive
        get_resp = await client.get(f"/api/v1/products/{product_id}", headers=auth_headers)
        assert get_resp.status_code == 200
        assert get_resp.json()["is_active"] is False

    async def test_margin_pct_none_when_no_cost(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        payload = {**_PRODUCT_PAYLOAD, "unit_cost_ars": None}
        resp = await client.post("/api/v1/products", json=payload, headers=auth_headers)
        assert resp.status_code == 201
        assert resp.json()["margin_pct"] is None


@pytest.mark.asyncio
class TestProductsTenantIsolation:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_cannot_read_other_tenant_product(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_auth_headers: dict,
    ) -> None:
        # Tenant A creates a product
        create_resp = await client.post(
            "/api/v1/products", json=_PRODUCT_PAYLOAD, headers=auth_headers
        )
        product_id = create_resp.json()["id"]

        # Tenant B tries to read it
        resp = await client.get(
            f"/api/v1/products/{product_id}", headers=second_auth_headers
        )
        assert resp.status_code == 404

    async def test_list_only_own_products(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_auth_headers: dict,
    ) -> None:
        await client.post("/api/v1/products", json=_PRODUCT_PAYLOAD, headers=auth_headers)

        resp_b = await client.get("/api/v1/products", headers=second_auth_headers)
        assert resp_b.status_code == 200
        assert resp_b.json() == []

    async def test_cannot_patch_other_tenant_product(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_auth_headers: dict,
    ) -> None:
        create_resp = await client.post(
            "/api/v1/products", json=_PRODUCT_PAYLOAD, headers=auth_headers
        )
        product_id = create_resp.json()["id"]

        resp = await client.patch(
            f"/api/v1/products/{product_id}",
            json={"stock_units": 999},
            headers=second_auth_headers,
        )
        assert resp.status_code == 404

    async def test_cannot_delete_other_tenant_product(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_auth_headers: dict,
    ) -> None:
        create_resp = await client.post(
            "/api/v1/products", json=_PRODUCT_PAYLOAD, headers=auth_headers
        )
        product_id = create_resp.json()["id"]

        resp = await client.delete(
            f"/api/v1/products/{product_id}", headers=second_auth_headers
        )
        assert resp.status_code == 404
