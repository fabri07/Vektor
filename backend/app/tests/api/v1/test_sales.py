"""Tests for /api/v1/sales endpoints."""

import unittest.mock
from datetime import date
from typing import Any

import pytest
from httpx import AsyncClient

_TODAY = str(date.today())

_BULK_PAYLOAD = {
    "period_type": "weekly",
    "period_date": _TODAY,
    "total_amount_ars": "50000.00",
}

_BULK_PAYLOAD_WITH_ENTRIES = {
    "period_type": "daily",
    "period_date": _TODAY,
    "total_amount_ars": "3000.00",
    "entries": [
        {"amount_ars": "1000.00", "quantity": 2},
        {"amount_ars": "2000.00", "quantity": 1},
    ],
}

_SINGLE_PAYLOAD = {
    "amount": "1500.00",
    "quantity": 3,
    "transaction_date": _TODAY,
    "payment_method": "cash",
}


class TestSalesBulk:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_bulk_without_entries_creates_one_record(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        resp = await client.post("/api/v1/sales/bulk", json=_BULK_PAYLOAD, headers=auth_headers)
        assert resp.status_code == 201
        data = resp.json()
        assert len(data) == 1
        # amount se serializa como número (no string) para que el frontend sume sin NaN.
        assert data[0]["amount"] == 50000.0
        # transaction_date ahora es datetime ISO ("YYYY-MM-DDThh:mm:ss"); chequear la fecha.
        assert data[0]["transaction_date"].startswith(_TODAY)

    async def test_bulk_with_entries_creates_multiple_records(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        resp = await client.post(
            "/api/v1/sales/bulk", json=_BULK_PAYLOAD_WITH_ENTRIES, headers=auth_headers
        )
        assert resp.status_code == 201
        data = resp.json()
        assert len(data) == 2
        amounts = {d["amount"] for d in data}
        assert 1000.0 in amounts
        assert 2000.0 in amounts

    async def test_bulk_invalid_period_type(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        payload = {**_BULK_PAYLOAD, "period_type": "yearly"}
        resp = await client.post("/api/v1/sales/bulk", json=payload, headers=auth_headers)
        assert resp.status_code == 422

    async def test_bulk_requires_auth(self, client: AsyncClient) -> None:
        resp = await client.post("/api/v1/sales/bulk", json=_BULK_PAYLOAD)
        assert resp.status_code == 401


class TestSalesSummary:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    # `test_summary_empty`, `test_date_range_empty`, `test_date_range_with_data` y
    # `test_summary_isolates_tenants` viven parametrizados por entidad en
    # test_transaction_summaries.py (eran clones exactos con expenses).

    async def test_summary_counts_entries(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        await client.post("/api/v1/sales/bulk", json=_BULK_PAYLOAD, headers=auth_headers)
        await client.post(
            "/api/v1/sales/bulk", json=_BULK_PAYLOAD_WITH_ENTRIES, headers=auth_headers
        )
        resp = await client.get("/api/v1/sales/summary", headers=auth_headers)
        data = resp.json()
        assert data["entry_count"] == 3  # 1 + 2
        # total = 50000 + 1000 + 2000
        assert float(data["total_ars"]) == pytest.approx(53000.0)


class TestSalesDateRange:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_afternoon_sale_included_in_same_day_range(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """Regresión: una venta de la tarde (23:30) se incluye en un rango to_date=hoy.

        Con transaction_date como DATETIME, `<= to_date` (medianoche) la excluiría;
        el filtro usa func.date() para preservar la semántica por día.
        """
        afternoon = {**_SINGLE_PAYLOAD, "transaction_date": f"{_TODAY}T23:30:00"}
        await client.post("/api/v1/sales", json=afternoon, headers=auth_headers)
        resp = await client.get(
            f"/api/v1/sales?from_date={_TODAY}&to_date={_TODAY}", headers=auth_headers
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["transaction_date"].startswith(_TODAY)
        # El summary (count_by_date_range) también debe contarla.
        summary = await client.get(
            f"/api/v1/sales/summary?from_date={_TODAY}&to_date={_TODAY}", headers=auth_headers
        )
        assert summary.json()["entry_count"] == 1

    async def test_date_range_not_confused_with_sale_id(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        # /date-range no debe matchear la ruta dinámica /{sale_id}
        resp = await client.get("/api/v1/sales/date-range", headers=auth_headers)
        assert resp.status_code == 200


class TestSalesCount:
    """Paginación real de /sales (corrección C4 ampliada, 2026-08-19): sin un
    endpoint de count, el frontend no puede saber cuántas páginas hay."""

    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_count_matches_total_beyond_first_page(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        for _ in range(3):
            resp = await client.post("/api/v1/sales", json=_SINGLE_PAYLOAD, headers=auth_headers)
            assert resp.status_code == 201

        resp = await client.get("/api/v1/sales/count", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == {"total": 3}

    async def test_count_respects_date_range_filter(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        await client.post("/api/v1/sales", json=_SINGLE_PAYLOAD, headers=auth_headers)
        old = {**_SINGLE_PAYLOAD, "transaction_date": "2020-01-01"}
        await client.post("/api/v1/sales", json=old, headers=auth_headers)

        resp = await client.get(
            f"/api/v1/sales/count?from_date={_TODAY}&to_date={_TODAY}",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.json() == {"total": 1}

    async def test_count_not_confused_with_sale_id_route(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        # /count no debe matchear la ruta dinámica /{sale_id} (mismo gotcha que
        # /date-range, ver test_date_range_not_confused_with_sale_id).
        resp = await client.get("/api/v1/sales/count", headers=auth_headers)
        assert resp.status_code == 200
        assert resp.json() == {"total": 0}

    async def test_count_tenant_isolation(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        second_auth_headers: dict[str, Any],
    ) -> None:
        await client.post("/api/v1/sales", json=_SINGLE_PAYLOAD, headers=auth_headers)

        resp_b = await client.get("/api/v1/sales/count", headers=second_auth_headers)
        assert resp_b.json() == {"total": 0}


class TestSalesTenantIsolation:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_cannot_read_other_tenant_sale(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        second_auth_headers: dict[str, Any],
    ) -> None:
        # Tenant A creates a sale
        create_resp = await client.post("/api/v1/sales", json=_SINGLE_PAYLOAD, headers=auth_headers)
        assert create_resp.status_code == 201
        sale_id = create_resp.json()["id"]

        # Tenant B tries to fetch it
        resp = await client.get(f"/api/v1/sales/{sale_id}", headers=second_auth_headers)
        assert resp.status_code == 404

    async def test_list_only_own_sales(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        second_auth_headers: dict[str, Any],
    ) -> None:
        await client.post("/api/v1/sales", json=_SINGLE_PAYLOAD, headers=auth_headers)

        resp_b = await client.get("/api/v1/sales", headers=second_auth_headers)
        assert resp_b.status_code == 200
        assert resp_b.json() == []

    async def test_cannot_delete_other_tenant_sale(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        second_auth_headers: dict[str, Any],
    ) -> None:
        create_resp = await client.post("/api/v1/sales", json=_SINGLE_PAYLOAD, headers=auth_headers)
        sale_id = create_resp.json()["id"]

        resp = await client.delete(f"/api/v1/sales/{sale_id}", headers=second_auth_headers)
        assert resp.status_code == 404


class TestSalesRBAC:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_viewer_cannot_create_sale(
        self, client: AsyncClient, viewer_headers: dict[str, Any]
    ) -> None:
        resp = await client.post("/api/v1/sales", json=_SINGLE_PAYLOAD, headers=viewer_headers)
        assert resp.status_code == 403

    async def test_viewer_cannot_bulk_create(
        self, client: AsyncClient, viewer_headers: dict[str, Any]
    ) -> None:
        resp = await client.post("/api/v1/sales/bulk", json=_BULK_PAYLOAD, headers=viewer_headers)
        assert resp.status_code == 403

    async def test_viewer_cannot_delete_sale(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        viewer_headers: dict[str, Any],
    ) -> None:
        create_resp = await client.post("/api/v1/sales", json=_SINGLE_PAYLOAD, headers=auth_headers)
        sale_id = create_resp.json()["id"]
        resp = await client.delete(f"/api/v1/sales/{sale_id}", headers=viewer_headers)
        assert resp.status_code == 403


async def _create_product(
    client: AsyncClient, headers: dict[str, Any], name: str, *, stock: int, price: str = "100.00"
) -> str:
    resp = await client.post(
        "/api/v1/products",
        json={"name": name, "sale_price_ars": price, "stock_units": stock},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


class TestManualBatchSale:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        # decrement_stock emite eventos vía EventBus (Celery send_task → Redis):
        # neutralizarlos en tests, igual que test_stock_workflow.
        with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
            yield

    async def test_batch_creates_one_sale_per_item_and_decrements_stock(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        p1 = await _create_product(client, auth_headers, "Yerba", stock=10, price="1000.00")
        p2 = await _create_product(client, auth_headers, "Fideos", stock=5, price="500.00")
        payload = {
            "payment_method": "cash",
            "transaction_date": _TODAY,
            "items": [
                {"product_id": p1, "quantity": 2, "unit_price": "1000.00"},
                {"product_id": p2, "quantity": 1, "unit_price": "500.00"},
            ],
        }
        resp = await client.post(
            "/api/v1/sales/manual-batch", json=payload, headers=auth_headers
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert len(data["sales"]) == 2
        assert data["total"] == 2500.0
        # mismo sale_group_id en ambas líneas
        groups = {s["custom_fields"]["sale_group_id"] for s in data["sales"]}
        assert groups == {data["sale_group_id"]}
        # stock descontado
        r1 = await client.get(f"/api/v1/products/{p1}", headers=auth_headers)
        r2 = await client.get(f"/api/v1/products/{p2}", headers=auth_headers)
        assert r1.json()["stock_units"] == 8
        assert r2.json()["stock_units"] == 4

    async def test_oversell_is_rejected_and_nothing_persists(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        p1 = await _create_product(client, auth_headers, "Agua", stock=3, price="200.00")
        payload = {
            "payment_method": "cash",
            "transaction_date": _TODAY,
            "items": [{"product_id": p1, "quantity": 5, "unit_price": "200.00"}],
        }
        resp = await client.post(
            "/api/v1/sales/manual-batch", json=payload, headers=auth_headers
        )
        assert resp.status_code == 400
        # stock intacto
        r1 = await client.get(f"/api/v1/products/{p1}", headers=auth_headers)
        assert r1.json()["stock_units"] == 3

    async def test_fiado_without_real_customer_rejected(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        p1 = await _create_product(client, auth_headers, "Pan", stock=10, price="100.00")
        payload = {
            "payment_method": "account",
            "transaction_date": _TODAY,
            "items": [{"product_id": p1, "quantity": 1, "unit_price": "100.00"}],
        }
        resp = await client.post(
            "/api/v1/sales/manual-batch", json=payload, headers=auth_headers
        )
        assert resp.status_code == 400

    async def test_duplicate_product_rejected(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        p1 = await _create_product(client, auth_headers, "Cola", stock=10, price="100.00")
        payload = {
            "payment_method": "cash",
            "transaction_date": _TODAY,
            "items": [
                {"product_id": p1, "quantity": 1, "unit_price": "100.00"},
                {"product_id": p1, "quantity": 2, "unit_price": "100.00"},
            ],
        }
        resp = await client.post(
            "/api/v1/sales/manual-batch", json=payload, headers=auth_headers
        )
        assert resp.status_code == 400
        # nada se persistió: stock intacto
        r1 = await client.get(f"/api/v1/products/{p1}", headers=auth_headers)
        assert r1.json()["stock_units"] == 10


class TestLiveSaleStockDecrement:
    """POST /sales y su ciclo de vida descuentan/reponen stock (antes solo manual-batch)."""

    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
            yield

    async def _stock(self, client: AsyncClient, headers: dict[str, Any], pid: str) -> int:
        r = await client.get(f"/api/v1/products/{pid}", headers=headers)
        return int(r.json()["stock_units"])

    async def test_create_sale_with_product_decrements_stock(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        pid = await _create_product(client, auth_headers, "Yerba", stock=10, price="1000.00")
        payload = {**_SINGLE_PAYLOAD, "quantity": 3, "product_id": pid}
        resp = await client.post("/api/v1/sales", json=payload, headers=auth_headers)
        assert resp.status_code == 201, resp.text
        assert await self._stock(client, auth_headers, pid) == 7

    async def test_create_sale_without_product_does_not_break(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        resp = await client.post("/api/v1/sales", json=_SINGLE_PAYLOAD, headers=auth_headers)
        assert resp.status_code == 201, resp.text

    async def test_bulk_with_product_decrements_stock_per_line(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        pid = await _create_product(client, auth_headers, "Cola", stock=10, price="500.00")
        payload = {
            "period_type": "daily",
            "period_date": _TODAY,
            "total_amount_ars": "1500.00",
            "entries": [{"amount_ars": "1500.00", "quantity": 3, "product_id": pid}],
        }
        resp = await client.post("/api/v1/sales/bulk", json=payload, headers=auth_headers)
        assert resp.status_code == 201, resp.text
        assert await self._stock(client, auth_headers, pid) == 7

    async def test_bulk_idempotency_key_prevents_double_decrement(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        pid = await _create_product(client, auth_headers, "Té", stock=10, price="400.00")
        payload = {
            "period_type": "daily",
            "period_date": _TODAY,
            "total_amount_ars": "800.00",
            "entries": [{"amount_ars": "800.00", "quantity": 2, "product_id": pid}],
        }
        headers = {**auth_headers, "Idempotency-Key": "bulk-key-1"}
        r1 = await client.post("/api/v1/sales/bulk", json=payload, headers=headers)
        assert r1.status_code == 201, r1.text
        # reintento con la misma key → 409, sin segundo descuento
        r2 = await client.post("/api/v1/sales/bulk", json=payload, headers=headers)
        assert r2.status_code == 409
        assert await self._stock(client, auth_headers, pid) == 8  # descontado una sola vez

    async def test_delete_sale_restores_stock(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        pid = await _create_product(client, auth_headers, "Fideos", stock=10, price="500.00")
        payload = {**_SINGLE_PAYLOAD, "quantity": 4, "product_id": pid}
        sale_id = (
            await client.post("/api/v1/sales", json=payload, headers=auth_headers)
        ).json()["id"]
        assert await self._stock(client, auth_headers, pid) == 6

        resp = await client.delete(f"/api/v1/sales/{sale_id}", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        assert await self._stock(client, auth_headers, pid) == 10  # repuesto

    async def test_patch_quantity_adjusts_stock_differentially(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        pid = await _create_product(client, auth_headers, "Agua", stock=10, price="200.00")
        payload = {**_SINGLE_PAYLOAD, "quantity": 3, "product_id": pid}
        sale_id = (
            await client.post("/api/v1/sales", json=payload, headers=auth_headers)
        ).json()["id"]
        assert await self._stock(client, auth_headers, pid) == 7

        # subir la cantidad a 5 → debe descontar 2 más (10 − 5 = 5)
        resp = await client.patch(
            f"/api/v1/sales/{sale_id}", json={"quantity": 5}, headers=auth_headers
        )
        assert resp.status_code == 200, resp.text
        assert await self._stock(client, auth_headers, pid) == 5

    async def test_patch_amount_only_leaves_stock_intact(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        pid = await _create_product(client, auth_headers, "Pan", stock=10, price="300.00")
        payload = {**_SINGLE_PAYLOAD, "quantity": 2, "product_id": pid}
        sale_id = (
            await client.post("/api/v1/sales", json=payload, headers=auth_headers)
        ).json()["id"]
        assert await self._stock(client, auth_headers, pid) == 8

        resp = await client.patch(
            f"/api/v1/sales/{sale_id}", json={"amount": "999.00"}, headers=auth_headers
        )
        assert resp.status_code == 200, resp.text
        assert await self._stock(client, auth_headers, pid) == 8  # sin cambios

    # ── NO se permite stock negativo: validación + rechazo ────────────────────

    async def test_create_sale_insufficient_stock_rejected(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """POST con stock insuficiente → 400 y NO crea venta ni movimiento."""
        pid = await _create_product(client, auth_headers, "Vino", stock=2, price="1000.00")
        payload = {**_SINGLE_PAYLOAD, "quantity": 5, "product_id": pid}
        resp = await client.post("/api/v1/sales", json=payload, headers=auth_headers)
        assert resp.status_code == 400, resp.text
        body = resp.json()
        # El código estructurado es el contrato: el flush de la cola offline tiene que
        # distinguir ESTE 400 de cualquier otro sin leer el texto del mensaje.
        assert body["code"] == "INSUFFICIENT_STOCK"
        assert body["available"] == 2
        assert body["requested"] == 5
        assert body["product_id"] == pid
        # El mensaje nombra el producto y conserva la frase accionable.
        assert "Vino" in body["detail"]
        assert "Cargá primero" in body["detail"]
        assert await self._stock(client, auth_headers, pid) == 2  # intacto
        # No quedó ninguna venta de ese producto.
        sales = (await client.get("/api/v1/sales", headers=auth_headers)).json()
        assert all(s["product_id"] != pid for s in sales)

    async def test_create_sale_unknown_product_returns_400_not_500(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """product_id inexistente/de otro tenant → 400 claro (no 500)."""
        import uuid as _uuid  # noqa: PLC0415

        payload = {**_SINGLE_PAYLOAD, "quantity": 1, "product_id": str(_uuid.uuid4())}
        resp = await client.post("/api/v1/sales", json=payload, headers=auth_headers)
        assert resp.status_code == 400, resp.text

    async def test_bulk_sums_quantities_per_product_and_blocks(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """/sales/bulk valida contra la SUMA por producto (no por línea): dos líneas de 3
        del mismo producto con stock 5 → 400, y nada se persiste."""
        pid = await _create_product(client, auth_headers, "Café", stock=5, price="500.00")
        payload = {
            "period_type": "daily",
            "period_date": _TODAY,
            "total_amount_ars": "3000.00",
            "entries": [
                {"amount_ars": "1500.00", "quantity": 3, "product_id": pid},
                {"amount_ars": "1500.00", "quantity": 3, "product_id": pid},
            ],
        }
        resp = await client.post("/api/v1/sales/bulk", json=payload, headers=auth_headers)
        assert resp.status_code == 400, resp.text
        assert await self._stock(client, auth_headers, pid) == 5  # intacto

    async def test_patch_associate_product_decrements_when_enough(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """PATCH de sin-producto a con-producto con stock suficiente → descuenta."""
        pid = await _create_product(client, auth_headers, "Sal", stock=10, price="100.00")
        # Venta sin producto: no toca stock.
        sale_id = (
            await client.post("/api/v1/sales", json=_SINGLE_PAYLOAD, headers=auth_headers)
        ).json()["id"]
        resp = await client.patch(
            f"/api/v1/sales/{sale_id}",
            json={"product_id": pid, "quantity": 3},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        assert await self._stock(client, auth_headers, pid) == 7  # descontó 3

    async def test_patch_associate_product_rejected_when_insufficient(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """PATCH de sin-producto a con-producto sin stock suficiente → 400, stock intacto."""
        pid = await _create_product(client, auth_headers, "Azúcar", stock=2, price="100.00")
        sale_id = (
            await client.post(
                "/api/v1/sales",
                json={**_SINGLE_PAYLOAD, "quantity": 5},
                headers=auth_headers,
            )
        ).json()["id"]
        resp = await client.patch(
            f"/api/v1/sales/{sale_id}",
            json={"product_id": pid},  # quantity queda en 5
            headers=auth_headers,
        )
        assert resp.status_code == 400, resp.text
        assert await self._stock(client, auth_headers, pid) == 2  # intacto

    async def test_patch_increase_quantity_rejected_when_insufficient(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """PATCH que sube la cantidad más allá del stock incremental → 400, sin cambios."""
        pid = await _create_product(client, auth_headers, "Arroz", stock=5, price="100.00")
        sale_id = (
            await client.post(
                "/api/v1/sales",
                json={**_SINGLE_PAYLOAD, "quantity": 3, "product_id": pid},
                headers=auth_headers,
            )
        ).json()["id"]
        assert await self._stock(client, auth_headers, pid) == 2  # 5 − 3

        # Subir a 10: repone 3 → disponible 5, sigue faltando → 400.
        resp = await client.patch(
            f"/api/v1/sales/{sale_id}", json={"quantity": 10}, headers=auth_headers
        )
        assert resp.status_code == 400, resp.text
        assert await self._stock(client, auth_headers, pid) == 2  # sin cambios

    async def test_patch_disassociate_product_replenishes(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """PATCH de con-producto a sin-producto → repone el stock descontado."""
        pid = await _create_product(client, auth_headers, "Leche", stock=10, price="100.00")
        sale_id = (
            await client.post(
                "/api/v1/sales",
                json={**_SINGLE_PAYLOAD, "quantity": 4, "product_id": pid},
                headers=auth_headers,
            )
        ).json()["id"]
        assert await self._stock(client, auth_headers, pid) == 6

        resp = await client.patch(
            f"/api/v1/sales/{sale_id}", json={"product_id": None}, headers=auth_headers
        )
        assert resp.status_code == 200, resp.text
        assert await self._stock(client, auth_headers, pid) == 10  # repuesto

    async def test_patch_imported_sale_does_not_auto_decrement(
        self, client: AsyncClient, auth_headers: dict[str, Any], db_session
    ) -> None:
        """Editar una venta importada/releída (source_upload_id) NO arranca a descontar:
        su cantidad ya la cuenta la integridad desde sales_entries."""
        import uuid as _uuid  # noqa: PLC0415

        from app.persistence.models.transaction import SaleEntry as _SaleEntry  # noqa: PLC0415

        pid = await _create_product(client, auth_headers, "Aceite", stock=10, price="100.00")
        # Venta sin producto (sin movimiento) → marcarla como importada en la DB.
        sale_id = (
            await client.post("/api/v1/sales", json=_SINGLE_PAYLOAD, headers=auth_headers)
        ).json()["id"]
        sale = await db_session.get(_SaleEntry, _uuid.UUID(sale_id))
        sale.source_upload_id = _uuid.uuid4()
        await db_session.flush()

        # Asociar producto + cantidad: por ser importada NO debe descontar stock.
        resp = await client.patch(
            f"/api/v1/sales/{sale_id}",
            json={"product_id": pid, "quantity": 3},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        assert await self._stock(client, auth_headers, pid) == 10  # intacto


class TestSalesSubscriptionGate:
    """`READ_ONLY` bloquea cargar ventas nuevas — nunca corregir las que ya existen."""

    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_read_only_bloquea_una_venta_nueva(
        self, client: AsyncClient, auth_headers: dict[str, Any], sample_tenant, db_session
    ) -> None:
        from sqlalchemy import update  # noqa: PLC0415

        from app.persistence.models.tenant import Subscription  # noqa: PLC0415

        await db_session.execute(
            update(Subscription)
            .where(Subscription.tenant_id == sample_tenant.tenant_id)
            .values(plan_code="control", status="READ_ONLY", seats_included=3)
        )
        await db_session.commit()

        resp = await client.post("/api/v1/sales", json=_SINGLE_PAYLOAD, headers=auth_headers)
        assert resp.status_code == 402, resp.text
        assert resp.json()["detail"]["code"] == "SUBSCRIPTION_READ_ONLY"

    async def test_sin_ninguna_suscripcion_no_es_acceso_libre(
        self, client: AsyncClient, auth_headers: dict[str, Any], sample_tenant, db_session
    ) -> None:
        """Dato roto, no plan gratis: el gate dejaba pasar al recibir `None`."""
        from sqlalchemy import delete  # noqa: PLC0415

        from app.persistence.models.tenant import Subscription  # noqa: PLC0415

        await db_session.execute(
            delete(Subscription).where(Subscription.tenant_id == sample_tenant.tenant_id)
        )
        await db_session.commit()

        resp = await client.post("/api/v1/sales", json=_SINGLE_PAYLOAD, headers=auth_headers)
        assert resp.status_code == 503, resp.text
        assert resp.json()["detail"]["code"] == "SUBSCRIPTION_UNAVAILABLE"

    async def test_read_only_no_bloquea_corregir_una_venta_existente(
        self, client: AsyncClient, auth_headers: dict[str, Any], sample_tenant, db_session
    ) -> None:
        from sqlalchemy import update  # noqa: PLC0415

        from app.persistence.models.tenant import Subscription  # noqa: PLC0415

        create_resp = await client.post(
            "/api/v1/sales", json=_SINGLE_PAYLOAD, headers=auth_headers
        )
        sale_id = create_resp.json()["id"]

        await db_session.execute(
            update(Subscription)
            .where(Subscription.tenant_id == sample_tenant.tenant_id)
            .values(plan_code="control", status="READ_ONLY", seats_included=3)
        )
        await db_session.commit()

        resp = await client.patch(
            f"/api/v1/sales/{sale_id}", json={"notes": "corregido"}, headers=auth_headers
        )
        assert resp.status_code == 200, resp.text  # corregir NUNCA se bloquea


class TestB0DeudaPrevia:
    """Arreglos que el POS necesita antes de poder cobrar nada.

    Los cuatro tienen el mismo origen: comportamientos que para una carga manual
    de a una eran tolerables y para una caja son pérdida de plata o de trazabilidad.
    """

    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
            yield

    # ── unit_price se persiste ────────────────────────────────────────────────

    async def test_create_sale_persiste_unit_price(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """Se aceptaba en el request y se descartaba al construir la entidad.

        Sin él un descuento es irrecuperable: `amount / quantity` no lo reconstruye
        (la política prohíbe esa división, porque en una fila histórica no se sabe si
        el monto es unitario o total).
        """
        payload = {**_SINGLE_PAYLOAD, "amount": "1500.00", "quantity": 3, "unit_price": "500.00"}
        resp = await client.post("/api/v1/sales", json=payload, headers=auth_headers)
        assert resp.status_code == 201, resp.text
        assert float(resp.json()["unit_price"]) == 500.00

    async def test_create_sale_sin_unit_price_lo_deja_en_none(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """No informado es None, nunca un valor derivado: eso sería inventar el dato."""
        resp = await client.post("/api/v1/sales", json=_SINGLE_PAYLOAD, headers=auth_headers)
        assert resp.status_code == 201, resp.text
        assert resp.json()["unit_price"] is None

    async def test_manual_batch_persiste_unit_price_por_linea(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """manual-batch calculaba amount = unit_price * quantity y tiraba el unitario."""
        pid = await _create_product(client, auth_headers, "Gaseosa", stock=10, price="700.00")
        payload = {
            "payment_method": "cash",
            "transaction_date": _TODAY,
            "items": [{"product_id": pid, "quantity": 2, "unit_price": "650.00"}],
        }
        resp = await client.post(
            "/api/v1/sales/manual-batch", json=payload, headers=auth_headers
        )
        assert resp.status_code == 201, resp.text
        linea = resp.json()["sales"][0]
        # El unitario REALMENTE vendido (650) sobrevive aunque difiera del de lista (700).
        assert float(linea["unit_price"]) == 650.00
        assert float(linea["amount"]) == 1300.00

    # ── el rechazo por stock lleva código estructurado en los dos caminos ─────

    async def test_manual_batch_stock_insuficiente_trae_codigo_estructurado(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """manual-batch levantaba un HTTPException suelto: distinto shape que /sales.

        El flush de la cola offline necesita reconocer ESTE 400 venga del camino que
        venga, y no puede hacerlo leyendo el texto del mensaje.
        """
        pid = await _create_product(client, auth_headers, "Fernet", stock=1, price="9000.00")
        payload = {
            "payment_method": "cash",
            "transaction_date": _TODAY,
            "items": [{"product_id": pid, "quantity": 4, "unit_price": "9000.00"}],
        }
        resp = await client.post(
            "/api/v1/sales/manual-batch", json=payload, headers=auth_headers
        )
        assert resp.status_code == 400, resp.text
        body = resp.json()
        assert body["code"] == "INSUFFICIENT_STOCK"
        assert body["available"] == 1
        assert body["requested"] == 4
        assert body["product_id"] == pid
        assert "Fernet" in body["detail"]

    # ── unit_price en la corrección: se asigna, o se limpia si quedó mintiendo ──

    async def test_patch_asigna_unit_price_cuando_lo_informan(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """UpdateSaleRequest lo declaraba y update_sale nunca lo tocaba: 200 sin efecto."""
        payload = {**_SINGLE_PAYLOAD, "amount": "1000.00", "quantity": 2, "unit_price": "500.00"}
        sid = (await client.post("/api/v1/sales", json=payload, headers=auth_headers)).json()["id"]
        resp = await client.patch(
            f"/api/v1/sales/{sid}",
            json={"amount": "800.00", "quantity": 2, "unit_price": "400.00"},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        assert float(resp.json()["unit_price"]) == 400.00

    async def test_patch_de_amount_sin_unit_price_lo_limpia(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """Conservarlo dejaría 500 junto a amount=800 y quantity=2: el campo mentiría.

        No se recalcula como amount/quantity — eso sería inventarlo, la misma división
        que la política prohíbe en el resto del código. None es "no informado", que es
        exactamente lo que se sabe después de la corrección.
        """
        payload = {**_SINGLE_PAYLOAD, "amount": "1000.00", "quantity": 2, "unit_price": "500.00"}
        sid = (await client.post("/api/v1/sales", json=payload, headers=auth_headers)).json()["id"]
        resp = await client.patch(
            f"/api/v1/sales/{sid}", json={"amount": "800.00"}, headers=auth_headers
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["unit_price"] is None

    async def test_patch_que_no_toca_monto_ni_cantidad_conserva_unit_price(
        self, client: AsyncClient, auth_headers: dict[str, Any]
    ) -> None:
        """Cambiar la nota no invalida el precio al que se vendió."""
        payload = {**_SINGLE_PAYLOAD, "amount": "1000.00", "quantity": 2, "unit_price": "500.00"}
        sid = (await client.post("/api/v1/sales", json=payload, headers=auth_headers)).json()["id"]
        resp = await client.patch(
            f"/api/v1/sales/{sid}", json={"notes": "corregido"}, headers=auth_headers
        )
        assert resp.status_code == 200, resp.text
        assert float(resp.json()["unit_price"]) == 500.00

    async def test_la_auditoria_registra_unit_price(
        self, client: AsyncClient, auth_headers: dict[str, Any], db_session
    ) -> None:
        """El campo existe para trazar el precio por línea; sin esto no deja rastro."""
        from sqlalchemy import select

        from app.persistence.models.audit import DecisionAuditLog

        payload = {**_SINGLE_PAYLOAD, "amount": "1000.00", "quantity": 2, "unit_price": "500.00"}
        sid = (await client.post("/api/v1/sales", json=payload, headers=auth_headers)).json()["id"]
        await client.patch(
            f"/api/v1/sales/{sid}", json={"unit_price": "450.00"}, headers=auth_headers
        )
        filas = (
            await db_session.execute(
                select(DecisionAuditLog).where(
                    DecisionAuditLog.decision_type == "DATA_RECORD_UPDATED"
                )
            )
        ).scalars().all()
        datos = [
            f.decision_data
            for f in filas
            if f.decision_data.get("after", {}).get("id") == sid
        ]
        assert datos, "no se auditó el update"
        assert datos[-1]["before"]["unit_price"] == "500.00"
        assert datos[-1]["after"]["unit_price"] == "450.00"
