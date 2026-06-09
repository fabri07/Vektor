"""FASE 4: tests del endpoint de resumen económico analítico."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry

_FROM = "2026-01-01"
_TO = "2026-01-31"
_IN_RANGE = datetime(2026, 1, 15, 12, 0)


async def _add_sale(db: AsyncSession, tenant_id: Any, amount: str) -> None:
    db.add(
        SaleEntry(
            tenant_id=tenant_id,
            amount=Decimal(amount),
            quantity=1,
            transaction_date=_IN_RANGE,
            payment_method="cash",
            provenance="REAL",
        )
    )


async def _add_expense(db: AsyncSession, tenant_id: Any, amount: str) -> None:
    db.add(
        ExpenseEntry(
            tenant_id=tenant_id,
            amount=Decimal(amount),
            category="importado",
            transaction_date=_IN_RANGE,
            description="gasto",
            payment_method="transfer",
            provenance="REAL",
        )
    )


async def _add_product(
    db: AsyncSession, tenant_id: Any, name: str, units: int, cost: str | None
) -> None:
    db.add(
        Product(
            tenant_id=tenant_id,
            name=name,
            sale_price_ars=Decimal("100"),
            unit_cost_ars=Decimal(cost) if cost is not None else None,
            stock_units=units,
            is_active=True,
            provenance="REAL",
        )
    )


@pytest.mark.asyncio
async def test_empty_range_has_no_data(
    client: AsyncClient, auth_headers: dict[str, Any]
) -> None:
    resp = await client.get(
        "/api/v1/economic-summary",
        params={"from_date": _FROM, "to_date": _TO},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["has_data"] is False
    assert data["total_income_ars"] == 0
    assert data["total_expenses_ars"] == 0
    assert data["net_result_ars"] == 0
    assert data["stock_value_ars"] == 0


@pytest.mark.asyncio
async def test_complete_data_summary(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    await _add_sale(db_session, sample_tenant.tenant_id, "10000")
    await _add_sale(db_session, sample_tenant.tenant_id, "5000")
    await _add_expense(db_session, sample_tenant.tenant_id, "3000")
    await _add_product(db_session, sample_tenant.tenant_id, "Yerba", 10, "200")  # 2000
    await db_session.commit()

    resp = await client.get(
        "/api/v1/economic-summary",
        params={"from_date": _FROM, "to_date": _TO},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["has_data"] is True
    assert data["total_income_ars"] == 15000
    assert data["total_expenses_ars"] == 3000
    assert data["net_result_ars"] == 12000
    assert data["stock_value_ars"] == 2000  # 10 × 200
    assert data["missing_cost_count"] == 0
    assert data["missing_cost_stock_units"] == 0


@pytest.mark.asyncio
async def test_products_without_cost_reported_not_valued(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    await _add_product(db_session, sample_tenant.tenant_id, "Con costo", 5, "100")  # 500
    await _add_product(db_session, sample_tenant.tenant_id, "Sin costo", 8, None)
    await db_session.commit()

    resp = await client.get(
        "/api/v1/economic-summary",
        params={"from_date": _FROM, "to_date": _TO},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["stock_value_ars"] == 500  # solo el producto con costo
    assert data["missing_cost_count"] == 1
    assert data["missing_cost_stock_units"] == 8
    assert data["has_data"] is True


@pytest.mark.asyncio
async def test_from_after_to_returns_422(
    client: AsyncClient, auth_headers: dict[str, Any]
) -> None:
    resp = await client.get(
        "/api/v1/economic-summary",
        params={"from_date": _TO, "to_date": _FROM},
        headers=auth_headers,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_requires_auth(client: AsyncClient) -> None:
    resp = await client.get(
        "/api/v1/economic-summary",
        params={"from_date": _FROM, "to_date": _TO},
    )
    assert resp.status_code in (401, 403)
