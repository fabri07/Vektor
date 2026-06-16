"""Tests for the customers CRUD endpoints + sales linkage.

Cubre:
- Crear / listar / get / patch / soft-delete de clientes.
- Idempotencia: 2º POST con la misma Idempotency-Key → 409, sin duplicar.
- Aislamiento por tenant: un tenant no ve ni accede a clientes de otro.
- Venta vinculada a cliente: POST /sales con customer_id y GET /sales?customer_id.
"""

import uuid
from datetime import date
from typing import Any

import pytest
from httpx import AsyncClient

_TODAY = str(date.today())

_CUSTOMER_PAYLOAD = {
    "name": "Cliente Uno",
    "email": "uno@example.com",
    "phone": "+54 11 1234-5678",
    "telegram_username": "@cliente_uno",
    "notes": "VIP",
}


def _key() -> str:
    return str(uuid.uuid4())


@pytest.mark.asyncio
class TestCustomersCRUD:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_create_customer(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        resp = await client.post(
            "/api/v1/customers", json=_CUSTOMER_PAYLOAD, headers=auth_headers
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["name"] == "Cliente Uno"
        assert body["email"] == "uno@example.com"
        assert body["telegram_username"] == "@cliente_uno"
        assert body["is_active"] is True
        assert body["custom_fields"] == {}
        assert "id" in body and "tenant_id" in body

    async def test_create_requires_name(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        resp = await client.post("/api/v1/customers", json={"name": ""}, headers=auth_headers)
        assert resp.status_code == 422

    async def test_list_customers(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        await client.post(
            "/api/v1/customers", json={"name": "A"}, headers=auth_headers
        )
        await client.post(
            "/api/v1/customers", json={"name": "B"}, headers=auth_headers
        )
        resp = await client.get("/api/v1/customers", headers=auth_headers)
        assert resp.status_code == 200
        names = {c["name"] for c in resp.json()}
        assert {"A", "B"} <= names

    async def test_get_customer(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        created = await client.post(
            "/api/v1/customers", json=_CUSTOMER_PAYLOAD, headers=auth_headers
        )
        cid = created.json()["id"]
        resp = await client.get(f"/api/v1/customers/{cid}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["id"] == cid

    async def test_get_customer_404(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        resp = await client.get(f"/api/v1/customers/{uuid.uuid4()}", headers=auth_headers)
        assert resp.status_code == 404

    async def test_patch_customer(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        created = await client.post(
            "/api/v1/customers", json=_CUSTOMER_PAYLOAD, headers=auth_headers
        )
        cid = created.json()["id"]
        resp = await client.patch(
            f"/api/v1/customers/{cid}",
            json={"name": "Cliente Renombrado", "phone": "+54 11 9999-0000"},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "Cliente Renombrado"
        assert body["phone"] == "+54 11 9999-0000"
        # Campo no enviado queda intacto.
        assert body["email"] == "uno@example.com"

    async def test_soft_delete_customer(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        created = await client.post(
            "/api/v1/customers", json=_CUSTOMER_PAYLOAD, headers=auth_headers
        )
        cid = created.json()["id"]
        resp = await client.delete(f"/api/v1/customers/{cid}", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json()["message"] == "Customer deactivated."
        # Ya no es accesible ni listado (soft-delete excluye deactivated).
        assert (await client.get(f"/api/v1/customers/{cid}", headers=auth_headers)).status_code == 404
        listed = await client.get("/api/v1/customers", headers=auth_headers)
        assert cid not in {c["id"] for c in listed.json()}


@pytest.mark.asyncio
class TestCustomersIdempotency:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_post_without_header_works(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        resp = await client.post(
            "/api/v1/customers", json={"name": "Sin Header"}, headers=auth_headers
        )
        assert resp.status_code == 201

    async def test_replay_returns_409_and_no_duplicate(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        key = _key()
        headers = {**auth_headers, "Idempotency-Key": key}

        before = await client.get("/api/v1/customers", headers=auth_headers)
        count_before = len(before.json())

        first = await client.post(
            "/api/v1/customers", json=_CUSTOMER_PAYLOAD, headers=headers
        )
        assert first.status_code == 201

        second = await client.post(
            "/api/v1/customers", json=_CUSTOMER_PAYLOAD, headers=headers
        )
        assert second.status_code == 409
        assert second.json()["detail"] == {"code": "DUPLICATE_IDEMPOTENT"}

        after = await client.get("/api/v1/customers", headers=auth_headers)
        assert len(after.json()) == count_before + 1


@pytest.mark.asyncio
class TestCustomersTenantIsolation:
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
            "/api/v1/customers", json=_CUSTOMER_PAYLOAD, headers=auth_headers
        )
        cid = created.json()["id"]

        # El segundo tenant no lo ve en su listado…
        other_list = await client.get("/api/v1/customers", headers=second_auth_headers)
        assert cid not in {c["id"] for c in other_list.json()}

        # …ni lo puede leer por id (404, no 403, para no filtrar existencia).
        other_get = await client.get(
            f"/api/v1/customers/{cid}", headers=second_auth_headers
        )
        assert other_get.status_code == 404


@pytest.mark.asyncio
class TestSaleCustomerLink:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_create_sale_with_customer_and_filter(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        # Cliente
        cust = await client.post(
            "/api/v1/customers", json={"name": "Comprador"}, headers=auth_headers
        )
        cid = cust.json()["id"]

        # Venta vinculada al cliente
        sale = await client.post(
            "/api/v1/sales",
            json={
                "amount": "2500.00",
                "quantity": 1,
                "transaction_date": _TODAY,
                "payment_method": "cash",
                "customer_id": cid,
            },
            headers=auth_headers,
        )
        assert sale.status_code == 201
        assert sale.json()["customer_id"] == cid

        # Venta sin cliente (no debe aparecer al filtrar)
        other_sale = await client.post(
            "/api/v1/sales",
            json={
                "amount": "999.00",
                "quantity": 1,
                "transaction_date": _TODAY,
                "payment_method": "cash",
            },
            headers=auth_headers,
        )
        assert other_sale.status_code == 201

        # GET /sales?customer_id= devuelve solo la venta del cliente
        listed = await client.get(
            f"/api/v1/sales?customer_id={cid}", headers=auth_headers
        )
        assert listed.status_code == 200
        rows = listed.json()
        assert len(rows) == 1
        assert rows[0]["customer_id"] == cid
