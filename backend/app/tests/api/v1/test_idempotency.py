"""Header opcional `Idempotency-Key` en los POST de creación.

Fuente ÚNICA del contrato de idempotencia HTTP, parametrizada sobre los 5
endpoints de creación (`/sales`, `/expenses`, `/products`, `/customers`,
`/suppliers`) — antes customers y suppliers tenían sus copias en sus propios
archivos:
- 2º POST con la misma `Idempotency-Key` → 409 `DUPLICATE_IDEMPOTENT` y NO crea
  duplicado.
- POST sin header → comportamiento intacto (201).
- Keys distintas → ambos 201; misma key en otro tenant → entra (el claim es por
  `(tenant_id, fingerprint)`). Estos dos prueban el MECANISMO y corren una sola
  vez, sobre ventas.
"""

import uuid
from datetime import date
from typing import Any

import pytest
from httpx import AsyncClient

_TODAY = str(date.today())

_SALE_PAYLOAD = {
    "amount": "1500.00",
    "quantity": 3,
    "transaction_date": _TODAY,
    "payment_method": "cash",
}

_EXPENSE_PAYLOAD = {
    "amount": "800.00",
    "category": "OTHER",
    "expense_date": _TODAY,
    "description": "test gasto",
}

_PRODUCT_PAYLOAD = {
    "name": "Producto Idem",
    "sku": "IDEM-001",
    "sale_price_ars": "100.00",
    "stock_units": 5,
}

_CUSTOMER_PAYLOAD = {
    "name": "Cliente Uno",
    "customer_type": "person",
    "last_name": "Pérez",
    "dni": "30123456",
    "email": "uno@example.com",
    "phone": "+54 11 1234-5678",
}

_SUPPLIER_PAYLOAD = {
    "name": "Proveedor Uno",
    "email": "uno@proveedor.com",
    "phone": "+54 11 1234-5678",
    "notes": "Mayorista",
}

#: (endpoint de creación, payload, endpoint de conteo o None → len(GET lista)).
_ENDPOINTS = [
    pytest.param("/api/v1/sales", _SALE_PAYLOAD, "/api/v1/sales/summary", id="sales"),
    pytest.param(
        "/api/v1/expenses", _EXPENSE_PAYLOAD, "/api/v1/expenses/summary", id="expenses"
    ),
    pytest.param("/api/v1/products", _PRODUCT_PAYLOAD, None, id="products"),
    pytest.param("/api/v1/customers", _CUSTOMER_PAYLOAD, None, id="customers"),
    pytest.param("/api/v1/suppliers", _SUPPLIER_PAYLOAD, None, id="suppliers"),
]


def _key() -> str:
    return str(uuid.uuid4())


async def _count(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    endpoint: str,
    summary_url: str | None,
) -> int:
    if summary_url is not None:
        resp = await client.get(summary_url, headers=auth_headers)
        return int(resp.json()["entry_count"])
    resp = await client.get(endpoint, headers=auth_headers)
    return len(resp.json())


@pytest.mark.usefixtures("mock_score_trigger")
class TestIdempotency:
    @pytest.mark.parametrize(("endpoint", "payload", "summary_url"), _ENDPOINTS)
    async def test_post_without_header_works(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        endpoint: str,
        payload: dict[str, Any],
        summary_url: str | None,
    ) -> None:
        resp = await client.post(endpoint, json=payload, headers=auth_headers)
        assert resp.status_code == 201

    @pytest.mark.parametrize(("endpoint", "payload", "summary_url"), _ENDPOINTS)
    async def test_replay_returns_409_and_no_duplicate(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        endpoint: str,
        payload: dict[str, Any],
        summary_url: str | None,
    ) -> None:
        headers = {**auth_headers, "Idempotency-Key": _key()}

        count_before = await _count(client, auth_headers, endpoint, summary_url)

        first = await client.post(endpoint, json=payload, headers=headers)
        assert first.status_code == 201

        second = await client.post(endpoint, json=payload, headers=headers)
        assert second.status_code == 409
        assert second.json()["detail"] == {"code": "DUPLICATE_IDEMPOTENT"}

        count_after = await _count(client, auth_headers, endpoint, summary_url)
        # Solo se creó UN registro a pesar de los dos POST.
        assert count_after == count_before + 1

    async def test_different_keys_both_create(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        h1 = {**auth_headers, "Idempotency-Key": _key()}
        h2 = {**auth_headers, "Idempotency-Key": _key()}
        r1 = await client.post("/api/v1/sales", json=_SALE_PAYLOAD, headers=h1)
        r2 = await client.post("/api/v1/sales", json=_SALE_PAYLOAD, headers=h2)
        assert r1.status_code == 201
        assert r2.status_code == 201

    async def test_same_key_across_tenants_isolated(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        second_auth_headers: dict[str, Any],
    ) -> None:
        # La key se reclama por (tenant_id, fingerprint): misma key en otro tenant entra.
        key = _key()
        r1 = await client.post(
            "/api/v1/sales",
            json=_SALE_PAYLOAD,
            headers={**auth_headers, "Idempotency-Key": key},
        )
        r2 = await client.post(
            "/api/v1/sales",
            json=_SALE_PAYLOAD,
            headers={**second_auth_headers, "Idempotency-Key": key},
        )
        assert r1.status_code == 201
        assert r2.status_code == 201
