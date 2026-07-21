"""Tests para reclasificar un registro de "Otros" como producto (F2-T2b).

Cubre la extensión del endpoint `POST /others/{id}/reclassify` con
`entity_type="product"`:
  - sin `target_product_id`: comportamiento actual (crear producto nuevo),
    ahora con el chequeo 409 por identidad duplicada (sku/barcode).
  - con `target_product_id`: vincula el record a un producto EXISTENTE del
    tenant en vez de crear uno nuevo, re-validando el id contra el tenant
    (nunca confiar en el id del cliente). Aplica price/stock opcionales.
  - `target_product_id` de otro tenant / inexistente / inactivo → 422
    `INVALID_TARGET_PRODUCT`, el record queda PENDING (no se toca nada).
"""

import uuid
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.unclassified_record import (
    UNCLASSIFIED_STATUS_IMPORTED,
    UNCLASSIFIED_STATUS_PENDING,
    UnclassifiedRecord,
)

_PRODUCT_PAYLOAD = {
    "name": "Coca-Cola 500ml",
    "category": "bebidas",
    "unit_cost_ars": "80.00",
    "sale_price_ars": "150.00",
    "stock_units": 50,
}


async def _make_pending_record(
    db_session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    status: str = UNCLASSIFIED_STATUS_PENDING,
) -> UnclassifiedRecord:
    record = UnclassifiedRecord(
        tenant_id=tenant_id,
        source="ingestion",
        context_label="Hoja 1",
        headers=["nombre", "precio"],
        row_data={"nombre": "Coca-Cola 500ml", "precio": "150"},
        suggested_entity="product",
        status=status,
    )
    db_session.add(record)
    await db_session.commit()
    return record


async def _make_active_product(
    db_session: AsyncSession, tenant_id: uuid.UUID, **overrides: Any
) -> Product:
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "tenant_id": tenant_id,
        "name": "Producto existente",
        "sale_price_ars": Decimal("100.00"),
        "unit_cost_ars": Decimal("50.00"),
        "stock_units": 10,
        "provenance": "REAL",
    }
    defaults.update(overrides)
    product = Product(**defaults)
    db_session.add(product)
    await db_session.commit()
    await db_session.refresh(product)
    return product


@pytest.mark.asyncio
class TestReclassifyAsProductLinkExisting:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_reclassify_links_to_existing_candidate(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        auth_headers: dict[str, Any],
        sample_tenant: Tenant,
    ) -> None:
        target = await _make_active_product(db_session, sample_tenant.tenant_id)
        record = await _make_pending_record(db_session, sample_tenant.tenant_id)

        resp = await client.post(
            f"/api/v1/others/{record.id}/reclassify",
            json={
                "entity_type": "product",
                "fields": {},
                "target_product_id": str(target.id),
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200

        # No se creó un producto nuevo (sigue habiendo solo 1 en el tenant).
        products = (
            (
                await db_session.execute(
                    select(Product).where(Product.tenant_id == sample_tenant.tenant_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(products) == 1
        assert products[0].id == target.id

        await db_session.refresh(record)
        assert record.status == UNCLASSIFIED_STATUS_IMPORTED
        assert record.resolved_at is not None

    async def test_reclassify_link_rejects_stock_without_ledger(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        auth_headers: dict[str, Any],
        sample_tenant: Tenant,
    ) -> None:
        target = await _make_active_product(db_session, sample_tenant.tenant_id)
        record = await _make_pending_record(db_session, sample_tenant.tenant_id)

        resp = await client.post(
            f"/api/v1/others/{record.id}/reclassify",
            json={
                "entity_type": "product",
                "fields": {
                    "sale_price_ars": "199.00",
                    "unit_cost_ars": "120.00",
                    "stock_units": 25,
                },
                "target_product_id": str(target.id),
            },
            headers=auth_headers,
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "STOCK_VIA_LINK_FORBIDDEN"

        await db_session.refresh(target)
        assert target.sale_price_ars == Decimal("100.00")
        assert target.unit_cost_ars == Decimal("50.00")
        assert target.stock_units == 10
        movements = (
            await db_session.execute(
                select(InventoryMovement).where(InventoryMovement.product_id == target.id)
            )
        ).scalars().all()
        assert movements == []

    async def test_reclassify_link_applies_price_cost_and_completes_product(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        auth_headers: dict[str, Any],
        sample_tenant: Tenant,
    ) -> None:
        target = await _make_active_product(
            db_session,
            sample_tenant.tenant_id,
            sale_price_ars=Decimal("0"),
            unit_cost_ars=None,
            requires_completion=True,
        )
        record = await _make_pending_record(db_session, sample_tenant.tenant_id)
        resp = await client.post(
            f"/api/v1/others/{record.id}/reclassify",
            json={
                "entity_type": "product",
                "fields": {"sale_price_ars": "199.00", "unit_cost_ars": "120.00"},
                "target_product_id": str(target.id),
            },
            headers=auth_headers,
        )
        assert resp.status_code == 200
        await db_session.refresh(target)
        assert target.requires_completion is False

    async def test_reclassify_target_from_other_tenant_422(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        auth_headers: dict[str, Any],
        second_auth_headers: dict[str, Any],
        sample_tenant: Tenant,
    ) -> None:
        # Crear un tenant "B" real vía second_auth_headers: necesitamos su tenant_id
        # para armar el producto ajeno. Lo más simple: crear el producto vía la API
        # del segundo tenant y usar el id devuelto.
        create_resp = await client.post(
            "/api/v1/products", json=_PRODUCT_PAYLOAD, headers=second_auth_headers
        )
        assert create_resp.status_code == 201
        other_tenant_product_id = create_resp.json()["id"]

        record = await _make_pending_record(db_session, sample_tenant.tenant_id)

        resp = await client.post(
            f"/api/v1/others/{record.id}/reclassify",
            json={
                "entity_type": "product",
                "fields": {},
                "target_product_id": other_tenant_product_id,
            },
            headers=auth_headers,
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "INVALID_TARGET_PRODUCT"

        await db_session.refresh(record)
        assert record.status == UNCLASSIFIED_STATUS_PENDING

    async def test_reclassify_target_nonexistent_422(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        auth_headers: dict[str, Any],
        sample_tenant: Tenant,
    ) -> None:
        record = await _make_pending_record(db_session, sample_tenant.tenant_id)

        resp = await client.post(
            f"/api/v1/others/{record.id}/reclassify",
            json={
                "entity_type": "product",
                "fields": {},
                "target_product_id": str(uuid.uuid4()),
            },
            headers=auth_headers,
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "INVALID_TARGET_PRODUCT"

        await db_session.refresh(record)
        assert record.status == UNCLASSIFIED_STATUS_PENDING

    async def test_reclassify_target_inactive_422(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        auth_headers: dict[str, Any],
        sample_tenant: Tenant,
    ) -> None:
        target = await _make_active_product(
            db_session,
            sample_tenant.tenant_id,
            is_active=False,
        )
        record = await _make_pending_record(db_session, sample_tenant.tenant_id)

        resp = await client.post(
            f"/api/v1/others/{record.id}/reclassify",
            json={
                "entity_type": "product",
                "fields": {},
                "target_product_id": str(target.id),
            },
            headers=auth_headers,
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "INVALID_TARGET_PRODUCT"

        await db_session.refresh(record)
        assert record.status == UNCLASSIFIED_STATUS_PENDING


@pytest.mark.asyncio
class TestReclassifyAsProductCreateNewDuplicate409:
    @pytest.fixture(autouse=True)
    def patch_celery(self, mock_score_trigger):
        pass

    async def test_reclassify_create_new_duplicate_sku_409(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        auth_headers: dict[str, Any],
        sample_tenant: Tenant,
    ) -> None:
        await _make_active_product(db_session, sample_tenant.tenant_id, sku="SKU-DUP")
        record = await _make_pending_record(db_session, sample_tenant.tenant_id)

        resp = await client.post(
            f"/api/v1/others/{record.id}/reclassify",
            json={
                "entity_type": "product",
                "fields": {**_PRODUCT_PAYLOAD, "sku": "sku-dup"},
            },
            headers=auth_headers,
        )
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "DUPLICATE_PRODUCT_IDENTITY"

        await db_session.refresh(record)
        assert record.status == UNCLASSIFIED_STATUS_PENDING


@pytest.mark.asyncio
class TestOthersExposesMatchCandidates:
    """F2-T2b (Finding 5): GET /others expone ``match_candidates`` para que el
    frontend ofrezca vincular a un producto existente en vez de crear duplicado."""

    async def test_list_includes_match_candidates(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        auth_headers: dict[str, Any],
        sample_tenant: Tenant,
    ) -> None:
        candidates = [
            {"id": str(uuid.uuid4()), "matched_by": ["name"], "name": "Agua",
             "sku": None, "barcode": None},
            {"id": str(uuid.uuid4()), "matched_by": ["name"], "name": "Agua",
             "sku": None, "barcode": None},
        ]
        record = UnclassifiedRecord(
            tenant_id=sample_tenant.tenant_id,
            source="ingestion",
            context_label="Producto ambiguo: coincide con 2 productos activos",
            headers=["producto"],
            row_data={"producto": "Agua"},
            suggested_entity="product",
            match_candidates=candidates,
            status=UNCLASSIFIED_STATUS_PENDING,
        )
        db_session.add(record)
        await db_session.commit()

        resp = await client.get("/api/v1/others", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["match_candidates"] is not None
        assert len(body[0]["match_candidates"]) == 2
        assert {c["id"] for c in body[0]["match_candidates"]} == {
            candidates[0]["id"], candidates[1]["id"]
        }
