"""Tests for /api/v1/sales endpoints."""

from datetime import date
from typing import Any

import pytest
from httpx import AsyncClient

_TODAY = str(date.today())

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
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        resp = await client.post("/api/v1/sales/bulk", json=_BULK_PAYLOAD, headers=auth_headers)
        assert resp.status_code == 201
        data = resp.json()
        assert len(data) == 1
        # amount se serializa como número (no string) para que el frontend sume sin NaN.
        assert data[0]["amount"] == 50000.0
        # transaction_date ahora es datetime ISO ("YYYY-MM-DDThh:mm:ss"); chequear la fecha.
        assert data[0]["transaction_date"].startswith(_TODAY)

    async def test_bulk_with_entries_creates_multiple_records(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        resp = await client.post(
            "/api/v1/sales/bulk", json=_BULK_PAYLOAD_WITH_ENTRIES, headers=auth_headers
        )
        assert resp.status_code == 201
        data = resp.json()
        assert len(data) == 2
        amounts = {d["amount"] for d in data}
        assert 1000.0 in amounts
        assert 2000.0 in amounts

    async def test_bulk_invalid_period_type(
        self, client: AsyncClient, auth_headers: dict[str, Any]
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

    async def test_summary_empty(self, client: AsyncClient, auth_headers: dict[str, Any]) -> None:
        resp = await client.get("/api/v1/sales/summary", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert float(data["total_ars"]) == 0.0
        assert data["entry_count"] == 0
        assert "period_covered" in data

    async def test_summary_counts_entries(
        self, client: AsyncClient, auth_headers: dict[str, Any]
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
class TestSalesDateRange:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_date_range_empty(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        resp = await client.get("/api/v1/sales/date-range", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["min_date"] is None
        assert data["max_date"] is None

    async def test_date_range_with_data(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        old = {**_SINGLE_PAYLOAD, "transaction_date": "2024-01-15"}
        recent = {**_SINGLE_PAYLOAD, "transaction_date": _TODAY}
        await client.post("/api/v1/sales", json=old, headers=auth_headers)
        await client.post("/api/v1/sales", json=recent, headers=auth_headers)
        resp = await client.get("/api/v1/sales/date-range", headers=auth_headers)
        data = resp.json()
        assert data["min_date"] == "2024-01-15"
        assert data["max_date"] == _TODAY

    async def test_afternoon_sale_included_in_same_day_range(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """Regresión: una venta de la tarde (23:30) se incluye en un rango to_date=hoy.

        Con transaction_date como DATETIME, `<= to_date` (medianoche) la excluiría;
        el filtro usa func.date() para preservar la semántica por día.
        """
        afternoon = {**_SINGLE_PAYLOAD, "transaction_date": f"{_TODAY}T23:30:00"}
        await client.post("/api/v1/sales", json=afternoon, headers=auth_headers)
        resp = await client.get(
            f"/api/v1/sales?from_date={_TODAY}&to_date={_TODAY}", headers=auth_headers
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["transaction_date"].startswith(_TODAY)
        # El summary (count_by_date_range) también debe contarla.
        summary = await client.get(
            f"/api/v1/sales/summary?from_date={_TODAY}&to_date={_TODAY}", headers=auth_headers
        )
        assert summary.json()["entry_count"] == 1

    async def test_date_range_not_confused_with_sale_id(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        # /date-range no debe matchear la ruta dinámica /{sale_id}
        resp = await client.get("/api/v1/sales/date-range", headers=auth_headers)
        assert resp.status_code == 200


@pytest.mark.asyncio
class TestSalesTenantIsolation:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_cannot_read_other_tenant_sale(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        second_auth_headers: dict[str, Any],
    ) -> None:
        # Tenant A creates a sale
        create_resp = await client.post("/api/v1/sales", json=_SINGLE_PAYLOAD, headers=auth_headers)
        assert create_resp.status_code == 201
        sale_id = create_resp.json()["id"]

        # Tenant B tries to fetch it
        resp = await client.get(f"/api/v1/sales/{sale_id}", headers=second_auth_headers)
        assert resp.status_code == 404

    async def test_list_only_own_sales(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        second_auth_headers: dict[str, Any],
    ) -> None:
        await client.post("/api/v1/sales", json=_SINGLE_PAYLOAD, headers=auth_headers)

        resp_b = await client.get("/api/v1/sales", headers=second_auth_headers)
        assert resp_b.status_code == 200
        assert resp_b.json() == []

    async def test_summary_isolates_tenants(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        second_auth_headers: dict[str, Any],
    ) -> None:
        await client.post("/api/v1/sales/bulk", json=_BULK_PAYLOAD, headers=auth_headers)

        # Tenant B sees zero
        resp_b = await client.get("/api/v1/sales/summary", headers=second_auth_headers)
        assert resp_b.json()["entry_count"] == 0

    async def test_cannot_delete_other_tenant_sale(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        second_auth_headers: dict[str, Any],
    ) -> None:
        create_resp = await client.post("/api/v1/sales", json=_SINGLE_PAYLOAD, headers=auth_headers)
        sale_id = create_resp.json()["id"]

        resp = await client.delete(f"/api/v1/sales/{sale_id}", headers=second_auth_headers)
        assert resp.status_code == 404


@pytest.mark.asyncio
class TestSalesRBAC:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_viewer_cannot_create_sale(
        self, client: AsyncClient, viewer_headers: dict[str, Any]
    ) -> None:
        resp = await client.post("/api/v1/sales", json=_SINGLE_PAYLOAD, headers=viewer_headers)
        assert resp.status_code == 403

    async def test_viewer_cannot_bulk_create(
        self, client: AsyncClient, viewer_headers: dict[str, Any]
    ) -> None:
        resp = await client.post("/api/v1/sales/bulk", json=_BULK_PAYLOAD, headers=viewer_headers)
        assert resp.status_code == 403

    async def test_viewer_cannot_delete_sale(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        viewer_headers: dict[str, Any],
    ) -> None:
        create_resp = await client.post("/api/v1/sales", json=_SINGLE_PAYLOAD, headers=auth_headers)
        sale_id = create_resp.json()["id"]
        resp = await client.delete(f"/api/v1/sales/{sale_id}", headers=viewer_headers)
        assert resp.status_code == 403
