"""Mejora C: no tomar totales de línea como costo unitario al crear productos.

Una columna de "costo total" (o la columna del monto/precio) NO debe escribirse
como ``unit_cost_ars`` del producto. Sin columna inequívoca de costo unitario, el
producto nace ``unit_cost_ars=None`` + ``requires_completion=True``. Con
``costo_unitario`` explícito, el costo se captura.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant


@pytest.mark.asyncio
async def test_costo_total_not_used_as_unit_cost(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "stock",
        "has_producto": True,
        "stock_detectado": [
            {
                "producto": "Yerba Mate 1kg",
                "precio": "3000",
                "costo total": "20000",
                "stock": "10",
            },
        ],
    }
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )

    product = (await db_session.execute(select(Product))).scalar_one()
    assert product.name == "Yerba Mate 1kg"
    assert product.unit_cost_ars is None  # "costo total" NO es costo unitario
    assert product.requires_completion is True  # falta costo → completar


@pytest.mark.asyncio
async def test_costo_unitario_captured(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "stock",
        "has_producto": True,
        "stock_detectado": [
            {
                "producto": "Azucar 1kg",
                "precio_venta": "3000",
                "costo_unitario": "1800",
                "stock": "5",
            },
        ],
    }
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )

    product = (await db_session.execute(select(Product))).scalar_one()
    assert product.name == "Azucar 1kg"
    assert product.unit_cost_ars is not None
    assert int(product.unit_cost_ars) == 1800
    # precio + costo presentes → no requiere completar.
    assert product.requires_completion is False


@pytest.mark.asyncio
async def test_no_cost_column_leaves_none_and_requires_completion(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "stock",
        "has_producto": True,
        "stock_detectado": [
            {"producto": "Fideos 500g", "precio": "1200", "stock": "8"},
        ],
    }
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )

    product = (await db_session.execute(select(Product))).scalar_one()
    assert product.unit_cost_ars is None
    assert product.requires_completion is True


@pytest.mark.asyncio
async def test_asteria_precio_compra_venta_disambiguation(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Caso real ASTERIA: catálogo con "Precio de compra" (costo) + "Precio de
    lista" + "Precio de venta final". El costo unitario sale de "Precio de compra"
    y el precio de venta de "Precio de venta final" (NO de compra ni de lista)."""
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "stock",
        "has_producto": True,
        "stock_detectado": [
            {
                "Tienda": "Sodimac",
                "Productos": "Lámpara colgante",
                "Stock": "4",
                "Precio de compra": "10000",
                "Precio de lista": "18000",
                "Precio de venta final": "20000",
            },
        ],
    }
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )

    product = (await db_session.execute(select(Product))).scalar_one()
    assert product.name == "Lámpara colgante"
    assert product.unit_cost_ars is not None
    assert int(product.unit_cost_ars) == 10000  # "Precio de compra" = costo
    assert product.sale_price_ars is not None
    assert int(product.sale_price_ars) == 20000  # "Precio de venta final", NO compra/lista
    assert product.requires_completion is False  # costo + precio presentes


def test_resolve_sale_price_col_helper() -> None:
    """Unit: prioridad venta > lista > precio, excluyendo compra/costo (sin DB)."""
    headers = ["Tienda", "Productos", "Precio de compra", "Precio de lista", "Precio de venta final"]
    # "venta" gana sobre "lista" y sobre el genérico "precio" de compra.
    assert importer._resolve_sale_price_col(headers, "Precio de compra") == "Precio de venta final"
    # Sin "venta": cae a "lista".
    assert (
        importer._resolve_sale_price_col(
            ["Productos", "Precio de compra", "Precio de lista"], "Precio de compra"
        )
        == "Precio de lista"
    )
    # Genérico "precio" nunca toma la columna de compra.
    assert (
        importer._resolve_sale_price_col(["Productos", "Precio de compra"], "Precio de compra")
        is None
    )


def test_resolve_unit_cost_col_helper() -> None:
    """Unit: narrow-first + exclusión de monto/total (sin DB)."""
    # Inequívoca gana.
    assert (
        importer._resolve_unit_cost_col(
            ["producto", "precio", "costo_unitario"], "precio", "precio"
        )
        == "costo_unitario"
    )
    # "costo total" nunca.
    assert (
        importer._resolve_unit_cost_col(
            ["producto", "precio", "costo total"], "precio", "precio"
        )
        is None
    )
    # broad "costo" se acepta si no es el monto/precio.
    assert (
        importer._resolve_unit_cost_col(["producto", "precio", "costo"], "precio", "precio")
        == "costo"
    )
    # broad "costo" que ES la columna de monto → no se usa.
    assert importer._resolve_unit_cost_col(["producto", "costo"], "costo", None) is None
