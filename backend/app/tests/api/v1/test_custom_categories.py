"""Tests de categorías custom por tenant (gasto + producto) en custom_fields."""

from datetime import date
from typing import Any

import pytest
from httpx import AsyncClient

from app.persistence.models.business import BusinessProfile

_TODAY = str(date.today())


@pytest.fixture
async def _business_profile(sample_business_profile: BusinessProfile) -> None:
    # Las categorías custom viven en business_profiles.custom_fields; el perfil
    # lo crea `sample_tenant` (en prod siempre existe, lo hace el alta).
    return None


class TestExpenseCustomCategories:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_other_with_label_persists_and_dedupes(
        self, client: AsyncClient, auth_headers: dict[str, Any], _business_profile: None
    ) -> None:
        base = {
            "amount": "1000.00",
            "category": "OTHER",
            "expense_date": _TODAY,
            "payment_method": "cash",
        }
        r1 = await client.post(
            "/api/v1/expenses",
            json={**base, "category_label": "Veterinaria"},
            headers=auth_headers,
        )
        assert r1.status_code == 201, r1.text
        # mismo label con distinta capitalización/acentos no duplica
        r2 = await client.post(
            "/api/v1/expenses",
            json={**base, "category_label": "veterinária"},
            headers=auth_headers,
        )
        assert r2.status_code == 201

        resp = await client.get("/api/v1/expenses/custom-categories", headers=auth_headers)
        assert resp.status_code == 200
        cats = resp.json()
        assert cats.count("Veterinaria") == 1
        assert len(cats) == 1


class TestProductCustomCategories:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_create_custom_category_appears_in_catalog(
        self, client: AsyncClient, auth_headers: dict[str, Any], _business_profile: None
    ) -> None:
        resp = await client.post(
            "/api/v1/products/custom-categories", json={"label": "Mascotas"}, headers=auth_headers
        )
        assert resp.status_code == 201, resp.text
        created = resp.json()
        assert created["label"] == "Mascotas"
        assert created["code"].startswith("CUSTOM_")

        cat = await client.get("/api/v1/products/categories", headers=auth_headers)
        labels = [c["label"] for c in cat.json()]
        assert "Mascotas" in labels
        # idempotente: crear de nuevo no duplica
        await client.post(
            "/api/v1/products/custom-categories", json={"label": "mascotas"}, headers=auth_headers
        )
        cat2 = await client.get("/api/v1/products/categories", headers=auth_headers)
        assert [c["label"] for c in cat2.json()].count("Mascotas") == 1

    async def test_product_assigned_custom_category_keeps_its_code(
        self, client: AsyncClient, auth_headers: dict[str, Any], _business_profile: None
    ) -> None:
        created = (
            await client.post(
                "/api/v1/products/custom-categories",
                json={"label": "Mascotas"},
                headers=auth_headers,
            )
        ).json()
        resp = await client.post(
            "/api/v1/products",
            json={"name": "Alimento perro", "sale_price_ars": "100.00", "category": "Mascotas"},
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["category"] == created["code"]
