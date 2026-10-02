"""Un archivo habla en la unidad de VENTA de un producto por peso (cierre de B3).

"5" de queso en una lista de stock son 5 kg, o sea 5000 g de stock: el stock
vive en unidades base. Sin la conversión, el archivo dejaba 5 gramos.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.ingestion_import_service import insert_confirmed_data
from app.persistence.models.file import UploadedFile
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant


async def _archivo(db: AsyncSession, tid: uuid.UUID) -> uuid.UUID:
    up = UploadedFile(
        id=uuid.uuid4(), tenant_id=tid, original_filename="stock.csv", s3_key="k",
        content_type="text/csv", size_bytes=10, purpose="ingestion",
    )
    db.add(up)
    await db.flush()
    return up.id


async def test_la_lista_de_stock_dice_kilos(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    queso = Product(
        tenant_id=tid, name="Queso", sale_price_ars=Decimal("10000"),
        unit_cost_ars=Decimal("800"), stock_units=0,
        sale_unit="gram", base_units_per_sale_unit=1000,
    )
    yerba = Product(
        tenant_id=tid, name="Yerba", sale_price_ars=Decimal("100"), stock_units=0,
    )
    db_session.add_all([queso, yerba])
    await db_session.flush()

    await insert_confirmed_data(
        db_session,
        tid,
        {
            "file_type": "spreadsheet",
            "inferred_type": "stock",
            "has_producto": True,
            "stock_detectado": [
                {"producto": "Queso", "stock": "5"},
                {"producto": "Yerba", "stock": "5"},
            ],
        },
        {"productos": True},
        column_mappings={"producto": "name", "stock": "stock_units"},
        uploaded_file_id=await _archivo(db_session, tid),
    )
    await db_session.flush()
    await db_session.refresh(queso)
    await db_session.refresh(yerba)
    assert queso.stock_units == 5000
    # Un producto por unidad no cambia.
    assert yerba.stock_units == 5


async def test_libro_de_varias_hojas_dice_kilos_en_stock_y_ventas(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Camino por contexto: la hoja de productos fija el stock y la de ventas
    guarda la cantidad, las dos en la unidad de venta del archivo."""
    from sqlalchemy import select

    from app.persistence.models.transaction import SaleEntry

    tid = sample_tenant.tenant_id
    queso = Product(
        tenant_id=tid, name="Queso", sale_price_ars=Decimal("10000"),
        unit_cost_ars=Decimal("800"), stock_units=0,
        sale_unit="gram", base_units_per_sale_unit=1000,
    )
    db_session.add(queso)
    await db_session.flush()

    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {"context_id": "sheet:Productos", "entity_type": "product", "source_kind": "sheet",
             "headers": ["producto", "stock"], "fields": None, "preview_rows": [],
             "row_count": 1},
            {"context_id": "sheet:Ventas", "entity_type": "sale", "source_kind": "sheet",
             "headers": ["fecha", "producto", "cantidad", "monto"], "fields": None,
             "preview_rows": [], "row_count": 1},
        ],
        "stock_detectado": [
            {"producto": "Queso", "stock": "5", "__context__": "sheet:Productos"}
        ],
        "ventas_detectadas": [
            {"fecha": "2024-01-15", "producto": "Queso", "cantidad": "2", "monto": "20000",
             "__context__": "sheet:Ventas"}
        ],
        "gastos_detectados": [],
    }
    await insert_confirmed_data(
        db_session,
        tid,
        summary,
        {"productos": True, "ventas": True},
        context_mappings={
            "sheet:Productos": {"producto": "name", "stock": "stock_units"},
            "sheet:Ventas": {"fecha": "transaction_date", "producto": "product_name",
                             "cantidad": "quantity", "monto": "amount"},
        },
        context_confirmed={"sheet:Productos": True, "sheet:Ventas": True},
        uploaded_file_id=await _archivo(db_session, tid),
    )
    await db_session.flush()
    await db_session.refresh(queso)
    assert queso.stock_units == 5000
    venta = (await db_session.execute(select(SaleEntry))).scalar_one()
    assert venta.product_id == queso.id
    assert venta.quantity == 2000
    assert venta.amount == Decimal("20000")
