"""Tests del guard HTTP 423 (mantenimiento) + wiring del shared lock (F3-T3).

Los advisory locks (``pg_advisory_xact_lock[_shared]``) son no-op en SQLite —
acá NO se testea bloqueo real de Postgres (eso es un test de integración PG
de una tarea posterior). Se testea el comportamiento observable:

1. El guard HTTP 423 en FastAPI (``ensure_tenant_not_under_maintenance``)
   corta el request ANTES de llegar al handler cuando el tenant está bajo
   mantenimiento, y deja pasar cuando no lo está.
2. El wiring: ``maintenance_lock_service.acquire_write_lock_shared`` se
   invoca en los chokepoints de escritura — acá con ``stock_service``
   (increment/decrement, que pasan por ``_get_or_create_balance``) y con
   ``ProductRepository.save`` — como muestra representativa del patrón (no
   los 4 chokepoints, eso está documentado en el reporte de la tarea).
"""

from __future__ import annotations

import unittest.mock
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import maintenance_lock_service, purchase_service
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.unclassified_record import (
    UNCLASSIFIED_STATUS_PENDING,
    UnclassifiedRecord,
)
from app.persistence.repositories.product_repository import ProductRepository

_PRODUCT_PAYLOAD = {
    "name": "Coca-Cola 500ml",
    "category": "bebidas",
    "unit_cost_ars": "80.00",
    "sale_price_ars": "150.00",
    "stock_units": 50,
    "low_stock_threshold_units": 10,
}

_TODAY = str(date.today())

_EXPENSE_PAYLOAD = {
    "amount": "15000.00",
    "category": "RENT",
    "expense_date": _TODAY,
    "notes": "Alquiler marzo",
}


async def _create_product_via_api(
    client: AsyncClient, headers: dict[str, Any], name: str, *, stock: int, price: str = "100.00"
) -> str:
    resp = await client.post(
        "/api/v1/products",
        json={"name": name, "sale_price_ars": price, "stock_units": stock},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _create_supplier_via_api(client: AsyncClient, headers: dict[str, Any], name: str) -> str:
    resp = await client.post("/api/v1/suppliers", json={"name": name}, headers=headers)
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def _make_product(db: AsyncSession, tenant_id: uuid.UUID, stock_units: int) -> Product:
    product = Product(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name="Yerba 1kg",
        sale_price_ars=Decimal("2500"),
        unit_cost_ars=Decimal("1500"),
        stock_units=stock_units,
    )
    db.add(product)
    await db.flush()
    return product


@pytest.mark.asyncio
class TestMaintenanceGuard423:
    """``ensure_tenant_not_under_maintenance`` — UX fast-fail, no la exclusión real."""

    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger: Any) -> None:
        pass

    async def test_post_products_423_when_locked(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            maintenance_lock_service,
            "is_locked",
            unittest.mock.AsyncMock(return_value=True),
        )
        resp = await client.post(
            "/api/v1/products", json=_PRODUCT_PAYLOAD, headers=auth_headers
        )
        assert resp.status_code == 423
        assert "mantenimiento" in resp.json()["detail"].lower()

    async def test_patch_products_423_when_locked(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Crear el producto ANTES de trabar (create también está gateado).
        created = await client.post(
            "/api/v1/products", json=_PRODUCT_PAYLOAD, headers=auth_headers
        )
        assert created.status_code == 201
        product_id = created.json()["id"]

        monkeypatch.setattr(
            maintenance_lock_service,
            "is_locked",
            unittest.mock.AsyncMock(return_value=True),
        )
        resp = await client.patch(
            f"/api/v1/products/{product_id}",
            json={"stock_units": 60},
            headers=auth_headers,
        )
        assert resp.status_code == 423

    async def test_post_products_ok_when_not_locked(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            maintenance_lock_service,
            "is_locked",
            unittest.mock.AsyncMock(return_value=False),
        )
        resp = await client.post(
            "/api/v1/products", json=_PRODUCT_PAYLOAD, headers=auth_headers
        )
        assert resp.status_code == 201

    async def test_get_products_not_gated_even_when_locked(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Lectura pura: el guard NO se aplicó a GET /products a propósito."""
        monkeypatch.setattr(
            maintenance_lock_service,
            "is_locked",
            unittest.mock.AsyncMock(return_value=True),
        )
        resp = await client.get("/api/v1/products", headers=auth_headers)
        assert resp.status_code == 200

    async def test_post_others_reclassify_423_when_locked(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        auth_headers: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """CRITICAL 1 (review): reclassify_record muta Product (crea/vincula) y
        quedaba sin gatear. Con el tenant lockeado, un POST reclassify hacia
        entity_type="product" debe cortar en 423 antes de crear el producto."""
        record = UnclassifiedRecord(
            id=uuid.uuid4(),
            tenant_id=sample_tenant.tenant_id,
            source="ingestion",
            row_data={"nombre": "Yerba 1kg", "precio": "2500"},
            suggested_entity="product",
            status=UNCLASSIFIED_STATUS_PENDING,
        )
        db_session.add(record)
        await db_session.commit()

        monkeypatch.setattr(
            maintenance_lock_service,
            "is_locked",
            unittest.mock.AsyncMock(return_value=True),
        )
        resp = await client.post(
            f"/api/v1/others/{record.id}/reclassify",
            json={
                "entity_type": "product",
                "fields": {**_PRODUCT_PAYLOAD, "name": "Yerba 1kg Reclasificada"},
            },
            headers=auth_headers,
        )
        assert resp.status_code == 423
        assert "mantenimiento" in resp.json()["detail"].lower()

    async def test_post_products_403_for_viewer_even_when_locked(
        self,
        client: AsyncClient,
        viewer_headers: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """IMPORTANT 3 (review): la auth de rol corre ANTES que el guard 423 — un
        VIEWER con el tenant lockeado tiene que ver 403 (sin permiso), no 423
        (que filtraría el estado de mantenimiento a alguien sin acceso de
        escritura)."""
        monkeypatch.setattr(
            maintenance_lock_service,
            "is_locked",
            unittest.mock.AsyncMock(return_value=True),
        )
        resp = await client.post(
            "/api/v1/products", json=_PRODUCT_PAYLOAD, headers=viewer_headers
        )
        assert resp.status_code == 403


@pytest.mark.asyncio
class TestMaintenanceGuard423SalesPurchasesExpenses:
    """F3 review final: gap del guard 423 en venta/compra/gasto manual."""

    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger: Any) -> None:
        with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
            yield

    async def test_post_purchases_manual_423_when_locked(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sup = await _create_supplier_via_api(client, auth_headers, "Distribuidora Norte")
        monkeypatch.setattr(
            maintenance_lock_service,
            "is_locked",
            unittest.mock.AsyncMock(return_value=True),
        )
        payload = {
            "supplier_id": sup,
            "payment_method": "cash",
            "transaction_date": _TODAY,
            "lines": [
                {
                    "name": "Yerba Nueva",
                    "category": "Bebidas",
                    "unit_cost": "1000.00",
                    "quantity": 12,
                    "sale_price_ars": "1500.00",
                }
            ],
        }
        resp = await client.post(
            "/api/v1/purchases/manual", json=payload, headers=auth_headers
        )
        assert resp.status_code == 423
        assert "mantenimiento" in resp.json()["detail"].lower()

    async def test_post_sales_manual_batch_423_when_locked(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        pid = await _create_product_via_api(
            client, auth_headers, "Yerba", stock=10, price="1000.00"
        )
        monkeypatch.setattr(
            maintenance_lock_service,
            "is_locked",
            unittest.mock.AsyncMock(return_value=True),
        )
        payload = {
            "payment_method": "cash",
            "transaction_date": _TODAY,
            "items": [{"product_id": pid, "quantity": 1, "unit_price": "1000.00"}],
        }
        resp = await client.post(
            "/api/v1/sales/manual-batch", json=payload, headers=auth_headers
        )
        assert resp.status_code == 423
        assert "mantenimiento" in resp.json()["detail"].lower()
        # nada se persistió: stock intacto
        r = await client.get(f"/api/v1/products/{pid}", headers=auth_headers)
        assert r.json()["stock_units"] == 10

    async def test_post_sales_bulk_423_when_locked(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            maintenance_lock_service,
            "is_locked",
            unittest.mock.AsyncMock(return_value=True),
        )
        payload = {
            "period_type": "weekly",
            "period_date": _TODAY,
            "total_amount_ars": "50000.00",
        }
        resp = await client.post("/api/v1/sales/bulk", json=payload, headers=auth_headers)
        assert resp.status_code == 423

    async def test_post_expenses_423_when_locked(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            maintenance_lock_service,
            "is_locked",
            unittest.mock.AsyncMock(return_value=True),
        )
        resp = await client.post("/api/v1/expenses", json=_EXPENSE_PAYLOAD, headers=auth_headers)
        assert resp.status_code == 423
        assert "mantenimiento" in resp.json()["detail"].lower()

    async def test_post_purchases_manual_ok_when_not_locked(
        self,
        client: AsyncClient,
        auth_headers: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sup = await _create_supplier_via_api(client, auth_headers, "Distribuidora Sur")
        monkeypatch.setattr(
            maintenance_lock_service,
            "is_locked",
            unittest.mock.AsyncMock(return_value=False),
        )
        payload = {
            "supplier_id": sup,
            "payment_method": "cash",
            "transaction_date": _TODAY,
            "lines": [
                {
                    "name": "Fideos",
                    "category": "Almacen",
                    "unit_cost": "300.00",
                    "quantity": 5,
                    "sale_price_ars": "500.00",
                }
            ],
        }
        resp = await client.post(
            "/api/v1/purchases/manual", json=payload, headers=auth_headers
        )
        assert resp.status_code == 201


@pytest.mark.asyncio
class TestMaintenanceGuardWiring:
    """``acquire_write_lock_shared`` se invoca en los write boundaries."""

    async def test_stock_service_increment_acquires_shared_lock(
        self,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from app.application.services.stock_service import increment_stock

        tid = sample_tenant.tenant_id
        product = await _make_product(db_session, tid, stock_units=0)

        spy = unittest.mock.AsyncMock(
            wraps=maintenance_lock_service.acquire_write_lock_shared
        )
        monkeypatch.setattr(maintenance_lock_service, "acquire_write_lock_shared", spy)

        with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
            await increment_stock(product.id, tid, 5, Decimal("1000"), "src", db_session)

        spy.assert_any_await(db_session, tid)

    async def test_stock_service_decrement_acquires_shared_lock(
        self,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from app.application.services.stock_service import decrement_stock

        tid = sample_tenant.tenant_id
        product = await _make_product(db_session, tid, stock_units=10)

        spy = unittest.mock.AsyncMock(
            wraps=maintenance_lock_service.acquire_write_lock_shared
        )
        monkeypatch.setattr(maintenance_lock_service, "acquire_write_lock_shared", spy)

        with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
            await decrement_stock(product.id, tid, 3, "src", db_session)

        spy.assert_any_await(db_session, tid)

    async def test_product_repository_save_acquires_shared_lock(
        self,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        tid = sample_tenant.tenant_id
        spy = unittest.mock.AsyncMock(
            wraps=maintenance_lock_service.acquire_write_lock_shared
        )
        monkeypatch.setattr(maintenance_lock_service, "acquire_write_lock_shared", spy)

        product = Product(
            id=uuid.uuid4(),
            tenant_id=tid,
            name="Fideos 500g",
            sale_price_ars=Decimal("900"),
            unit_cost_ars=Decimal("500"),
            stock_units=20,
        )
        await ProductRepository(db_session).save(product)

        spy.assert_awaited_once_with(db_session, tid)

    async def test_manual_batch_sale_acquires_shared_lock(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        auth_headers: dict[str, Any],
        monkeypatch: pytest.MonkeyPatch,
        mock_score_trigger: Any,
    ) -> None:
        """F3 review final: create_manual_batch_sale hoisteó el acquire ANTES del
        ``SELECT ... FOR UPDATE`` sobre Product — antes solo se tomaba después,
        dentro de ``decrement_for_sale``."""
        tid = sample_tenant.tenant_id
        pid = await _create_product_via_api(
            client, auth_headers, "Yerba", stock=10, price="1000.00"
        )

        spy = unittest.mock.AsyncMock(
            wraps=maintenance_lock_service.acquire_write_lock_shared
        )
        monkeypatch.setattr(maintenance_lock_service, "acquire_write_lock_shared", spy)

        payload = {
            "payment_method": "cash",
            "transaction_date": _TODAY,
            "items": [{"product_id": pid, "quantity": 1, "unit_price": "1000.00"}],
        }
        with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
            resp = await client.post(
                "/api/v1/sales/manual-batch", json=payload, headers=auth_headers
            )
        assert resp.status_code == 201, resp.text
        spy.assert_any_await(db_session, tid)

    async def test_register_manual_purchase_acquires_shared_lock(
        self,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """F3 review final: register_manual_purchase hoisteó el acquire ANTES del
        ``SELECT ... FOR UPDATE`` sobre productos existentes — antes solo se
        tomaba después, dentro de ``increment_stock``."""
        from app.persistence.models.supplier import Supplier
        from app.schemas.purchase import ManualPurchaseRequest, PurchaseLine

        tid = sample_tenant.tenant_id
        supplier = Supplier(id=uuid.uuid4(), tenant_id=tid, name="Distribuidora Norte")
        db_session.add(supplier)
        await db_session.flush()

        spy = unittest.mock.AsyncMock(
            wraps=maintenance_lock_service.acquire_write_lock_shared
        )
        monkeypatch.setattr(maintenance_lock_service, "acquire_write_lock_shared", spy)

        body = ManualPurchaseRequest(
            supplier_id=supplier.id,
            payment_method="cash",
            transaction_date=date.today(),
            lines=[
                PurchaseLine(
                    name="Yerba Nueva",
                    category="Bebidas",
                    unit_cost=Decimal("1000.00"),
                    quantity=12,
                    sale_price_ars=Decimal("1500.00"),
                )
            ],
        )
        with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
            await purchase_service.register_manual_purchase(
                db_session, tid, body, uuid.uuid4()
            )

        spy.assert_any_await(db_session, tid)
