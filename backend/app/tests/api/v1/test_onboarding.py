"""
Tests for /api/v1/onboarding endpoints.

Required tests:
  - test_onboarding_submit_kiosco
  - test_onboarding_calculates_completeness_correctly
  - test_onboarding_cannot_submit_twice
  - test_onboarding_status_before_and_after
"""

import pytest
from httpx import AsyncClient

# ── Helpers ────────────────────────────────────────────────────────────────────

_REGISTER_PAYLOAD = {
    "email": "owner@kiosco.example.com",
    "password": "Secure123",
    "full_name": "Juan Pérez",
    "business_name": "Kiosco El Rápido",
    "vertical_code": "kiosco",
}

_ONBOARDING_PAYLOAD = {
    "vertical_code": "kiosco",
    "weekly_sales_estimate_ars": 50000,
    "monthly_inventory_cost_ars": 80000,
    "monthly_fixed_expenses_ars": 30000,
    "cash_on_hand_ars": 20000,
    "product_count_estimate": 10,
    "supplier_count_estimate": 3,
    "main_concern": "MARGIN",
}


async def _register_and_token(client: AsyncClient) -> str:
    await client.post("/api/v1/auth/register", json=_REGISTER_PAYLOAD)
    resp = await client.post(
        "/api/v1/auth/login",
        json={"email": _REGISTER_PAYLOAD["email"], "password": _REGISTER_PAYLOAD["password"]},
    )
    return resp.json()["access_token"]


# ── Tests ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestOnboarding:
    async def test_onboarding_submit_kiosco(self, client: AsyncClient) -> None:
        """POST /onboarding/submit with valid kiosco data returns snapshot_id and message."""
        token = await _register_and_token(client)
        headers = {"Authorization": f"Bearer {token}"}

        response = await client.post(
            "/api/v1/onboarding/submit",
            json=_ONBOARDING_PAYLOAD,
            headers=headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert "snapshot_id" in data
        assert "data_completeness_score" in data
        assert "confidence_level" in data
        assert data["message"] == "Procesando tu score..."

    async def test_onboarding_calculates_completeness_correctly(
        self, client: AsyncClient
    ) -> None:
        """Full payload (all fields > 0, products >= 5, suppliers >= 1) scores 100 HIGH."""
        token = await _register_and_token(client)
        headers = {"Authorization": f"Bearer {token}"}

        # ventas(25) + mercaderia(20) + fijos(15) + caja(20) + productos>=5(10) + proveedores>=1(10) = 100
        response = await client.post(
            "/api/v1/onboarding/submit",
            json=_ONBOARDING_PAYLOAD,
            headers=headers,
        )

        assert response.status_code == 200
        data = response.json()
        assert data["data_completeness_score"] == 100
        assert data["confidence_level"] == "HIGH"

    async def test_onboarding_cannot_submit_twice(self, client: AsyncClient) -> None:
        """Second submit with same tenant must return 409."""
        token = await _register_and_token(client)
        headers = {"Authorization": f"Bearer {token}"}

        await client.post(
            "/api/v1/onboarding/submit",
            json=_ONBOARDING_PAYLOAD,
            headers=headers,
        )
        response = await client.post(
            "/api/v1/onboarding/submit",
            json=_ONBOARDING_PAYLOAD,
            headers=headers,
        )

        assert response.status_code == 409

    async def test_onboarding_status_before_and_after(
        self, client: AsyncClient
    ) -> None:
        """GET /onboarding/status reflects completed=False before and True after submit."""
        token = await _register_and_token(client)
        headers = {"Authorization": f"Bearer {token}"}

        # Before onboarding
        resp_before = await client.get("/api/v1/onboarding/status", headers=headers)
        assert resp_before.status_code == 200
        assert resp_before.json()["completed"] is False

        # Submit onboarding
        await client.post(
            "/api/v1/onboarding/submit",
            json=_ONBOARDING_PAYLOAD,
            headers=headers,
        )

        # After onboarding
        resp_after = await client.get("/api/v1/onboarding/status", headers=headers)
        assert resp_after.status_code == 200
        after_data = resp_after.json()
        assert after_data["completed"] is True
        assert after_data["vertical_code"] == "kiosco"
        assert after_data["data_completeness_score"] == 100
