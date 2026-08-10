"""Tests for /api/v1/products endpoints."""

from typing import Any

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


class TestProductsCRUD:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_create_product(self, client: AsyncClient, auth_headers: dict[str, Any]) -> None:
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
        # acquired_at por defecto None; created_at siempre presente.
        assert data["acquired_at"] is None
        assert data["created_at"] is not None

    async def test_create_product_rechaza_nombre_solo_espacios(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """Un ``name`` de solo espacios no puede pasar (F8d): sin strip previo,
        Pydantic ``min_length=1`` lo aceptaría y el listener de identidad lo
        normalizaría a ``""``, colando un producto sin nombre real."""
        payload = {**_PRODUCT_PAYLOAD, "name": "   "}
        resp = await client.post("/api/v1/products", json=payload, headers=auth_headers)
        assert resp.status_code == 422

    async def test_create_and_update_acquired_at(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """La fecha de alta (acquired_at) se persiste al crear y al editar."""
        payload = {**_PRODUCT_PAYLOAD, "acquired_at": "2026-01-10T09:30:00"}
        resp = await client.post("/api/v1/products", json=payload, headers=auth_headers)
        assert resp.status_code == 201
        assert resp.json()["acquired_at"].startswith("2026-01-10T09:30")
        product_id = resp.json()["id"]

        patch = await client.patch(
            f"/api/v1/products/{product_id}",
            json={"acquired_at": "2026-02-15T14:00:00"},
            headers=auth_headers,
        )
        assert patch.status_code == 200
        assert patch.json()["acquired_at"].startswith("2026-02-15T14:00")

        # Borrar acquired_at: enviar null debe limpiarlo (no quedar el valor previo).
        cleared = await client.patch(
            f"/api/v1/products/{product_id}",
            json={"acquired_at": None},
            headers=auth_headers,
        )
        assert cleared.status_code == 200
        assert cleared.json()["acquired_at"] is None

    async def test_list_products(self, client: AsyncClient, auth_headers: dict[str, Any]) -> None:
        await client.post("/api/v1/products", json=_PRODUCT_PAYLOAD, headers=auth_headers)
        resp = await client.get("/api/v1/products", headers=auth_headers)
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

    async def test_list_tolerates_legacy_json_null_custom_fields(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: Any,
        sample_tenant: Any,
    ) -> None:
        """Regresión: una fila con ``custom_fields = JSON null`` (``None`` en Python,
        lo que persistía el import de catálogo sin marca) NO debe romper el listado
        con 503 (``ResponseValidationError``). La respuesta la normaliza a ``{}``
        (mutación: quitar el ``field_validator`` de ``ProductResponse`` → 503)."""
        import uuid
        from decimal import Decimal

        from app.persistence.models.product import Product

        product = Product(
            id=uuid.uuid4(),
            tenant_id=sample_tenant.tenant_id,
            name="Legacy sin custom_fields",
            sale_price_ars=Decimal("1000"),
            stock_units=5,
            provenance="REAL",
            custom_fields=None,  # como el import viejo → 'null'::jsonb
        )
        db_session.add(product)
        await db_session.commit()

        resp = await client.get("/api/v1/products", headers=auth_headers)
        assert resp.status_code == 200
        row = next(p for p in resp.json() if p["id"] == str(product.id))
        assert row["custom_fields"] == {}

    async def test_list_products_filter_active(
        self, client: AsyncClient, auth_headers: dict[str, Any]
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

    async def test_get_product(self, client: AsyncClient, auth_headers: dict[str, Any]) -> None:
        create_resp = await client.post(
            "/api/v1/products", json=_PRODUCT_PAYLOAD, headers=auth_headers
        )
        product_id = create_resp.json()["id"]
        resp = await client.get(f"/api/v1/products/{product_id}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["id"] == product_id

    async def test_patch_product(self, client: AsyncClient, auth_headers: dict[str, Any]) -> None:
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

    async def test_patch_completing_data_clears_requires_completion(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        db_session: Any,
        sample_tenant: Any,
    ) -> None:
        """FASE 3 (B2): un producto auto-creado incompleto se completa vía PATCH."""
        import uuid
        from decimal import Decimal

        from app.persistence.models.product import Product

        product = Product(
            id=uuid.uuid4(),
            tenant_id=sample_tenant.tenant_id,
            name="Auto-creado sin costo",
            sale_price_ars=Decimal("2500"),
            unit_cost_ars=None,
            stock_units=5,
            provenance="REAL",
            requires_completion=True,
        )
        db_session.add(product)
        await db_session.commit()

        # Antes: el flag está activo en la respuesta.
        before = await client.get(f"/api/v1/products/{product.id}", headers=auth_headers)
        assert before.json()["requires_completion"] is True

        # PATCH que completa el costo → el flag se limpia.
        resp = await client.patch(
            f"/api/v1/products/{product.id}",
            json={"unit_cost_ars": "1500.00"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json()["requires_completion"] is False

    async def test_soft_delete_product(
        self, client: AsyncClient, auth_headers: dict[str, Any]
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
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        payload = {**_PRODUCT_PAYLOAD, "unit_cost_ars": None}
        resp = await client.post("/api/v1/products", json=payload, headers=auth_headers)
        assert resp.status_code == 201
        assert resp.json()["margin_pct"] is None


class TestProductsTenantIsolation:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_cannot_read_other_tenant_product(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        second_auth_headers: dict[str, Any],
    ) -> None:
        # Tenant A creates a product
        create_resp = await client.post(
            "/api/v1/products", json=_PRODUCT_PAYLOAD, headers=auth_headers
        )
        product_id = create_resp.json()["id"]

        # Tenant B tries to read it
        resp = await client.get(f"/api/v1/products/{product_id}", headers=second_auth_headers)
        assert resp.status_code == 404


class TestProductIdentityBarcodeExpiry:
    """F2-T2b: `barcode`/`expiry_date` en schemas + 409 por identidad duplicada."""

    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_create_product_with_barcode_and_expiry(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        payload = {
            **_PRODUCT_PAYLOAD,
            "barcode": "7791234567890",
            "expiry_date": "2027-03-15",
        }
        resp = await client.post("/api/v1/products", json=payload, headers=auth_headers)
        assert resp.status_code == 201
        data = resp.json()
        assert data["barcode"] == "7791234567890"
        assert data["expiry_date"] == "2027-03-15"

    async def test_create_product_duplicate_sku_409(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        first = {**_PRODUCT_PAYLOAD, "sku": "SKU-001"}
        resp1 = await client.post("/api/v1/products", json=first, headers=auth_headers)
        assert resp1.status_code == 201

        second = {**_PRODUCT_PAYLOAD, "name": "Otro nombre", "sku": "sku-001"}
        resp2 = await client.post("/api/v1/products", json=second, headers=auth_headers)
        assert resp2.status_code == 409
        detail = resp2.json()["detail"]
        assert detail["code"] == "DUPLICATE_PRODUCT_IDENTITY"
        assert detail["field"] == "sku"
        assert detail["existing_id"] == resp1.json()["id"]

    async def test_create_product_duplicate_barcode_409(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        first = {**_PRODUCT_PAYLOAD, "barcode": "7791111111111"}
        resp1 = await client.post("/api/v1/products", json=first, headers=auth_headers)
        assert resp1.status_code == 201

        second = {**_PRODUCT_PAYLOAD, "name": "Otro nombre", "barcode": "779-1111111111"}
        resp2 = await client.post("/api/v1/products", json=second, headers=auth_headers)
        assert resp2.status_code == 409
        detail = resp2.json()["detail"]
        assert detail["code"] == "DUPLICATE_PRODUCT_IDENTITY"
        assert detail["field"] == "barcode"

    async def test_create_product_barcode_vs_sku_conflict_409(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """Review F2 #6: si el barcode matchea un producto y el sku matchea OTRO,
        es un CONFLICTO de identidad (no un duplicado simple) → 409 IDENTITY_CONFLICT
        con AMBOS ids (nunca un .first() arbitrario)."""
        a = {**_PRODUCT_PAYLOAD, "name": "Producto A", "barcode": "7790000000017"}
        resp_a = await client.post("/api/v1/products", json=a, headers=auth_headers)
        assert resp_a.status_code == 201
        b = {**_PRODUCT_PAYLOAD, "name": "Producto B", "sku": "SKU-B"}
        resp_b = await client.post("/api/v1/products", json=b, headers=auth_headers)
        assert resp_b.status_code == 201

        # barcode → A, sku → B: conflicto.
        clash = {
            **_PRODUCT_PAYLOAD,
            "name": "Producto C",
            "barcode": "7790000000017",
            "sku": "SKU-B",
        }
        resp = await client.post("/api/v1/products", json=clash, headers=auth_headers)
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["code"] == "IDENTITY_CONFLICT"
        assert detail["barcode_product_id"] == resp_a.json()["id"]
        assert detail["sku_product_id"] == resp_b.json()["id"]

    async def test_patch_product_duplicate_sku_409(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """Review F2 #2: el PATCH valida identidad duplicada antes de aplicar un
        cambio de sku/barcode (antes hacía setattr directo → 2 activos con mismo SKU)."""
        a = {**_PRODUCT_PAYLOAD, "name": "Producto A", "sku": "SKU-AAA"}
        resp_a = await client.post("/api/v1/products", json=a, headers=auth_headers)
        assert resp_a.status_code == 201
        b = {**_PRODUCT_PAYLOAD, "name": "Producto B", "sku": "SKU-BBB"}
        resp_b = await client.post("/api/v1/products", json=b, headers=auth_headers)
        assert resp_b.status_code == 201

        # Intentar pisar B con el SKU de A → 409.
        resp = await client.patch(
            f"/api/v1/products/{resp_b.json()['id']}",
            json={"sku": "sku-aaa"},
            headers=auth_headers,
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "DUPLICATE_PRODUCT_IDENTITY"

    async def test_patch_product_same_sku_and_fresh_sku_ok(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """El PATCH NO se rechaza a sí mismo (exclude_id) y acepta un sku libre."""
        a = {**_PRODUCT_PAYLOAD, "name": "Producto A", "sku": "SKU-SELF"}
        resp_a = await client.post("/api/v1/products", json=a, headers=auth_headers)
        pid = resp_a.json()["id"]
        # Re-mandar el MISMO sku (no es duplicado de sí mismo).
        resp_same = await client.patch(
            f"/api/v1/products/{pid}", json={"sku": "SKU-SELF"}, headers=auth_headers
        )
        assert resp_same.status_code == 200
        # Un sku nuevo y libre.
        resp_new = await client.patch(
            f"/api/v1/products/{pid}", json={"sku": "SKU-NEW"}, headers=auth_headers
        )
        assert resp_new.status_code == 200
        assert resp_new.json()["sku"] == "SKU-NEW"

    async def test_create_product_duplicate_sku_other_tenant_ok(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        second_auth_headers: dict[str, Any],
    ) -> None:
        first = {**_PRODUCT_PAYLOAD, "sku": "SKU-ISO"}
        resp1 = await client.post("/api/v1/products", json=first, headers=auth_headers)
        assert resp1.status_code == 201

        second = {**_PRODUCT_PAYLOAD, "sku": "SKU-ISO"}
        resp2 = await client.post("/api/v1/products", json=second, headers=second_auth_headers)
        assert resp2.status_code == 201

    async def test_create_product_duplicate_name_only_allowed(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        resp1 = await client.post("/api/v1/products", json=_PRODUCT_PAYLOAD, headers=auth_headers)
        assert resp1.status_code == 201

        resp2 = await client.post("/api/v1/products", json=_PRODUCT_PAYLOAD, headers=auth_headers)
        assert resp2.status_code == 201

    async def test_list_only_own_products(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        second_auth_headers: dict[str, Any],
    ) -> None:
        await client.post("/api/v1/products", json=_PRODUCT_PAYLOAD, headers=auth_headers)

        resp_b = await client.get("/api/v1/products", headers=second_auth_headers)
        assert resp_b.status_code == 200
        assert resp_b.json() == []

    async def test_cannot_patch_other_tenant_product(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        second_auth_headers: dict[str, Any],
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
        auth_headers: dict[str, Any],
        second_auth_headers: dict[str, Any],
    ) -> None:
        create_resp = await client.post(
            "/api/v1/products", json=_PRODUCT_PAYLOAD, headers=auth_headers
        )
        product_id = create_resp.json()["id"]

        resp = await client.delete(f"/api/v1/products/{product_id}", headers=second_auth_headers)
        assert resp.status_code == 404
