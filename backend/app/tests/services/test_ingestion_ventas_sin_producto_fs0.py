"""F-S.0 mecanismo 4: una venta con nombre de producto que no resuelve contra
el catálogo se cuenta (`ventas_sin_producto`) y guarda el nombre crudo en
`custom_fields` — nunca se pierde en silencio, nunca se inventa un producto.
Cubre los dos caminos de import: el plano (una sola hoja) y el multi-hoja.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import ingestion_import_service as importer
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry


async def test_venta_sin_producto_se_cuenta_y_guarda_el_nombre_crudo_camino_plano(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "has_ingreso": True,
        "ventas_detectadas": [
            {
                "fecha": "2026-08-01",
                "monto": "1500",
                "articulo": "Producto que no está en ningún catálogo",
            }
        ],
    }

    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        confirmed_fields={"ventas": True},
    )

    assert counts["ventas"] == 1
    assert counts["ventas_sin_producto"] == 1
    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.product_id is None
    assert sale.custom_fields.get("_unlinked_product_name_raw") == (
        "Producto que no está en ningún catálogo"
    )


async def test_venta_sin_producto_se_cuenta_camino_multihoja(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {"context_id": "vta", "entity_type": "sale", "label": "Ventas"},
        ],
        "ventas_detectadas": [
            {
                "fecha": "2026-08-01",
                "monto": "1500",
                "articulo_vendido": "Producto que no está en ningún catálogo",
                "__context__": "vta",
            }
        ],
    }
    context_mappings = {
        "vta": {
            "fecha": "transaction_date",
            "monto": "amount",
            "articulo_vendido": "product_name",
        },
    }

    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        context_mappings=context_mappings,
        context_confirmed={"vta": True},
    )

    assert counts["ventas"] == 1
    assert counts["ventas_sin_producto"] == 1
    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.product_id is None
    assert sale.custom_fields.get("_unlinked_product_name_raw") == (
        "Producto que no está en ningún catálogo"
    )


async def test_venta_sin_nombre_de_producto_no_cuenta_como_sin_producto(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Una venta que NUNCA declaró producto (venta de mostrador, sin columna
    de nombre) no es lo mismo que una que declaró un nombre y no resolvió —
    no hay nada que ofrecer en la cola de vinculación."""
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {"context_id": "vta", "entity_type": "sale", "label": "Ventas"},
        ],
        "ventas_detectadas": [
            {"fecha": "2026-08-01", "monto": "1500", "__context__": "vta"}
        ],
    }
    context_mappings = {
        "vta": {"fecha": "transaction_date", "monto": "amount"},
    }

    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        context_mappings=context_mappings,
        context_confirmed={"vta": True},
    )

    assert counts["ventas"] == 1
    assert counts.get("ventas_sin_producto", 0) == 0


async def test_venta_declarada_solo_por_sku_camino_plano_tambien_se_cuenta(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Mismo caso que el multi-hoja de abajo, pero por el camino plano
    (`column_mappings`, no `context_mappings`)."""
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "has_ingreso": True,
        "ventas_detectadas": [
            {"fecha": "2026-08-01", "monto": "1500", "codigo_interno": "SKU-INEXISTENTE"}
        ],
    }

    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        confirmed_fields={"ventas": True},
        column_mappings={
            "fecha": "transaction_date",
            "monto": "amount",
            "codigo_interno": "sku",
        },
    )

    assert counts["ventas"] == 1
    assert counts["ventas_sin_producto"] == 1
    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.product_id is None
    assert sale.custom_fields.get("_unlinked_product_name_raw") == "SKU-INEXISTENTE"


async def test_venta_declarada_solo_por_sku_sin_columna_de_nombre_tambien_se_cuenta(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Regresión (code review): una hoja puede mapear SÓLO `sku` (sin columna
    de nombre) — F-S.0 mecanismo 1 habilita justo ese caso. Si el sku no
    resuelve, antes esto se perdía en silencio porque el conteo sólo miraba
    el nombre. Ahora también cuenta por sku (o barcode) y usa ESE valor como
    identificador crudo en la cola, ya que es el único dato que la fila trajo."""
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {"context_id": "vta", "entity_type": "sale", "label": "Ventas"},
        ],
        "ventas_detectadas": [
            {
                "fecha": "2026-08-01",
                "monto": "1500",
                "codigo": "SKU-QUE-NO-EXISTE",
                "__context__": "vta",
            }
        ],
    }
    context_mappings = {
        "vta": {"fecha": "transaction_date", "monto": "amount", "codigo": "sku"},
    }

    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        context_mappings=context_mappings,
        context_confirmed={"vta": True},
    )

    assert counts["ventas"] == 1
    assert counts["ventas_sin_producto"] == 1
    sale = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert sale.product_id is None
    assert sale.custom_fields.get("_unlinked_product_name_raw") == "SKU-QUE-NO-EXISTE"
