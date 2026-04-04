"""Tests for /api/v1/sales endpoints."""

import pytest
from httpx import AsyncClient

_TODAY = "2026-03-13"

_BULK_PAYLOAD = {
    "period_type": "weekly",
    "period_date": _TODAY,
    "total_amount_ars": "50000.00",
}

_BULK_PAYLOAD_WITH_ENTRIES = {
    "period_type": "daily",
    "period_date": _TODAY,
    "total_amount_ars": "3000.00",
    "entries": [
        {"amount_ars": "1000.00", "quantity": 2},
        {"amount_ars": "2000.00", "quantity": 1},
    ],
}

_SINGLE_PAYLOAD = {
    "amount": "1500.00",
    "quantity": 3,
    "transaction_date": _TODAY,
    "payment_method": "cash",
}


@pytest.mark.asyncio
class TestSalesBulk:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_bulk_without_entries_creates_one_record(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        resp = await client.post(
            "/api/v1/sales/bulk", json=_BULK_PAYLOAD, headers=auth_headers
        )
        assert resp.status_code == 201
        data = resp.json()
        assert len(data) == 1
        assert data[0]["amount"] == "50000.00"
        assert data[0]["transaction_date"] == _TODAY

    async def test_bulk_with_entries_creates_multiple_records(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        resp = await client.post(
            "/api/v1/sales/bulk", json=_BULK_PAYLOAD_WITH_ENTRIES, headers=auth_headers
        )
        assert resp.status_code == 201
        data = resp.json()
        assert len(data) == 2
        amounts = {d["amount"] for d in data}
        assert "1000.00" in amounts
        assert "2000.00" in amounts

    async def test_bulk_invalid_period_type(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        payload = {**_BULK_PAYLOAD, "period_type": "yearly"}
        resp = await client.post("/api/v1/sales/bulk", json=payload, headers=auth_headers)
        assert resp.status_code == 422

    async def test_bulk_requires_auth(self, client: AsyncClient) -> None:
        resp = await client.post("/api/v1/sales/bulk", json=_BULK_PAYLOAD)
        assert resp.status_code == 401


@pytest.mark.asyncio
class TestSalesSummary:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_summary_empty(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        resp = await client.get("/api/v1/sales/summary", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert float(data["total_ars"]) == 0.0
        assert data["entry_count"] == 0
        assert "period_covered" in data

    async def test_summary_counts_entries(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        await client.post("/api/v1/sales/bulk", json=_BULK_PAYLOAD, headers=auth_headers)
        await client.post(
            "/api/v1/sales/bulk", json=_BULK_PAYLOAD_WITH_ENTRIES, headers=auth_headers
        )
        resp = await client.get("/api/v1/sales/summary", headers=auth_headers)
        data = resp.json()
        assert data["entry_count"] == 3  # 1 + 2
        # total = 50000 + 1000 + 2000
        assert float(data["total_ars"]) == pytest.approx(53000.0)


@pytest.mark.asyncio
class TestSalesTenantIsolation:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_cannot_read_other_tenant_sale(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_auth_headers: dict,
    ) -> None:
        # Tenant A creates a sale
        create_resp = await client.post(
            "/api/v1/sales", json=_SINGLE_PAYLOAD, headers=auth_headers
        )
        assert create_resp.status_code == 201
        sale_id = create_resp.json()["id"]

        # Tenant B tries to fetch it
        resp = await client.get(f"/api/v1/sales/{sale_id}", headers=second_auth_headers)
        assert resp.status_code == 404

    async def test_list_only_own_sales(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_auth_headers: dict,
    ) -> None:
        await client.post("/api/v1/sales", json=_SINGLE_PAYLOAD, headers=auth_headers)

        resp_b = await client.get("/api/v1/sales", headers=second_auth_headers)
        assert resp_b.status_code == 200
        assert resp_b.json() == []

    async def test_summary_isolates_tenants(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_auth_headers: dict,
    ) -> None:
        await client.post("/api/v1/sales/bulk", json=_BULK_PAYLOAD, headers=auth_headers)

        # Tenant B sees zero
        resp_b = await client.get("/api/v1/sales/summary", headers=second_auth_headers)
        assert resp_b.json()["entry_count"] == 0

    async def test_cannot_delete_other_tenant_sale(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_auth_headers: dict,
    ) -> None:
        create_resp = await client.post(
            "/api/v1/sales", json=_SINGLE_PAYLOAD, headers=auth_headers
        )
        sale_id = create_resp.json()["id"]

        resp = await client.delete(f"/api/v1/sales/{sale_id}", headers=second_auth_headers)
        assert resp.status_code == 404
