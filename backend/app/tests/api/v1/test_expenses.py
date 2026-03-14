"""Tests for /api/v1/expenses endpoints."""

import pytest
from httpx import AsyncClient

_TODAY = "2026-03-13"

_EXPENSE_PAYLOAD = {
    "amount": "15000.00",
    "category": "RENT",
    "expense_date": _TODAY,
    "notes": "Alquiler marzo",
}


@pytest.mark.asyncio
class TestExpensesCRUD:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_create_expense(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        resp = await client.post(
            "/api/v1/expenses", json=_EXPENSE_PAYLOAD, headers=auth_headers
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["amount"] == "15000.00"
        assert data["category"] == "RENT"
        assert data["transaction_date"] == _TODAY

    async def test_invalid_category_rejected(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        payload = {**_EXPENSE_PAYLOAD, "category": "rent"}  # lowercase not accepted
        resp = await client.post("/api/v1/expenses", json=payload, headers=auth_headers)
        assert resp.status_code == 422

    async def test_all_valid_categories_accepted(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        for cat in ("RENT", "UTILITIES", "PAYROLL", "INVENTORY", "MARKETING", "OTHER"):
            resp = await client.post(
                "/api/v1/expenses",
                json={**_EXPENSE_PAYLOAD, "category": cat},
                headers=auth_headers,
            )
            assert resp.status_code == 201, f"Category {cat} failed with {resp.json()}"

    async def test_get_expense(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        create_resp = await client.post(
            "/api/v1/expenses", json=_EXPENSE_PAYLOAD, headers=auth_headers
        )
        expense_id = create_resp.json()["id"]
        resp = await client.get(f"/api/v1/expenses/{expense_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["id"] == expense_id

    async def test_patch_expense(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        create_resp = await client.post(
            "/api/v1/expenses", json=_EXPENSE_PAYLOAD, headers=auth_headers
        )
        expense_id = create_resp.json()["id"]
        resp = await client.patch(
            f"/api/v1/expenses/{expense_id}",
            json={"amount": "18000.00"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["amount"] == "18000.00"

    async def test_delete_expense(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        create_resp = await client.post(
            "/api/v1/expenses", json=_EXPENSE_PAYLOAD, headers=auth_headers
        )
        expense_id = create_resp.json()["id"]
        resp = await client.delete(f"/api/v1/expenses/{expense_id}", headers=auth_headers)
        assert resp.status_code == 200

        get_resp = await client.get(f"/api/v1/expenses/{expense_id}", headers=auth_headers)
        assert get_resp.status_code == 404

    async def test_list_expenses(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        await client.post("/api/v1/expenses", json=_EXPENSE_PAYLOAD, headers=auth_headers)
        resp = await client.get("/api/v1/expenses", headers=auth_headers)
        assert resp.status_code == 200
        assert len(resp.json()) >= 1


@pytest.mark.asyncio
class TestExpensesSummary:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_summary_empty(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        resp = await client.get("/api/v1/expenses/summary", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert float(data["total_ars"]) == 0.0
        assert data["entry_count"] == 0
        assert "period_covered" in data

    async def test_summary_counts_entries(
        self, client: AsyncClient, auth_headers: dict
    ) -> None:
        await client.post("/api/v1/expenses", json=_EXPENSE_PAYLOAD, headers=auth_headers)
        await client.post(
            "/api/v1/expenses",
            json={**_EXPENSE_PAYLOAD, "category": "UTILITIES", "amount": "5000.00"},
            headers=auth_headers,
        )
        resp = await client.get("/api/v1/expenses/summary", headers=auth_headers)
        data = resp.json()
        assert data["entry_count"] == 2
        assert float(data["total_ars"]) == pytest.approx(20000.0)


@pytest.mark.asyncio
class TestExpensesTenantIsolation:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_cannot_read_other_tenant_expense(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_auth_headers: dict,
    ) -> None:
        create_resp = await client.post(
            "/api/v1/expenses", json=_EXPENSE_PAYLOAD, headers=auth_headers
        )
        expense_id = create_resp.json()["id"]

        resp = await client.get(
            f"/api/v1/expenses/{expense_id}", headers=second_auth_headers
        )
        assert resp.status_code == 404

    async def test_list_only_own_expenses(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_auth_headers: dict,
    ) -> None:
        await client.post("/api/v1/expenses", json=_EXPENSE_PAYLOAD, headers=auth_headers)

        resp_b = await client.get("/api/v1/expenses", headers=second_auth_headers)
        assert resp_b.status_code == 200
        assert resp_b.json() == []

    async def test_summary_isolates_tenants(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_auth_headers: dict,
    ) -> None:
        await client.post("/api/v1/expenses", json=_EXPENSE_PAYLOAD, headers=auth_headers)

        resp_b = await client.get("/api/v1/expenses/summary", headers=second_auth_headers)
        assert resp_b.json()["entry_count"] == 0

    async def test_cannot_patch_other_tenant_expense(
        self,
        client: AsyncClient,
        auth_headers: dict,
        second_auth_headers: dict,
    ) -> None:
        create_resp = await client.post(
            "/api/v1/expenses", json=_EXPENSE_PAYLOAD, headers=auth_headers
        )
        expense_id = create_resp.json()["id"]

        resp = await client.patch(
            f"/api/v1/expenses/{expense_id}",
            json={"amount": "99999.00"},
            headers=second_auth_headers,
        )
        assert resp.status_code == 404
