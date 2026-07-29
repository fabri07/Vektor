"""Tests for /api/v1/cash-closes endpoints (arqueo de caja diario). Sprint 20."""

from datetime import date
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.business import BusinessProfile

_TODAY = str(date.today())


@pytest.mark.asyncio
class TestCashClosePreview:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_preview_empty_day(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        resp = await client.get(
            f"/api/v1/cash-closes/preview?close_date={_TODAY}", headers=auth_headers
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["expected_total_ars"] == 0.0
        assert data["breakdown"] == []
        assert data["already_closed"] is False

    async def test_preview_nets_cash_expenses(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        # Venta en efectivo 10000 + venta por transferencia 5000
        await client.post(
            "/api/v1/sales",
            json={
                "amount": "10000.00",
                "quantity": 1,
                "transaction_date": _TODAY,
                "payment_method": "cash",
            },
            headers=auth_headers,
        )
        await client.post(
            "/api/v1/sales",
            json={
                "amount": "5000.00",
                "quantity": 1,
                "transaction_date": _TODAY,
                "payment_method": "transfer",
            },
            headers=auth_headers,
        )
        # Gasto en efectivo 3000 (debe restarse del efectivo esperado)
        await client.post(
            "/api/v1/expenses",
            json={
                "amount": "3000.00",
                "category": "INVENTORY",
                "expense_date": _TODAY,
                "payment_method": "cash",
            },
            headers=auth_headers,
        )
        resp = await client.get(
            f"/api/v1/cash-closes/preview?close_date={_TODAY}", headers=auth_headers
        )
        data = resp.json()
        methods = {b["payment_method"]: b["expected_ars"] for b in data["breakdown"]}
        assert methods["cash"] == 7000.0  # 10000 ventas - 3000 gasto efectivo
        assert methods["transfer"] == 5000.0
        assert data["expected_total_ars"] == 12000.0

    async def test_preview_normalizes_legacy_fiscal_condition(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: AsyncSession,
        sample_tenant: Any,
    ) -> None:
        """El preview normaliza el legacy 'registered' → 'monotributo', igual que
        GET /settings/fiscal-condition (consistencia entre ambos endpoints)."""
        profile = (
            await db_session.execute(
                select(BusinessProfile).where(
                    BusinessProfile.tenant_id == sample_tenant.tenant_id
                )
            )
        ).scalar_one()
        profile.fiscal_condition = "registered"  # valor legacy crudo
        await db_session.commit()

        resp = await client.get(
            f"/api/v1/cash-closes/preview?close_date={_TODAY}", headers=auth_headers
        )
        assert resp.status_code == 200
        assert resp.json()["fiscal_condition"] == "monotributo"


@pytest.mark.asyncio
class TestCashCloseCreate:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_create_computes_difference_server_side(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        await client.post(
            "/api/v1/sales",
            json={
                "amount": "10000.00",
                "quantity": 1,
                "transaction_date": _TODAY,
                "payment_method": "cash",
            },
            headers=auth_headers,
        )
        # El cliente cuenta 9500 (faltan 500)
        resp = await client.post(
            "/api/v1/cash-closes",
            json={"close_date": _TODAY, "counted_total_ars": "9500.00"},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["expected_total_ars"] == 10000.0
        assert data["counted_total_ars"] == 9500.0
        assert data["difference_ars"] == -500.0

    async def test_double_close_returns_409(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        body = {"close_date": _TODAY, "counted_total_ars": "0.00"}
        first = await client.post("/api/v1/cash-closes", json=body, headers=auth_headers)
        assert first.status_code == 201
        second = await client.post("/api/v1/cash-closes", json=body, headers=auth_headers)
        assert second.status_code == 409

    async def test_create_requires_owner_or_admin(
        self, client: AsyncClient, viewer_headers: dict[str, Any]
    ) -> None:
        resp = await client.post(
            "/api/v1/cash-closes",
            json={"close_date": _TODAY, "counted_total_ars": "100.00"},
            headers=viewer_headers,
        )
        assert resp.status_code == 403

    async def test_list_returns_created_close(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        await client.post(
            "/api/v1/cash-closes",
            json={"close_date": _TODAY, "counted_total_ars": "0.00"},
            headers=auth_headers,
        )
        resp = await client.get(
            f"/api/v1/cash-closes?from_date={_TODAY}&to_date={_TODAY}", headers=auth_headers
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 1
