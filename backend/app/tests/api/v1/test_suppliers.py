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
        assert (await client.get(f"/api/v1/suppliers/{sid}", headers=auth_headers)).status_code == 404
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
