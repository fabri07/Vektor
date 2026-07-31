"""Los tres precios de un producto son conceptos distintos y coexisten.

Antes de esto el modelo tenía dos columnas de precio y el importador metía tres
conceptos en la misma: costo de compra, sugerido de lista y precio de venta
terminaban peleando por ``sale_price_ars``, y ganaba el que apareciera primero
en el orden del Excel (incidente ASTERIA, 2026-07-31).

La separación que se verifica acá:

* ``Product.unit_cost_ars``  — costo unitario vigente o de referencia.
* ``Product.list_price_ars`` — sugerido por proveedor/lista (informativo).
* ``Product.sale_price_ars`` — precio de venta vigente que configuró el negocio.
* ``SaleEntry.unit_price``   — precio REALMENTE vendido en esa transacción.

La última es la que faltaba: el precio histórico no puede vivir solo en
``products``, porque cambia por descuento, fecha, canal o cliente.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry


@pytest.mark.asyncio
async def test_los_tres_precios_de_producto_se_guardan_por_separado(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El caso ASTERIA: una hoja con las tres columnas de precio a la vez.

    Antes las tres caían en ``sale_price_ars`` y dos se perdían en silencio.
    """
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "stock",
        "has_producto": True,
        "stock_detectado": [
            {
                "Productos": "Vela aromática 200g",
                "Precio de compra": "1200",
                "Precio de lista": "2400",
                "Precio de venta final": "2100",
                "Stock": "10",
            }
        ],
    }
    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"productos": True},
        column_mappings={
            "Productos": "name",
            "Precio de compra": "unit_cost_ars",
            "Precio de lista": "list_price_ars",
            "Precio de venta final": "sale_price_ars",
            "Stock": "stock_units",
        },
    )

    assert counts["productos"] == 1
    prod = (await db_session.execute(select(Product))).scalars().one()
    assert prod.unit_cost_ars == Decimal("1200.00")
    assert prod.list_price_ars == Decimal("2400.00")
    assert prod.sale_price_ars == Decimal("2100.00")
    # El margen se sigue calculando con vigente − costo; el sugerido no participa.
    assert prod.sale_price_ars > prod.unit_cost_ars


@pytest.mark.asyncio
async def test_precio_de_lista_sin_mapear_queda_null(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Sin mapeo explícito NO se adivina desde un header parecido: queda NULL.

    Es la regla no-invention aplicada al mapeo — un header que "suena a" lista no
    alcanza para afirmar que ese número es el sugerido del proveedor.
    """
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "stock",
        "has_producto": True,
        "stock_detectado": [
            {"Productos": "Sahumerio lavanda", "Precio de venta final": "900", "Stock": "5"}
        ],
    }
    await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"productos": True},
        column_mappings={
            "Productos": "name",
            "Precio de venta final": "sale_price_ars",
            "Stock": "stock_units",
        },
    )

    prod = (await db_session.execute(select(Product))).scalars().one()
    assert prod.sale_price_ars == Decimal("900.00")
    assert prod.list_price_ars is None


@pytest.mark.asyncio
async def test_venta_guarda_el_precio_realmente_vendido(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "ventas_detectadas": [
            {
                "fecha": "2026-07-15",
                "detalle": "Vela aromática 200g",
                "monto": "4200",
                "cantidad": "2",
                "precio unitario": "2100",
            }
        ],
    }
    await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"ventas": True},
        column_mappings={
            "fecha": "transaction_date",
            "detalle": "product_name",
            "monto": "amount",
            "cantidad": "quantity",
            "precio unitario": "unit_price",
        },
    )

    venta = (await db_session.execute(select(SaleEntry))).scalars().one()
    assert venta.amount == Decimal("4200.00")
    assert venta.quantity == 2
    assert venta.unit_price == Decimal("2100.00")


@pytest.mark.asyncio
async def test_unit_price_nunca_se_deriva_de_amount_sobre_quantity(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Sin columna de precio unitario, ``unit_price`` queda NULL.

    4200/2 = 2100 sería un número plausible, pero en una fila histórica no se
    sabe si el monto es unitario o total: derivarlo es inventar precisión.
    """
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "ventas_detectadas": [
            {"fecha": "2026-07-15", "detalle": "Vela", "monto": "4200", "cantidad": "2"}
        ],
    }
    await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"ventas": True},
        column_mappings={
            "fecha": "transaction_date",
            "detalle": "product_name",
            "monto": "amount",
            "cantidad": "quantity",
        },
    )

    venta = (await db_session.execute(select(SaleEntry))).scalars().one()
    assert venta.amount == Decimal("4200.00")
    assert venta.quantity == 2
    assert venta.unit_price is None
