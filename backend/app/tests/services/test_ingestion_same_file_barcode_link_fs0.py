"""F-S.0 mecanismo 2 (gap fix): un producto creado por una hoja de catálogo
tiene que quedar vinculable por BARCODE para las ventas del MISMO archivo, no
solo por sku/nombre (F-H1 ya lo hacía para esos dos, ver
`_register_product_transaction_indexes`). Sin esto, una venta que declara el
barcode de un producto recién creado por el catálogo adjunto no resuelve hasta
la corrida SIGUIENTE — justo el caso que F-S.0 existe para arreglar.

Incluye el caso de orden físico INVERSO (ventas antes que catálogo en el
archivo): la garantía same-file depende de `_orden_de_pasada`
(`product: 0, expense: 1, sale: 2`), no del orden en que las hojas aparecen
en el Excel — con el orden al revés, si la garantía dependiera del orden
físico, este test fallaría y el feliz no lo mostraría.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import ingestion_import_service as importer
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry


async def test_venta_vincula_por_barcode_de_producto_creado_en_el_mismo_archivo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {"context_id": "cat", "entity_type": "product", "label": "Catálogo"},
            {"context_id": "vta", "entity_type": "sale", "label": "Ventas"},
        ],
        "stock_detectado": [
            {
                "nombre": "Coca Cola 500ml",
                "cod_barras": "7791234567890",
                "precio": "1500",
                "stock": "10",
                "__context__": "cat",
            }
        ],
        "ventas_detectadas": [
            {
                "fecha": "2026-08-01",
                "monto": "1500",
                # Nombre deliberadamente distinto: solo el barcode puede
                # resolverlo, y el producto NO existía antes de esta corrida.
                "articulo_vendido": "Gaseosa sin marca",
                "cod_barras_venta": "7791234567890",
                "__context__": "vta",
            }
        ],
    }
    context_mappings = {
        "cat": {
            "nombre": "name",
            "cod_barras": "barcode",
            "precio": "sale_price_ars",
            "stock": "stock_units",
        },
        "vta": {
            "fecha": "transaction_date",
            "monto": "amount",
            "articulo_vendido": "product_name",
            "cod_barras_venta": "barcode",
        },
    }

    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        context_mappings=context_mappings,
        context_confirmed={"cat": True, "vta": True},
    )

    assert counts["productos"] == 1
    assert counts["ventas"] == 1
    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.product_id is not None, (
        "la venta tenía que vincular por barcode contra el producto que el "
        "catálogo del MISMO archivo acaba de crear"
    )


async def test_vincula_por_barcode_aunque_la_hoja_de_ventas_venga_antes_en_el_archivo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Mismo escenario, pero declarando primero el contexto de ventas y
    después el de catálogo en `mapping_contexts` — la garantía same-file la
    da `_orden_de_pasada` (product antes que sale, SIEMPRE), no el orden de
    aparición de las hojas en el archivo."""
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {"context_id": "vta", "entity_type": "sale", "label": "Ventas"},
            {"context_id": "cat", "entity_type": "product", "label": "Catálogo"},
        ],
        "ventas_detectadas": [
            {
                "fecha": "2026-08-01",
                "monto": "1500",
                "articulo_vendido": "Gaseosa sin marca",
                "cod_barras_venta": "7791234567890",
                "__context__": "vta",
            }
        ],
        "stock_detectado": [
            {
                "nombre": "Coca Cola 500ml",
                "cod_barras": "7791234567890",
                "precio": "1500",
                "stock": "10",
                "__context__": "cat",
            }
        ],
    }
    context_mappings = {
        "vta": {
            "fecha": "transaction_date",
            "monto": "amount",
            "articulo_vendido": "product_name",
            "cod_barras_venta": "barcode",
        },
        "cat": {
            "nombre": "name",
            "cod_barras": "barcode",
            "precio": "sale_price_ars",
            "stock": "stock_units",
        },
    }

    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        context_mappings=context_mappings,
        context_confirmed={"vta": True, "cat": True},
    )

    assert counts["productos"] == 1
    assert counts["ventas"] == 1
    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.product_id is not None, (
        "el orden físico de las hojas en el archivo no puede afectar la "
        "garantía same-file — la decide _orden_de_pasada, no el Excel"
    )
