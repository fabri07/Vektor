"""F-S.0 mecanismo 1, end-to-end: una venta con SKU mapeado vincula por código
aunque el nombre de la fila no coincida con el del catálogo (variante de
nombre, error de tipeo, lo que sea) — el código gana sobre el nombre, igual
que ya hace `_resolve_product` para compras/gastos.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import ingestion_import_service as importer
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry


async def test_venta_vincula_por_sku_mapeado_aunque_el_nombre_no_matchee(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    product = Product(
        tenant_id=sample_tenant.tenant_id,
        name="Coca Cola 500ml",
        sku="COCA-500",
        sale_price_ars=Decimal("1500"),
        stock_units=10,
    )
    db_session.add(product)
    await db_session.flush()

    summary = {
        "file_type": "spreadsheet",
        "mapping_contexts": [
            {"context_id": "c1", "entity_type": "sale", "label": "Ventas"},
        ],
        "ventas_detectadas": [
            {
                "fecha": "2026-08-01",
                "monto": "1500",
                # Nombre deliberadamente distinto al del catálogo — sólo el
                # código mapeado puede resolverlo.
                "articulo_vendido": "Gaseosa cola cualquiera",
                "codigo_interno": "COCA-500",
                "__context__": "c1",
            }
        ],
    }
    context_mappings = {
        "c1": {
            "fecha": "transaction_date",
            "monto": "amount",
            "articulo_vendido": "product_name",
            "codigo_interno": "sku",
        }
    }

    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        context_mappings=context_mappings,
        context_confirmed={"c1": True},
    )

    assert counts["ventas"] == 1
    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.product_id == product.id, (
        "la venta tenía que vincular por SKU mapeado, no por nombre"
    )
