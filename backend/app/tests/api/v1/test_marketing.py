"""Tests para los endpoints de marketing (métricas de redes + dashboard).

Cubre:
- CRUD de métrica (create / list / patch / delete).
- Carga bulk.
- Dashboard agrega por plataforma + ads vs ventas con ventas sembradas.
- Sin métricas → estructura vacía clara (has_data=False).
"""

from datetime import date
from typing import Any

import pytest
from httpx import AsyncClient

_TODAY = str(date.today())


def _metric(platform: str, **fields: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "platform": platform,
        "metric_date": _TODAY,
        "followers": 100,
        "reach": 500,
        "engagement": 50,
        "ads_spend_ars": "1000.00",
    }
    payload.update(fields)
    return payload


@pytest.mark.asyncio
class TestMarketingCRUD:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_create_metric(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        resp = await client.post(
            "/api/v1/marketing/metrics", json=_metric("instagram"), headers=auth_headers
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["platform"] == "instagram"
        assert body["followers"] == 100
        assert "id" in body and "tenant_id" in body

    async def test_create_invalid_platform_422(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        resp = await client.post(
            "/api/v1/marketing/metrics",
            json=_metric("myspace"),
            headers=auth_headers,
        )
        assert resp.status_code == 422

    async def test_list_metrics_with_platform_filter(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        await client.post(
            "/api/v1/marketing/metrics", json=_metric("instagram"), headers=auth_headers
        )
        await client.post(
            "/api/v1/marketing/metrics", json=_metric("tiktok"), headers=auth_headers
        )
        resp = await client.get(
            "/api/v1/marketing/metrics?platform=tiktok", headers=auth_headers
        )
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["platform"] == "tiktok"

    async def test_patch_metric(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        created = await client.post(
            "/api/v1/marketing/metrics", json=_metric("facebook"), headers=auth_headers
        )
        mid = created.json()["id"]
        resp = await client.patch(
            f"/api/v1/marketing/metrics/{mid}",
            json={"followers": 250},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["followers"] == 250

    async def test_delete_metric(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        created = await client.post(
            "/api/v1/marketing/metrics", json=_metric("other"), headers=auth_headers
        )
        mid = created.json()["id"]
        resp = await client.delete(
            f"/api/v1/marketing/metrics/{mid}", headers=auth_headers
        )
        assert resp.status_code == 200
        listed = await client.get("/api/v1/marketing/metrics", headers=auth_headers)
        assert mid not in {m["id"] for m in listed.json()}

    async def test_bulk_create(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        resp = await client.post(
            "/api/v1/marketing/metrics/bulk",
            json=[_metric("instagram"), _metric("tiktok"), _metric("facebook")],
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.text
        assert len(resp.json()) == 3

    async def test_cross_tenant_isolation(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        second_auth_headers: dict[str, Any],
    ) -> None:
        created = await client.post(
            "/api/v1/marketing/metrics", json=_metric("instagram"), headers=auth_headers
        )
        mid = created.json()["id"]
        other = await client.get("/api/v1/marketing/metrics", headers=second_auth_headers)
        assert mid not in {m["id"] for m in other.json()}
        patch_other = await client.patch(
            f"/api/v1/marketing/metrics/{mid}",
            json={"followers": 1},
            headers=second_auth_headers,
        )
        assert patch_other.status_code == 404


@pytest.mark.asyncio
class TestMarketingDashboard:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_dashboard_empty(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        resp = await client.get("/api/v1/marketing/dashboard", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["has_data"] is False
        assert body["platforms"] == []
        assert body["total_ads_spend_ars"] == "0.00"
        assert body["ads_vs_sales"]["ratio"] is None

    async def test_dashboard_aggregates_and_ads_vs_sales(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        # Dos métricas de instagram (reach/engagement/ads se suman; followers = última).
        await client.post(
            "/api/v1/marketing/metrics",
            json=_metric("instagram", followers=100, reach=300, engagement=30,
                         ads_spend_ars="500.00"),
            headers=auth_headers,
        )
        await client.post(
            "/api/v1/marketing/metrics",
            json=_metric("instagram", followers=120, reach=200, engagement=20,
                         ads_spend_ars="500.00"),
            headers=auth_headers,
        )
        # Una venta del período → revenue para el ratio ads/ventas.
        sale = await client.post(
            "/api/v1/sales",
            json={
                "amount": "2000.00",
                "quantity": 1,
                "transaction_date": _TODAY,
                "payment_method": "cash",
            },
            headers=auth_headers,
        )
        assert sale.status_code == 201, sale.text

        resp = await client.get(
            "/api/v1/marketing/dashboard?days=30", headers=auth_headers
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["has_data"] is True
        ig = next(p for p in body["platforms"] if p["platform"] == "instagram")
        assert ig["reach"] == 500  # 300 + 200
        assert ig["engagement"] == 50  # 30 + 20
        assert ig["ads_spend_ars"] == "1000.00"  # 500 + 500
        assert ig["followers"] == 120  # último valor

        assert body["total_ads_spend_ars"] == "1000.00"
        avs = body["ads_vs_sales"]
        assert avs["revenue_ars"] == "2000.00"
        # ratio = 1000 / 2000 = 0.5
        assert avs["ratio"] == pytest.approx(0.5)
