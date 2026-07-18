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
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import maintenance_lock_service
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.repositories.product_repository import ProductRepository

_PRODUCT_PAYLOAD = {
    "name": "Coca-Cola 500ml",
    "category": "bebidas",
    "unit_cost_ars": "80.00",
    "sale_price_ars": "150.00",
    "stock_units": 50,
    "low_stock_threshold_units": 10,
}


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
