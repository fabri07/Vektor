"""M·1c-2 — la ingesta, el remito y las reversas recalculan el costo vigente.

Contra el importador real (`insert_confirmed_data`), el remito (`import_receipt`)
y el borrado de archivo (`revert_file_data`): una compra importada ya no pisa el
costo por llegar última; el lote recalcula una vez al final con la regla de M·1c.
"""

from __future__ import annotations

import unittest.mock
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.file_deletion_service import (
    record_import_ledger,
    revert_file_data,
)
from app.application.services.ingestion_import_service import (
    ReceiptLine,
    import_receipt,
    insert_confirmed_data,
)
from app.application.services.stock_service import increment_stock
from app.domain.costo_vigente import ORIGEN_FIELD
from app.domain.ingestion_version import INGESTION_VERSION
from app.domain.purchase_cost import COSTO_BASE_FIELD
from app.persistence.models.file import PROCESSING_STATUS_DONE, UploadedFile
from app.persistence.models.product import Product
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant

_CTX = "table"
_COLS = {
    "fecha": "expense_date",
    "proveedor": "supplier_name",
    "articulo": "product_name",
    "cantidad": "quantity",
    "precio_compra": "unit_price",
    "total": "amount",
}


async def _producto(db: AsyncSession, tenant: Tenant, costo: str | None = "60") -> Product:
    p = Product(
        tenant_id=tenant.tenant_id,
        name="Vela",
        sale_price_ars=Decimal("300"),
        unit_cost_ars=Decimal(costo) if costo is not None else None,
        stock_units=0,
    )
    db.add(p)
    await db.flush()
    return p


async def _comprar(
    db: AsyncSession, tenant: Tenant, p: Product, costo: str, fecha: datetime
) -> None:
    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        await increment_stock(
            p.id, tenant.tenant_id, 1, Decimal(costo), "src", db, occurred_at=fecha
        )
    await db.refresh(p)


async def _archivo(db: AsyncSession, tenant: Tenant) -> UploadedFile:
    a = UploadedFile(
        tenant_id=tenant.tenant_id,
        uploaded_by=None,
        original_filename="compras.xlsx",
        s3_key=f"uploads/test/{uuid.uuid4()}/compras.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=1024,
        purpose="general",
        status="uploaded",
        processing_status=PROCESSING_STATUS_DONE,
        parsed_summary_json={},
        ingestion_version=INGESTION_VERSION,
    )
    db.add(a)
    await db.flush()
    return a


async def _importar_compra(
    db: AsyncSession,
    tenant: Tenant,
    archivo: UploadedFile,
    *,
    fecha: str,
    costo: str,
) -> dict[str, Any]:
    fila = {
        "fecha": fecha, "proveedor": "Sur", "articulo": "Vela", "cantidad": "10",
        "precio_compra": costo, "total": str(Decimal(costo) * 10), "__context__": _CTX,
    }
    counts = await insert_confirmed_data(
        db,
        tenant.tenant_id,
        {
            "file_type": "spreadsheet",
            "inferred_type": "gastos",
            "multi_sheet": False,
            "row_count": 1,
            "mapping_contexts": [
                {
                    "context_id": _CTX,
                    "entity_type": "expense",
                    "source_kind": "sheet",
                    "headers": list(_COLS),
                    "row_count": 1,
                }
            ],
            "gastos_detectados": [fila],
        },
        {},
        return_details=True,
        context_mappings={_CTX: dict(_COLS)},
        context_entity={_CTX: "expense"},
        context_confirmed={_CTX: True},
        source="ingestion",
        uploaded_file_id=archivo.id,
    )
    await db.flush()
    return counts


async def test_un_libro_de_compras_atrasado_no_pisa_una_compra_mas_nueva(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    p = await _producto(db_session, sample_tenant)
    await _comprar(db_session, sample_tenant, p, "100", datetime(2026, 9, 10, 10))
    archivo = await _archivo(db_session, sample_tenant)
    await _importar_compra(db_session, sample_tenant, archivo, fecha="2024-03-05", costo="120")
    await db_session.refresh(p)
    assert p.unit_cost_ars == Decimal("100.00")


async def test_un_libro_de_compras_mas_nuevo_manda(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    p = await _producto(db_session, sample_tenant)
    await _comprar(db_session, sample_tenant, p, "100", datetime(2024, 1, 10, 10))
    archivo = await _archivo(db_session, sample_tenant)
    await _importar_compra(db_session, sample_tenant, archivo, fecha="2024-03-05", costo="120")
    await db_session.refresh(p)
    assert p.unit_cost_ars == Decimal("120.00")
    assert (p.custom_fields or {})[ORIGEN_FIELD] == "compra"


async def test_producto_legado_el_libro_mas_nuevo_lo_actualiza_el_viejo_no(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Sin procedencia (costo anterior a M·1c): lo cambia sólo una compra DEL
    LOTE que sea la más reciente; un libro más viejo que otra compra no."""
    p = await _producto(db_session, sample_tenant, costo="500")
    viejo = await _archivo(db_session, sample_tenant)
    await _importar_compra(db_session, sample_tenant, viejo, fecha="2024-03-05", costo="120")
    await db_session.refresh(p)
    assert p.unit_cost_ars == Decimal("120.00")  # la del lote es la más reciente

    # Vuelve a quedar sin procedencia (costo legado), y llega un libro más viejo.
    p.unit_cost_ars = Decimal("500")
    p.custom_fields = {}
    await db_session.flush()
    mas_viejo = await _archivo(db_session, sample_tenant)
    await _importar_compra(
        db_session, sample_tenant, mas_viejo, fecha="2023-01-05", costo="90"
    )
    await db_session.refresh(p)
    assert p.unit_cost_ars == Decimal("500")


async def test_borrar_el_archivo_que_fijaba_el_costo_vuelve_a_la_compra_viva(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """La reversa restaura el `before` del ledger (80) y DESPUÉS recalcula: una
    compra atrasada cargada en el medio (90, del 5/9) es la fuente viva más
    reciente cuando se anula la del archivo (10/9)."""
    p = await _producto(db_session, sample_tenant)
    await _comprar(db_session, sample_tenant, p, "80", datetime(2026, 9, 1, 10))
    archivo = await _archivo(db_session, sample_tenant)
    counts = await _importar_compra(
        db_session, sample_tenant, archivo, fecha="2026-09-10", costo="120"
    )
    await db_session.refresh(p)
    assert p.unit_cost_ars == Decimal("120.00")
    await record_import_ledger(
        db_session,
        tenant_id=sample_tenant.tenant_id,
        file_id=archivo.id,
        product_details=counts.pop("product_details", []) or [],
    )
    await _comprar(db_session, sample_tenant, p, "90", datetime(2026, 9, 5, 10))
    assert p.unit_cost_ars == Decimal("120.00")

    contadores = await revert_file_data(db_session, archivo.id, sample_tenant.tenant_id)
    await db_session.refresh(p)
    assert p.unit_cost_ars == Decimal("90.00")
    assert contadores["productos_restaurados"] == 1


async def test_el_remito_recalcula_y_rotula_el_envio_aparte(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    p = await _producto(db_session, sample_tenant)
    await _comprar(db_session, sample_tenant, p, "100", datetime(2026, 9, 10, 10))
    proveedor = Supplier(tenant_id=sample_tenant.tenant_id, name="Mayorista")
    db_session.add(proveedor)
    await db_session.flush()

    # Remito atrasado: no pisa la compra del 10/9.
    await import_receipt(
        db_session,
        sample_tenant.tenant_id,
        proveedor.id,
        [ReceiptLine("Vela", None, 5, Decimal("70"))],
        shipping_cost=Decimal("500"),
        transaction_date=datetime(2026, 8, 1, 12),
    )
    await db_session.refresh(p)
    assert p.unit_cost_ars == Decimal("100.00")

    # Remito de hoy: manda, y el rótulo dice que su costo no trae el envío.
    await import_receipt(
        db_session,
        sample_tenant.tenant_id,
        proveedor.id,
        [ReceiptLine("Vela", None, 5, Decimal("110"))],
        shipping_cost=Decimal("500"),
    )
    await db_session.refresh(p)
    assert p.unit_cost_ars == Decimal("110.00")
    assert (p.custom_fields or {})[COSTO_BASE_FIELD] == "sin_flete"
