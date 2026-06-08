"""FASE 3 (B2): productos auto-creados incompletos (requires_completion).

Cuando un import auto-crea un producto al que le falta precio o costo, se marca
`requires_completion=True` para que el usuario lo complete. Un producto importado
con precio y costo queda completo.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant


def _stock_summary(rows: list[dict[str, object]]) -> dict[str, object]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "stock",
        "has_producto": True,
        "stock_detectado": rows,
    }


@pytest.mark.asyncio
async def test_complete_product_not_flagged(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    summary = _stock_summary(
        [{"producto": "Yerba 1kg", "precio": "2500", "costo": "1500", "stock": "10"}]
    )
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )
    product = (await db_session.execute(select(Product))).scalar_one()
    assert product.requires_completion is False


@pytest.mark.asyncio
async def test_autocreated_without_cost_is_flagged(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    summary = _stock_summary([{"producto": "Sin costo", "precio": "2500", "stock": "5"}])
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )
    product = (await db_session.execute(select(Product))).scalar_one()
    assert product.requires_completion is True  # falta unit_cost


@pytest.mark.asyncio
async def test_autocreated_without_price_is_flagged(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    summary = _stock_summary([{"producto": "Sin precio", "costo": "800", "stock": "5"}])
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )
    product = (await db_session.execute(select(Product))).scalar_one()
    assert product.requires_completion is True  # precio default 0 → incompleto
    assert product.sale_price_ars == Decimal("0")
    # El cierre del flag vía PATCH se prueba en test_products.py
    # (test_patch_completing_data_clears_requires_completion).
