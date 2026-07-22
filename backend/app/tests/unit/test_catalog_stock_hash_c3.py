"""F6-C3: el source_row_hash de un movimiento de catálogo NO debe incluir la fecha
sintética de import (today). Su único consumidor es F3 (product_dedup_service), que
dedupea movimientos al fusionar productos duplicados; con today el mismo catálogo
releído otro día daba un hash distinto → doble conteo de stock."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.ingestion_import_service import insert_confirmed_data
from app.application.services.inventory_movement_origin import (
    SOURCE_CATALOG_INITIAL_STOCK,
    compute_source_row_hash,
)
from app.domain.business_time import now_ar_naive
from app.persistence.models.file import UploadedFile
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.tenant import Tenant


async def _import_catalog(
    db_session: AsyncSession, tid: uuid.UUID, upload_id: uuid.UUID, stock: str = "5"
) -> None:
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "stock",
        "has_producto": True,
        "stock_detectado": [{"producto": "Leche", "stock": stock}],
    }
    await insert_confirmed_data(
        db_session,
        tid,
        summary,
        {"productos": True},
        column_mappings={"producto": "name", "stock": "stock_units"},
        uploaded_file_id=upload_id,
    )


@pytest.mark.asyncio
async def test_hash_de_catalogo_no_depende_del_dia_de_import(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    up = UploadedFile(
        id=uuid.uuid4(),
        tenant_id=tid,
        original_filename="cat.csv",
        s3_key="k",
        content_type="text/csv",
        size_bytes=10,
        purpose="ingestion",
    )
    db_session.add(up)
    await db_session.flush()
    await _import_catalog(db_session, tid, up.id)
    await db_session.commit()

    mov = (
        await db_session.execute(
            select(InventoryMovement).where(
                InventoryMovement.source_type == SOURCE_CATALOG_INITIAL_STOCK
            )
        )
    ).scalar_one()

    # El hash es la identidad lógica pura (sin fecha).
    expected = compute_source_row_hash(
        product_key="Leche",
        qty=mov.qty,
        unit_cost=mov.unit_cost,
        movement_date=None,
        supplier_key=None,
        upload_id=up.id,
    )
    assert mov.source_row_hash == expected
    # Mutation guard: NO coincide con la variante que metía today.
    with_today = compute_source_row_hash(
        product_key="Leche",
        qty=mov.qty,
        unit_cost=mov.unit_cost,
        movement_date=now_ar_naive(),
        supplier_key=None,
        upload_id=up.id,
    )
    assert mov.source_row_hash != with_today


def test_hash_distingue_archivos_y_filas() -> None:
    """La identidad lógica separa archivos (upload_id) y filas (qty), y es estable
    para la misma fila del mismo archivo (retry/relectura → mismo hash)."""
    u1 = uuid.uuid4()
    u2 = uuid.uuid4()
    h1 = compute_source_row_hash(
        product_key="Leche", qty=5, unit_cost=None, movement_date=None,
        supplier_key=None, upload_id=u1,
    )
    # Archivos distintos con la misma fila → sin colisión.
    assert h1 != compute_source_row_hash(
        product_key="Leche", qty=5, unit_cost=None, movement_date=None,
        supplier_key=None, upload_id=u2,
    )
    # Misma fila, mismo archivo → mismo hash (estable, sin depender del día).
    assert h1 == compute_source_row_hash(
        product_key="Leche", qty=5, unit_cost=None, movement_date=None,
        supplier_key=None, upload_id=u1,
    )
    # Fila distinta (qty) → hash distinto.
    assert h1 != compute_source_row_hash(
        product_key="Leche", qty=6, unit_cost=None, movement_date=None,
        supplier_key=None, upload_id=u1,
    )
