"""Tests del endpoint fase 1 GET /admin/inventory-integrity/{tenant_id} (SUPERADMIN,
read-only, no persiste nada)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.inventory_movement_origin import SOURCE_CATALOG_INITIAL_STOCK
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.user import User
from app.utils.security import create_access_token, hash_password


def _endpoint(tenant_id: uuid.UUID) -> str:
    return f"/api/v1/admin/inventory-integrity/{tenant_id}"


async def _superadmin_headers(db: AsyncSession, tenant: Tenant) -> dict[str, str]:
    user = User(
        user_id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        email="super-inv@vektor.app",
        full_name="Super Admin",
        password_hash=hash_password("Secure789"),
        role_code="SUPERADMIN",
        is_active=True,
    )
    db.add(user)
    await db.commit()
    token = create_access_token(
        {"sub": str(user.user_id), "tenant_id": str(tenant.tenant_id), "role_code": "SUPERADMIN"}
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_owner_forbidden(
    client: AsyncClient, auth_headers: dict[str, str], sample_tenant: Tenant
) -> None:
    resp = await client.get(_endpoint(sample_tenant.tenant_id), headers=auth_headers)
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_reports_divergence_shaped_like_real_incident(
    client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    headers = await _superadmin_headers(db_session, sample_tenant)

    product = Product(
        id=uuid.uuid4(),
        tenant_id=tid,
        name="Coca Cola 1.5L",
        sale_price_ars=Decimal("2500"),
        unit_cost_ars=Decimal("1500"),
        stock_units=184,
    )
    db_session.add(product)
    await db_session.flush()
    db_session.add(
        InventoryMovement(
            tenant_id=tid,
            product_id=product.id,
            movement_type="adjustment",
            qty=36,
            source_type=SOURCE_CATALOG_INITIAL_STOCK,
        )
    )
    db_session.add(
        InventoryMovement(
            tenant_id=tid,
            product_id=product.id,
            movement_type="purchase",
            qty=217,
            source_type="purchase_import",
        )
    )
    db_session.add(
        SaleEntry(
            tenant_id=tid,
            product_id=product.id,
            amount=Decimal("100"),
            quantity=249,
            transaction_date=datetime(2026, 6, 1),
        )
    )
    await db_session.commit()

    resp = await client.get(_endpoint(tid), headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["checked"] == 1
    assert len(data["divergences"]) == 1
    div = data["divergences"][0]
    assert div["stock_esperado"] == 4
    assert div["diff"] == 180


@pytest.mark.asyncio
async def test_no_products_returns_empty(
    client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    headers = await _superadmin_headers(db_session, sample_tenant)
    resp = await client.get(_endpoint(sample_tenant.tenant_id), headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["checked"] == 0
    assert data["divergences"] == []
