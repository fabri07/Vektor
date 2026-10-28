"""V-16 (revisión) — el monto de una fila de «Otros» sale de SU columna.

- La forma canónica («1200.00») vale sólo en una fila que escribió Véktor
  (``MONTO_CANONICO_KEY``, B·5e/B·5g): en una fila de archivo manda la columna, y
  un «12.50» en una columna de coma decimal no entra.
- Si la fila tiene una columna de monto/total y no se lee, no hay monto: nunca se
  cae al precio unitario ni al costo.
"""

from __future__ import annotations

import io
import uuid
from decimal import Decimal
from typing import Any

from httpx import AsyncClient
from openpyxl import Workbook
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.file_parsing import parse_uploaded_content
from app.application.services.ingestion_import_service import MONTO_CANONICO_KEY
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord
from app.tests._respaldo import producto_con_compra
from app.tests.api.v1.test_otros_lote_compuertas_i245 import (
    _VELA,
    _confirmar_ventas,
    _importar_sugeridos,
    _pendiente,
    _sin_broker,  # noqa: F401
    _todos,
)

_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


async def _fila(
    db: AsyncSession, tenant: Tenant, row: dict[str, str], entidad: str = "sale"
) -> UnclassifiedRecord:
    rec = UnclassifiedRecord(
        tenant_id=tenant.tenant_id,
        source="ingestion",
        context_label="Venta sin compra previa",
        row_data=row,
        suggested_entity=entidad,
        status="PENDING",
    )
    db.add(rec)
    await db.commit()
    return rec


async def test_celda_incompatible_con_su_columna_no_entra_por_el_lote(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Revisor: «12.50» en una columna de coma decimal (AR) es INCOMPATIBLE."""
    tid = sample_tenant.tenant_id
    await producto_con_compra(db_session, tid, nombre=_VELA)
    await _confirmar_ventas(
        client, auth_headers, db_session, sample_tenant,
        [["05/03/2025", "Ana", _VELA, 1, "1.234,50"], ["06/03/2025", "Ana", _VELA, 1, "12.50"]],
    )
    filas = await _todos(db_session, UnclassifiedRecord, *_pendiente(tid))
    assert len(filas) == 1
    assert MONTO_CANONICO_KEY not in filas[0].row_data

    cuerpo = await _importar_sugeridos(client, auth_headers)

    assert (cuerpo["imported_sales"], cuerpo["needs_manual"]) == (0, 1), cuerpo
    ventas = await _todos(db_session, SaleEntry, SaleEntry.tenant_id == tid)
    assert sorted(v.amount for v in ventas) == [Decimal("1234.50")]


async def test_total_ilegible_no_cae_al_precio_unitario(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Revisor: con el total incompatible, «9.99» del precio unitario no es el monto."""
    tid = sample_tenant.tenant_id
    await producto_con_compra(db_session, tid, nombre=_VELA)
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Ventas"
    ws.append(["fecha", "cliente", "articulo", "cantidad", "precio_unitario", "total"])
    ws.append(["05/03/2025", "Ana", _VELA, 1, "5.00", "1,234.50"])
    ws.append(["06/03/2025", "Ana", _VELA, 1, "9.99", "12,50"])
    buf = io.BytesIO()
    wb.save(buf)
    contenido = buf.getvalue()
    rec = UploadedFile(
        tenant_id=tid, uploaded_by=None, original_filename="v.xlsx",
        s3_key=f"uploads/test/{uuid.uuid4()}/v.xlsx", content_type=_XLSX,
        size_bytes=len(contenido), purpose="ingestion", status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=parse_uploaded_content(contenido, _XLSX, "v.xlsx"),
    )
    db_session.add(rec)
    await db_session.commit()
    mapeo = {
        "fecha": "transaction_date", "articulo": "product_name",
        "cantidad": "quantity", "total": "amount",
    }
    r = await client.post(
        f"/api/v1/ingestion/files/{rec.id}/confirm",
        json={
            "column_mappings": [{"source_column": c, "target_field": t} for c, t in mapeo.items()],
            "confirmed_fields": {"ventas": True},
        },
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text

    cuerpo = await _importar_sugeridos(client, auth_headers)

    assert cuerpo["imported_sales"] == 0, cuerpo
    ventas = await _todos(db_session, SaleEntry, SaleEntry.tenant_id == tid)
    assert Decimal("9.99") not in {v.amount for v in ventas}


async def test_lote_con_monto_ilegible_no_usa_el_costo(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    await _fila(
        db_session, sample_tenant,
        {"fecha": "2024-05-01", "detalle": "Pago luz", "costo": "800", "monto": "12.500",
         "categoria": "Luz"},
        entidad="expense",
    )

    cuerpo = await _importar_sugeridos(client, auth_headers)

    assert (cuerpo["imported_expenses"], cuerpo["needs_manual"]) == (0, 1), cuerpo


async def test_link_product_toma_el_monto_y_no_el_costo(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    coca = await producto_con_compra(db_session, sample_tenant.tenant_id, nombre="Coca Cola 500ml")
    # El costo va ANTES del monto en la fila: por orden de columnas ganaba él.
    rec = await _fila(
        db_session, sample_tenant,
        {"fecha": "2024-04-13", "costo": "800", "monto": "1200", "cantidad": "1",
         "producto": "Negrita chica", "medio_de_pago": "cash"},
    )

    r = await client.post(
        f"/api/v1/others/{rec.id}/link-product",
        json={"product_id": str(coca.id)}, headers=auth_headers,
    )

    assert r.status_code == 200, r.text
    ventas = await _todos(db_session, SaleEntry, SaleEntry.tenant_id == sample_tenant.tenant_id)
    assert [v.amount for v in ventas] == [Decimal("1200")]


async def test_link_product_con_monto_ilegible_no_usa_el_costo(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    coca = await producto_con_compra(db_session, sample_tenant.tenant_id, nombre="Coca Cola 500ml")
    rec = await _fila(
        db_session, sample_tenant,
        {"fecha": "2024-04-13", "costo": "800", "monto": "12.500", "cantidad": "1",
         "producto": "Negrita chica", "medio_de_pago": "cash"},
    )

    r = await client.post(
        f"/api/v1/others/{rec.id}/link-product",
        json={"product_id": str(coca.id)}, headers=auth_headers,
    )

    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "MONTO_REQUERIDO"


async def test_sin_marca_la_forma_canonica_no_vale(
    client: AsyncClient,
    auth_headers: dict[str, Any],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Una fila B·5e vieja (sin marca) queda como antes: a resolver a mano."""
    await producto_con_compra(db_session, sample_tenant.tenant_id, nombre=_VELA)
    await _fila(
        db_session, sample_tenant,
        {"fecha": "2024-03-05", "monto": "1200.00", "cantidad": "1", "producto": _VELA,
         "medio_de_pago": "cash"},
    )

    cuerpo = await _importar_sugeridos(client, auth_headers)

    assert (cuerpo["imported_sales"], cuerpo["needs_manual"]) == (0, 1), cuerpo
