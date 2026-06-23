"""Tests de los fixes de review (Fase 2/3): vínculo de ventas/gastos a entidades.

Cubre:
- customer_id / supplier_id de OTRO tenant → 400 (no se confía en la FK sola).
- supplier_id se puede limpiar (null) en un PATCH de gasto.
- supplier_name se denormaliza desde el proveedor cuando no se envía (para que
  los rankings/análisis que agrupan por supplier_name vean la compra).
"""

import uuid
from datetime import date
from typing import Any

import pytest
from httpx import AsyncClient

_TODAY = str(date.today())


@pytest.mark.asyncio
class TestCrossTenantLinkRejected:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_sale_with_foreign_customer_id_rejected(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        second_auth_headers: dict[str, Any],
    ) -> None:
        # Cliente del segundo tenant (identidad + documento + celular obligatorios).
        other = await client.post(
            "/api/v1/customers",
            json={
                "name": "Ajeno",
                "last_name": "Test",
                "dni": "30123456",
                "phone": "1122334455",
            },
            headers=second_auth_headers,
        )
        other_id = other.json()["id"]

        # El primer tenant no puede vincular una venta a ese cliente ajeno.
        resp = await client.post(
            "/api/v1/sales",
            json={
                "amount": "100.00",
                "quantity": 1,
                "transaction_date": f"{_TODAY}T10:00:00",
                "payment_method": "cash",
                "customer_id": other_id,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400

    async def test_sale_with_unknown_customer_id_rejected(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        resp = await client.post(
            "/api/v1/sales",
            json={
                "amount": "100.00",
                "quantity": 1,
                "transaction_date": f"{_TODAY}T10:00:00",
                "payment_method": "cash",
                "customer_id": str(uuid.uuid4()),
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400

    async def test_expense_with_foreign_supplier_id_rejected(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        second_auth_headers: dict[str, Any],
    ) -> None:
        other = await client.post(
            "/api/v1/suppliers", json={"name": "Ajeno"}, headers=second_auth_headers
        )
        other_id = other.json()["id"]
        resp = await client.post(
            "/api/v1/expenses",
            json={
                "amount": "100.00",
                "category": "OTHER",
                "expense_date": _TODAY,
                "payment_method": "cash",
                "supplier_id": other_id,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 400


@pytest.mark.asyncio
class TestSupplierLinkBehaviour:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_supplier_name_denormalized_from_supplier(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        sup = await client.post(
            "/api/v1/suppliers", json={"name": "Distribuidora Norte"}, headers=auth_headers
        )
        sid = sup.json()["id"]
        # Gasto con supplier_id pero SIN supplier_name → se denormaliza el nombre.
        exp = await client.post(
            "/api/v1/expenses",
            json={
                "amount": "500.00",
                "category": "INVENTORY",
                "expense_date": _TODAY,
                "payment_method": "transfer",
                "supplier_id": sid,
            },
            headers=auth_headers,
        )
        assert exp.status_code == 201
        assert exp.json()["supplier_name"] == "Distribuidora Norte"

    async def test_supplier_id_can_be_cleared_on_patch(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        sup = await client.post(
            "/api/v1/suppliers", json={"name": "Prov"}, headers=auth_headers
        )
        sid = sup.json()["id"]
        exp = await client.post(
            "/api/v1/expenses",
            json={
                "amount": "500.00",
                "category": "OTHER",
                "expense_date": _TODAY,
                "payment_method": "cash",
                "supplier_id": sid,
            },
            headers=auth_headers,
        )
        eid = exp.json()["id"]
        assert exp.json()["supplier_id"] == sid

        # PATCH con supplier_id=null → se limpia.
        patched = await client.patch(
            f"/api/v1/expenses/{eid}",
            json={"supplier_id": None},
            headers=auth_headers,
        )
        assert patched.status_code == 200
        assert patched.json()["supplier_id"] is None
