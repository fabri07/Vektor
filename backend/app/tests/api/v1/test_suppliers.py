"""Tests for the suppliers CRUD endpoints + expense linkage.

Cubre:
- Crear / listar / get / patch / soft-delete de proveedores.
- Idempotencia: 2º POST con la misma Idempotency-Key → 409, sin duplicar.
- Aislamiento por tenant: un tenant no ve ni accede a proveedores de otro.
- Gasto vinculado a proveedor: POST /expenses con supplier_id y GET /expenses?supplier_id.
"""

import uuid
from datetime import date
from typing import Any

import pytest
from httpx import AsyncClient

_TODAY = str(date.today())

_SUPPLIER_PAYLOAD = {
    "name": "Proveedor Uno",
    "email": "uno@proveedor.com",
    "phone": "+54 11 1234-5678",
    "notes": "Mayorista",
}


def _key() -> str:
    return str(uuid.uuid4())


@pytest.mark.asyncio
class TestSuppliersCRUD:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_create_supplier(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        resp = await client.post(
            "/api/v1/suppliers", json=_SUPPLIER_PAYLOAD, headers=auth_headers
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "Proveedor Uno"
        assert body["email"] == "uno@proveedor.com"
        assert body["phone"] == "+54 11 1234-5678"
        assert body["is_active"] is True
        assert body["custom_fields"] == {}
        assert "id" in body and "tenant_id" in body

    async def test_create_requires_name(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        resp = await client.post("/api/v1/suppliers", json={"name": ""}, headers=auth_headers)
        assert resp.status_code == 422

    async def test_list_suppliers(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        await client.post("/api/v1/suppliers", json={"name": "A"}, headers=auth_headers)
        await client.post("/api/v1/suppliers", json={"name": "B"}, headers=auth_headers)
        resp = await client.get("/api/v1/suppliers", headers=auth_headers)
        assert resp.status_code == 200
        names = {s["name"] for s in resp.json()}
        assert {"A", "B"} <= names

    async def test_get_supplier(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        created = await client.post(
            "/api/v1/suppliers", json=_SUPPLIER_PAYLOAD, headers=auth_headers
        )
        sid = created.json()["id"]
        resp = await client.get(f"/api/v1/suppliers/{sid}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["id"] == sid

    async def test_get_supplier_404(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        resp = await client.get(f"/api/v1/suppliers/{uuid.uuid4()}", headers=auth_headers)
        assert resp.status_code == 404

    async def test_patch_supplier(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        created = await client.post(
            "/api/v1/suppliers", json=_SUPPLIER_PAYLOAD, headers=auth_headers
        )
        sid = created.json()["id"]
        resp = await client.patch(
            f"/api/v1/suppliers/{sid}",
            json={"name": "Proveedor Renombrado", "phone": "+54 11 9999-0000"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "Proveedor Renombrado"
        assert body["phone"] == "+54 11 9999-0000"
        # Campo no enviado queda intacto.
        assert body["email"] == "uno@proveedor.com"

    async def test_soft_delete_supplier(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        created = await client.post(
            "/api/v1/suppliers", json=_SUPPLIER_PAYLOAD, headers=auth_headers
        )
        sid = created.json()["id"]
        resp = await client.delete(f"/api/v1/suppliers/{sid}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["message"] == "Supplier deactivated."
        # Ya no es accesible ni listado (soft-delete excluye deactivated).
        detail = await client.get(f"/api/v1/suppliers/{sid}", headers=auth_headers)
        assert detail.status_code == 404
        listed = await client.get("/api/v1/suppliers", headers=auth_headers)
        assert sid not in {s["id"] for s in listed.json()}


@pytest.mark.asyncio
class TestSuppliersIdempotency:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_post_without_header_works(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        resp = await client.post(
            "/api/v1/suppliers", json={"name": "Sin Header"}, headers=auth_headers
        )
        assert resp.status_code == 201

    async def test_replay_returns_409_and_no_duplicate(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        key = _key()
        headers = {**auth_headers, "Idempotency-Key": key}

        before = await client.get("/api/v1/suppliers", headers=auth_headers)
        count_before = len(before.json())

        first = await client.post(
            "/api/v1/suppliers", json=_SUPPLIER_PAYLOAD, headers=headers
        )
        assert first.status_code == 201

        second = await client.post(
            "/api/v1/suppliers", json=_SUPPLIER_PAYLOAD, headers=headers
        )
        assert second.status_code == 409
        assert second.json()["detail"] == {"code": "DUPLICATE_IDEMPOTENT"}

        after = await client.get("/api/v1/suppliers", headers=auth_headers)
        assert len(after.json()) == count_before + 1


@pytest.mark.asyncio
class TestSuppliersTenantIsolation:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_other_tenant_cannot_see_or_access(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        second_auth_headers: dict[str, Any],
    ) -> None:
        created = await client.post(
            "/api/v1/suppliers", json=_SUPPLIER_PAYLOAD, headers=auth_headers
        )
        sid = created.json()["id"]

        # El segundo tenant no lo ve en su listado…
        other_list = await client.get("/api/v1/suppliers", headers=second_auth_headers)
        assert sid not in {s["id"] for s in other_list.json()}

        # …ni lo puede leer por id (404, no 403, para no filtrar existencia).
        other_get = await client.get(
            f"/api/v1/suppliers/{sid}", headers=second_auth_headers
        )
        assert other_get.status_code == 404


@pytest.mark.asyncio
class TestExpenseSupplierLink:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_create_expense_with_supplier_and_filter(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        # Proveedor
        sup = await client.post(
            "/api/v1/suppliers", json={"name": "Distribuidora"}, headers=auth_headers
        )
        sid = sup.json()["id"]

        # Gasto vinculado al proveedor
        expense = await client.post(
            "/api/v1/expenses",
            json={
                "amount": "2500.00",
                "category": "INVENTORY",
                "expense_date": _TODAY,
                "payment_method": "transfer",
                "supplier_id": sid,
            },
            headers=auth_headers,
        )
        assert expense.status_code == 201
        assert expense.json()["supplier_id"] == sid

        # Gasto sin proveedor (no debe aparecer al filtrar)
        other_expense = await client.post(
            "/api/v1/expenses",
            json={
                "amount": "999.00",
                "category": "OTHER",
                "expense_date": _TODAY,
                "payment_method": "cash",
            },
            headers=auth_headers,
        )
        assert other_expense.status_code == 201

        # GET /expenses?supplier_id= devuelve solo el gasto del proveedor
        listed = await client.get(
            f"/api/v1/expenses?supplier_id={sid}", headers=auth_headers
        )
        assert listed.status_code == 200
        rows = listed.json()
        assert len(rows) == 1
        assert rows[0]["supplier_id"] == sid


# ── FASE 2: campos fiscales (persona/empresa, CUIL, forma de pago) ────────────


@pytest.mark.asyncio
class TestSupplierFiscalFields:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_create_with_fiscal_fields(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        resp = await client.post(
            "/api/v1/suppliers",
            json={
                "name": "Juan",
                "last_name": "Pérez",
                "cuil": "20-12345678-6",
                "payment_method": "transfer",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["last_name"] == "Pérez"
        assert body["cuil"] == "20-12345678-6"
        assert body["payment_method"] == "transfer"

    async def test_invalid_cuil_rejected(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        resp = await client.post(
            "/api/v1/suppliers",
            json={"name": "Empresa SA", "cuil": "no-es-un-cuil"},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    async def test_cuil_bad_check_digit_rejected(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        # Formato válido pero dígito verificador incorrecto (el correcto es 6).
        resp = await client.post(
            "/api/v1/suppliers",
            json={"name": "Empresa SA", "cuil": "20-12345678-9"},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    async def test_empty_cuil_accepted_as_null(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        resp = await client.post(
            "/api/v1/suppliers",
            json={"name": "Razón Social SA", "cuil": ""},
            headers=auth_headers,
        )
        assert resp.status_code == 201
        assert resp.json()["cuil"] is None

    async def test_patch_can_complete_sentinel(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """Completar el sentinela: editarle datos y quitarle el flag _sentinel."""
        created = await client.post(
            "/api/v1/suppliers",
            json={"name": "No identificado", "custom_fields": {"_sentinel": "true"}},
            headers=auth_headers,
        )
        sid = created.json()["id"]
        patched = await client.patch(
            f"/api/v1/suppliers/{sid}",
            json={"name": "Proveedor Real", "custom_fields": {}},
            headers=auth_headers,
        )
        assert patched.status_code == 200
        assert patched.json()["name"] == "Proveedor Real"
        assert patched.json()["custom_fields"] == {}


# ── FASE 4: remito (también puebla la tabla de FASE 3) ────────────────────────


_RECEIPT_PAYLOAD = {
    "currency": "ARS",
    "shipping_cost": "1500.00",
    "lines": [
        {"product_name": "Yerba Playadito", "qty": 10, "unit_price": "800.00"},
        {"product_name": "Azúcar Ledesma", "qty": 5, "unit_price": "600.00"},
    ],
}


@pytest.mark.asyncio
class TestSupplierReceipts:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def _make_supplier(
        self, client: AsyncClient, headers: dict[str, Any]
    ) -> str:
        resp = await client.post(
            "/api/v1/suppliers", json={"name": "Mayorista Remito"}, headers=headers
        )
        return str(resp.json()["id"])

    async def test_receipt_creates_products_stock_cogs_and_logistics(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        sid = await self._make_supplier(client, auth_headers)
        resp = await client.post(
            f"/api/v1/suppliers/{sid}/receipts",
            json=_RECEIPT_PAYLOAD,
            headers=auth_headers,
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["lines"] == 2
        assert len(body["products_created"]) == 2
        assert len(body["expense_ids"]) == 2
        assert body["shipping_expense_id"] is not None
        # COGS = 10*800 + 5*600 = 11000
        assert body["total_cogs_ars"] == "11000.00"

        # Productos creados con stock.
        products = await client.get("/api/v1/products", headers=auth_headers)
        by_name = {p["name"]: p for p in products.json()}
        assert by_name["Yerba Playadito"]["stock_units"] == 10
        assert by_name["Azúcar Ledesma"]["stock_units"] == 5

        # Gasto LOGISTICS (envío) entre los gastos.
        expenses = await client.get("/api/v1/expenses", headers=auth_headers)
        cats = {e["category"] for e in expenses.json()}
        assert "LOGISTICS" in cats
        assert "INVENTORY" in cats

    async def test_receipt_idempotent(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        sid = await self._make_supplier(client, auth_headers)
        key = _key()
        headers = {**auth_headers, "Idempotency-Key": key}
        first = await client.post(
            f"/api/v1/suppliers/{sid}/receipts", json=_RECEIPT_PAYLOAD, headers=headers
        )
        assert first.status_code == 201
        second = await client.post(
            f"/api/v1/suppliers/{sid}/receipts", json=_RECEIPT_PAYLOAD, headers=headers
        )
        assert second.status_code == 409
        # No se duplicó: solo 2 productos del primer remito.
        products = await client.get("/api/v1/products", headers=auth_headers)
        assert len(products.json()) == 2

    async def test_receipt_validations(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        sid = await self._make_supplier(client, auth_headers)
        base = {"currency": "ARS", "lines": [
            {"product_name": "X", "qty": 1, "unit_price": "10.00"}
        ]}
        # qty <= 0
        bad_qty = {**base, "lines": [{"product_name": "X", "qty": 0, "unit_price": "10"}]}
        r = await client.post(
            f"/api/v1/suppliers/{sid}/receipts", json=bad_qty, headers=auth_headers
        )
        assert r.status_code == 422
        # qty fraccionario: se rechaza (el pipeline usa unidades enteras; truncar
        # perdería la línea silenciosamente).
        frac_qty = {**base, "lines": [{"product_name": "X", "qty": 0.5, "unit_price": "10"}]}
        r = await client.post(
            f"/api/v1/suppliers/{sid}/receipts", json=frac_qty, headers=auth_headers
        )
        assert r.status_code == 422
        # unit_price < 0
        bad_price = {
            **base,
            "lines": [{"product_name": "X", "qty": 1, "unit_price": "-5"}],
        }
        r = await client.post(
            f"/api/v1/suppliers/{sid}/receipts", json=bad_price, headers=auth_headers
        )
        assert r.status_code == 422
        # shipping < 0
        bad_ship = {**base, "shipping_cost": "-1"}
        r = await client.post(
            f"/api/v1/suppliers/{sid}/receipts", json=bad_ship, headers=auth_headers
        )
        assert r.status_code == 422
        # sin líneas
        r = await client.post(
            f"/api/v1/suppliers/{sid}/receipts",
            json={"currency": "ARS", "lines": []},
            headers=auth_headers,
        )
        assert r.status_code == 422
        # moneda != ARS
        bad_curr = {**base, "currency": "USD"}
        r = await client.post(
            f"/api/v1/suppliers/{sid}/receipts", json=bad_curr, headers=auth_headers
        )
        assert r.status_code == 422

    async def test_receipt_rejected_against_sentinel(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        # Un remito identifica al proveedor: no se permite contra "No identificado".
        created = await client.post(
            "/api/v1/suppliers",
            json={"name": "No identificado", "custom_fields": {"_sentinel": "true"}},
            headers=auth_headers,
        )
        sid = created.json()["id"]
        r = await client.post(
            f"/api/v1/suppliers/{sid}/receipts", json=_RECEIPT_PAYLOAD, headers=auth_headers
        )
        assert r.status_code == 400

    async def test_receipt_other_tenant_404(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        second_auth_headers: dict[str, Any],
    ) -> None:
        sid = await self._make_supplier(client, auth_headers)
        resp = await client.post(
            f"/api/v1/suppliers/{sid}/receipts",
            json=_RECEIPT_PAYLOAD,
            headers=second_auth_headers,
        )
        assert resp.status_code == 404


# ── FASE 3: tabla de productos comprados a un proveedor ───────────────────────


@pytest.mark.asyncio
class TestSupplierProducts:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_lists_products_after_receipt(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        sup = await client.post(
            "/api/v1/suppliers", json={"name": "Prov Fase3"}, headers=auth_headers
        )
        sid = sup.json()["id"]
        await client.post(
            f"/api/v1/suppliers/{sid}/receipts",
            json=_RECEIPT_PAYLOAD,
            headers=auth_headers,
        )
        resp = await client.get(
            f"/api/v1/suppliers/{sid}/products", headers=auth_headers
        )
        assert resp.status_code == 200
        rows = resp.json()
        names = {r["name"] for r in rows}
        assert names == {"Yerba Playadito", "Azúcar Ledesma"}
        yerba = next(r for r in rows if r["name"] == "Yerba Playadito")
        assert yerba["total_qty"] == 10
        # unit_price se serializa como número (convención del repo), no string.
        assert yerba["unit_price"] == 800.0
        assert yerba["last_purchase_at"] is not None

    async def test_empty_when_no_purchases(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        sup = await client.post(
            "/api/v1/suppliers", json={"name": "Sin Compras"}, headers=auth_headers
        )
        sid = sup.json()["id"]
        resp = await client.get(
            f"/api/v1/suppliers/{sid}/products", headers=auth_headers
        )
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_other_tenant_404(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        second_auth_headers: dict[str, Any],
    ) -> None:
        sup = await client.post(
            "/api/v1/suppliers", json={"name": "Aislado"}, headers=auth_headers
        )
        sid = sup.json()["id"]
        resp = await client.get(
            f"/api/v1/suppliers/{sid}/products", headers=second_auth_headers
        )
        assert resp.status_code == 404
