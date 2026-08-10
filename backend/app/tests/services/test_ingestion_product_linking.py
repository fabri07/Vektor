"""Mejora B: linking conservador producto/categoría por tokens.

Tras fallar el match exacto por SKU y por nombre normalizado, ``_resolve_product``
intenta un 3er tier: intersección de los sets de productos que comparten los
tokens del nombre de entrada. Acepta SOLO si la intersección tiene exactamente un
id (0 o >1 → abstención, sin inventar).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry


async def _make_product(
    db: AsyncSession, tenant_id: uuid.UUID, name: str, sku: str | None = None
) -> Product:
    product = Product(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name=name,
        sku=sku,
        sale_price_ars=Decimal("100"),
        stock_units=10,
        provenance="REAL",
    )
    db.add(product)
    await db.commit()
    return product


async def test_token_match_links_unique_candidate(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    # Catálogo "Coca-Cola 500ml" debe matchear venta "Coca Cola 500" por tokens
    # (coca, cola, 500) — ni exacto por nombre ni por SKU.
    product = await _make_product(
        db_session, sample_tenant.tenant_id, "Coca-Cola 500ml"
    )

    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "has_venta": True,
        "ventas_detectadas": [
            {"fecha": "2024-01-15", "monto": "1500", "producto": "Coca Cola 500"},
        ],
    }
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"ventas": True}
    )

    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.product_id == product.id


async def test_token_match_ambiguous_leaves_none(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    # Dos productos comparten los tokens "coca cola" → intersección > 1 → abstención.
    await _make_product(db_session, sample_tenant.tenant_id, "Coca Cola 500ml", sku="C500")
    await _make_product(db_session, sample_tenant.tenant_id, "Coca Cola 1500ml", sku="C1500")

    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "has_venta": True,
        "ventas_detectadas": [
            {"fecha": "2024-01-15", "monto": "1500", "producto": "Coca Cola"},
        ],
    }
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"ventas": True}
    )

    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.product_id is None  # ambiguo → no se inventa


async def test_exact_sku_wins_over_tokens(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    # El SKU exacto resuelve a un producto distinto del que matchearía por tokens.
    by_sku_product = await _make_product(
        db_session, sample_tenant.tenant_id, "Galletita surtida", sku="GAL-1"
    )
    await _make_product(db_session, sample_tenant.tenant_id, "Galletita chocolate")

    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "has_venta": True,
        "ventas_detectadas": [
            # nombre apunta a "chocolate" pero el SKU exacto gana.
            {
                "fecha": "2024-01-15",
                "monto": "1500",
                "producto": "Galletita chocolate",
                "sku": "GAL-1",
            },
        ],
    }
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"ventas": True}
    )

    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.product_id == by_sku_product.id


async def test_no_catalog_leaves_none(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "has_venta": True,
        "ventas_detectadas": [
            {"fecha": "2024-01-15", "monto": "1500", "producto": "Algo Inexistente"},
        ],
    }
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"ventas": True}
    )

    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.product_id is None


def test_resolve_product_unit_helper() -> None:
    """Unit: tier 3 token-match aislado (sin DB)."""
    pid = uuid.uuid4()
    by_token = {"coca": {pid}, "cola": {pid}, "500": {pid}}
    # Único candidato en la intersección → resuelve.
    assert (
        importer._resolve_product({}, {}, "Coca Cola 500", None, by_token) == pid
    )
    # Token que no comparte ningún producto → sin candidatos → None.
    assert importer._resolve_product({}, {}, "Sprite Lima", None, by_token) is None
    # Sin by_token → no degrada (legacy).
    assert importer._resolve_product({}, {}, "Coca Cola 500", None) is None
