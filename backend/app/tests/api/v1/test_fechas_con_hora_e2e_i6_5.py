"""I6-5 — una planilla US con hora entra con las fechas en su mes.

Hermano de ``test_el_orden_dia_mes_lo_decide_la_columna_no_la_celda`` en
``test_ingestion_interpretacion_e2e``, con la hora pegada a la fecha: así llegan
las exportaciones de pasarelas y e-commerce. Las celdas viajan como TEXTO (igual
que en un CSV); el parser de producción arma el summary, el confirm entra por
HTTP y se afirma la venta guardada.
"""

from __future__ import annotations

import io
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.file_parsing import parse_uploaded_content
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.tests._respaldo import productos_con_compra

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_PRODUCTO = "Vela aromatica 200g"
_CLIENTE = "Consumidor final"
#: La columna `cliente` no es decorativa: sin ella el clasificador manda la hoja a
#: productos y el test mediría otro bucket.
_HEADERS = ["fecha", "producto", "cantidad", "total", "cliente", "forma de pago"]
_MAPEO = [
    {"source_column": "fecha", "target_field": "transaction_date"},
    {"source_column": "producto", "target_field": "product_name"},
    {"source_column": "cantidad", "target_field": "quantity"},
    {"source_column": "total", "target_field": "amount"},
    {"source_column": "cliente", "target_field": "customer_name"},
    {"source_column": "forma de pago", "target_field": "payment_method"},
]


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker, cada confirm paga ~5s de reintentos de kombu (fail-safe)."""


async def _subir_y_confirmar(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    tenant: Tenant,
    fechas_y_montos: list[tuple[str, int]],
) -> Any:
    await productos_con_compra(db_session, tenant.tenant_id, [_PRODUCTO])
    wb = Workbook()
    hoja = wb.active
    hoja.title = "Ventas"
    hoja.append(_HEADERS)
    for fecha, monto in fechas_y_montos:
        hoja.append([fecha, _PRODUCTO, 1, monto, _CLIENTE, "efectivo"])
    buffer = io.BytesIO()
    wb.save(buffer)
    nombre = "ventas_us_con_hora.xlsx"
    record = UploadedFile(
        tenant_id=tenant.tenant_id,
        uploaded_by=None,
        original_filename=nombre,
        s3_key=f"uploads/test/{uuid.uuid4()}/{nombre}",
        content_type=_XLSX_MIME,
        size_bytes=4096,
        purpose="ingestion",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=parse_uploaded_content(buffer.getvalue(), _XLSX_MIME, nombre),
    )
    db_session.add(record)
    await db_session.commit()
    return await client.post(
        f"/api/v1/ingestion/files/{record.id}/confirm",
        json={"column_mappings": _MAPEO, "confirmed_fields": {"ventas": True}},
        headers=auth_headers,
    )


async def _ventas(db_session: AsyncSession, tenant: Tenant) -> dict[Decimal, datetime]:
    result = await db_session.execute(
        select(SaleEntry).where(
            SaleEntry.tenant_id == tenant.tenant_id, SaleEntry.voided_at.is_(None)
        )
    )
    return {v.amount: v.transaction_date for v in result.scalars().all()}


async def test_la_columna_us_con_hora_entra_con_cada_fecha_en_su_mes(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    resp = await _subir_y_confirmar(
        client,
        auth_headers,
        db_session,
        sample_tenant,
        [("04/13/2026 10:30", 8400), ("03/04/2026 11:00", 6300)],
    )
    assert resp.status_code == 200, resp.text

    assert await _ventas(db_session, sample_tenant) == {
        Decimal("8400"): datetime(2026, 4, 13, 10, 30),
        # Sin el arreglo: 3 de ABRIL, leída dd/mm celda por celda.
        Decimal("6300"): datetime(2026, 3, 4, 11, 0),
    }

