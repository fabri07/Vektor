"""El gasto de mercadería de un stock de catálogo tratado como COMPRA.

Lo arma ``armar_gasto_de_stock_de_catalogo`` y lo usan la importación y la mejora
``compra_catalogo_sin_costo``. Antes de extraerlo, ningún test fijaba su forma (cambiar
el medio de pago por defecto no hacía fallar nada): estos la fijan, directo y por el
camino real de la importación.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.application.services.gasto_de_stock_de_catalogo import (
    MEDIO_DE_PAGO_POR_DEFECTO,
    armar_gasto_de_stock_de_catalogo,
)
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry

_CTX = "sheet:compras"


def test_el_gasto_armado_es_cogs_ligado_al_producto_y_a_la_fila() -> None:
    tid, pid, archivo = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    fecha = datetime(2026, 11, 14)

    g = armar_gasto_de_stock_de_catalogo(
        tid,
        product_id=pid,
        product_name="Camisa lino beige M",
        amount=Decimal("12345.67"),
        tx_date=fecha,
        source_upload_id=archivo,
        source_row_ref="a" * 64,
    )

    assert (g.tenant_id, g.product_id, g.source_upload_id) == (tid, pid, archivo)
    assert g.amount == Decimal("12345.67")
    assert (g.category, g.expense_type, g.provenance) == ("INVENTORY", "COGS", "REAL")
    assert g.payment_method == MEDIO_DE_PAGO_POR_DEFECTO == "transfer"
    assert g.description == "Stock inicial: Camisa lino beige M"
    assert g.transaction_date == fecha
    assert g.source_row_ref == "a" * 64
    assert g.is_recurring is False


def test_el_medio_de_pago_se_puede_elegir_y_la_fila_de_origen_es_opcional() -> None:
    g = armar_gasto_de_stock_de_catalogo(
        uuid.uuid4(),
        product_id=uuid.uuid4(),
        product_name="X" * 600,
        amount=Decimal("10"),
        tx_date=datetime(2026, 11, 14),
        source_upload_id=None,
        source_row_ref=None,
        payment_method="cash",
    )

    assert g.payment_method == "cash"
    assert g.source_row_ref is None
    assert len(g.description or "") == 500  # mismo tope que la importación


def _summary() -> dict[str, Any]:
    filas = [
        {"producto": "Sahumerio lavanda", "cantidad": "5", "costo": "800", "__context__": _CTX}
    ]
    return {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "has_producto": True,
        "stock_detectado": filas,
        "mapping_contexts": [
            {
                "context_id": _CTX,
                "label": "compras",
                "source_kind": "sheet",
                "entity_type": "product",
                "headers": ["producto", "cantidad", "costo"],
                "fields": None,
                "preview_rows": filas,
                "row_count": 1,
            }
        ],
    }


async def test_la_importacion_de_una_compra_de_catalogo_crea_el_mismo_gasto(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    archivo = uuid.uuid4()
    await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _summary(),
        {"productos": True},
        context_mappings={
            _CTX: {"producto": "name", "cantidad": "stock_units", "costo": "unit_cost_ars"}
        },
        context_confirmed={_CTX: True},
        stock_treatment={_CTX: "purchase"},
        uploaded_file_id=archivo,
    )

    producto = (await db_session.execute(select(Product))).scalar_one()
    mov = (await db_session.execute(select(InventoryMovement))).scalar_one()
    gasto = (await db_session.execute(select(ExpenseEntry))).scalar_one()

    assert gasto.amount == Decimal("4000.00")  # 5 × 800
    assert (gasto.category, gasto.expense_type, gasto.payment_method) == (
        "INVENTORY",
        "COGS",
        "transfer",
    )
    assert gasto.description == "Stock inicial: Sahumerio lavanda"
    assert gasto.product_id == producto.id
    assert gasto.source_upload_id == archivo
    # La misma fila de origen que el movimiento: lo que permite cruzarlos y que una
    # relectura no duplique el gasto.
    assert gasto.source_row_ref is not None
    assert gasto.source_row_ref == mov.source_row_ref
