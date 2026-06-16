"""Tests for optional Idempotency-Key header on POST creation endpoints.

Cubre los 3 endpoints de creación (`/sales`, `/expenses`, `/products`):
- 2º POST con la misma `Idempotency-Key` → 409 `DUPLICATE_IDEMPOTENT` y NO crea duplicado.
- POST sin header → comportamiento intacto (201).
- Keys distintas → ambos 201.
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


def _key() -> str:
    return str(uuid.uuid4())


@pytest.mark.asyncio
class TestIdempotencySales:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_post_without_header_works(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        resp = await client.post("/api/v1/sales", json=_SALE_PAYLOAD, headers=auth_headers)
        assert resp.status_code == 201

    async def test_replay_returns_409_and_no_duplicate(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        key = _key()
        headers = {**auth_headers, "Idempotency-Key": key}

        # Conteo previo
        before = await client.get("/api/v1/sales/summary", headers=auth_headers)
        count_before = before.json()["entry_count"]

        first = await client.post("/api/v1/sales", json=_SALE_PAYLOAD, headers=headers)
        assert first.status_code == 201

        second = await client.post("/api/v1/sales", json=_SALE_PAYLOAD, headers=headers)
        assert second.status_code == 409
        assert second.json()["detail"] == {"code": "DUPLICATE_IDEMPOTENT"}

        after = await client.get("/api/v1/sales/summary", headers=auth_headers)
        count_after = after.json()["entry_count"]
        # Solo se creó UNA venta a pesar de los dos POST.
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


@pytest.mark.asyncio
class TestIdempotencyExpenses:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_post_without_header_works(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        resp = await client.post("/api/v1/expenses", json=_EXPENSE_PAYLOAD, headers=auth_headers)
        assert resp.status_code == 201

    async def test_replay_returns_409_and_no_duplicate(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        key = _key()
        headers = {**auth_headers, "Idempotency-Key": key}

        before = await client.get("/api/v1/expenses/summary", headers=auth_headers)
        count_before = before.json()["entry_count"]

        first = await client.post("/api/v1/expenses", json=_EXPENSE_PAYLOAD, headers=headers)
        assert first.status_code == 201

        second = await client.post("/api/v1/expenses", json=_EXPENSE_PAYLOAD, headers=headers)
        assert second.status_code == 409
        assert second.json()["detail"] == {"code": "DUPLICATE_IDEMPOTENT"}

        after = await client.get("/api/v1/expenses/summary", headers=auth_headers)
        assert after.json()["entry_count"] == count_before + 1


@pytest.mark.asyncio
class TestIdempotencyProducts:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_post_without_header_works(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        payload = {**_PRODUCT_PAYLOAD, "sku": f"NOHDR-{_key()[:8]}"}
        resp = await client.post("/api/v1/products", json=payload, headers=auth_headers)
        assert resp.status_code == 201

    async def test_replay_returns_409_and_no_duplicate(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        key = _key()
        headers = {**auth_headers, "Idempotency-Key": key}
        payload = {**_PRODUCT_PAYLOAD, "sku": f"IDEM-{key[:8]}"}

        before = await client.get("/api/v1/products", headers=auth_headers)
        count_before = len(before.json())

        first = await client.post("/api/v1/products", json=payload, headers=headers)
        assert first.status_code == 201

        second = await client.post("/api/v1/products", json=payload, headers=headers)
        assert second.status_code == 409
        assert second.json()["detail"] == {"code": "DUPLICATE_IDEMPOTENT"}

        after = await client.get("/api/v1/products", headers=auth_headers)
        assert len(after.json()) == count_before + 1
