"""I5-1 e I6-6 — lo que el parseo le entrega al import, cruzando archivo real y base.

I5-1: el parser pasaba a texto toda celda de un ``.xlsx``, y el convenio de la
columna (E4) volvía a interpretar los números NATIVOS: un ``1.234`` (uno coma
doscientos treinta y cuatro, celda numérica) se leía como grupo de miles y
entraba como 1234 sin aviso; con un ``302.5`` y un ``149.431`` nativos en la
misma columna, la columna salía contradictoria y las dos filas iban a «Otros».

I6-6: una fecha ISO con zona (``...Z``) llegaba como datetime CON zona a
``transaction_date`` (sin zona): en Postgres el INSERT reventaba y en SQLite se
guardaba la hora UTC como local, con el día corrido. El caso Postgres está en
``integration/test_import_fecha_con_zona_pg.py``.

El archivo lo arma openpyxl (o es un CSV de verdad), lo parsea el parser de
producción, el confirm entra por HTTP y se afirma lo PERSISTIDO.
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
from app.persistence.models.unclassified_record import UnclassifiedRecord
from app.tests._respaldo import productos_con_compra

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_ENCABEZADO = ["fecha", "producto", "total", "cliente", "forma de pago"]


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker, cada confirm paga ~5s de reintentos de kombu (fail-safe)."""


def _planilla(filas: list[list[Any]], *, con_otra_hoja: bool) -> bytes:
    """Un xlsx real. ``con_otra_hoja`` lo lleva por el camino multi-hoja del parser,
    que arma las filas en otro punto que el de una sola hoja."""
    wb = Workbook()
    hoja = wb.active
    hoja.title = "Ventas"
    hoja.append(_ENCABEZADO)
    for fila in filas:
        hoja.append(fila)
    if con_otra_hoja:
        notas = wb.create_sheet("Notas")
        notas.append(["nota", "detalle"])
        notas.append(["reponer", "velas grandes"])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


async def _subir(
    db_session: AsyncSession, tenant: Tenant, contenido: bytes, mime: str, nombre: str
) -> UploadedFile:
    # B·5e: la venta importada de un producto sin compra va a «Otros»; acá se mide
    # el parseo, así que la «Vela» del archivo ya tiene su compra.
    await productos_con_compra(db_session, tenant.tenant_id, ["Vela"])
    record = UploadedFile(
        tenant_id=tenant.tenant_id,
        uploaded_by=None,
        original_filename=nombre,
        s3_key=f"uploads/test/{uuid.uuid4()}/{nombre}",
        content_type=mime,
        size_bytes=4096,
        purpose="ingestion",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=parse_uploaded_content(contenido, mime, nombre),
    )
    db_session.add(record)
    await db_session.commit()
    return record


async def _confirmar(
    client: AsyncClient, auth_headers: dict[str, str], record: UploadedFile
) -> Any:
    return await client.post(
        f"/api/v1/ingestion/files/{record.id}/confirm",
        json={
            "column_mappings": [
                {"source_column": "fecha", "target_field": "transaction_date"},
                {"source_column": "producto", "target_field": "product_name"},
                {"source_column": "total", "target_field": "amount"},
                {"source_column": "cliente", "target_field": "customer_name"},
                {"source_column": "forma de pago", "target_field": "payment_method"},
            ],
            "confirmed_fields": {"ventas": True},
        },
        headers=auth_headers,
    )


async def _ventas(db_session: AsyncSession, tenant: Tenant) -> list[SaleEntry]:
    return list(
        (
            await db_session.execute(
                select(SaleEntry)
                .where(SaleEntry.tenant_id == tenant.tenant_id, SaleEntry.voided_at.is_(None))
                .order_by(SaleEntry.transaction_date)
            )
        ).scalars()
    )


async def _pendientes(db_session: AsyncSession, tenant: Tenant) -> list[UnclassifiedRecord]:
    return list(
        (
            await db_session.execute(
                select(UnclassifiedRecord).where(UnclassifiedRecord.tenant_id == tenant.tenant_id)
            )
        ).scalars()
    )


# ── I5-1 ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("con_otra_hoja", [False, True], ids=["una_hoja", "varias_hojas"])
async def test_un_nativo_con_tres_decimales_no_se_multiplica_por_mil(
    con_otra_hoja: bool,
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Nativos y texto en la MISMA columna: cada uno por su lado.

    El texto ``"12.500"`` fija el convenio de la columna por la forma (el punto
    es de miles) y entra como 12500, como siempre. Los nativos ``1.234`` y
    ``149.431`` no tienen formato que interpretar: entran tal cual (redondeados a
    los centavos de la columna), no como 1234 y 149431.
    """
    record = await _subir(
        db_session,
        sample_tenant,
        _planilla(
            [
                ["2024-03-10", "Vela", 1.234, "Consumidor final", "efectivo"],
                ["2024-03-11", "Vela", 149.431, "Consumidor final", "efectivo"],
                ["2024-03-12", "Vela", "12.500", "Consumidor final", "efectivo"],
            ],
            con_otra_hoja=con_otra_hoja,
        ),
        _XLSX_MIME,
        "ventas.xlsx",
    )
    resp = await _confirmar(client, auth_headers, record)
    assert resp.status_code == 200, resp.text

    montos = [v.amount for v in await _ventas(db_session, sample_tenant)]
    assert montos == [Decimal("1.23"), Decimal("149.43"), Decimal("12500")], (
        "un nativo de Excel se reinterpretó con el convenio de la columna"
    )
    # La hoja «Notas» va a «Otros», como cualquier hoja sin clasificar; ninguna
    # venta tiene que quedar ahí.
    assert [
        p.row_data for p in await _pendientes(db_session, sample_tenant) if "total" in p.row_data
    ] == []


async def test_nativos_de_uno_y_tres_decimales_no_vuelven_contradictoria_la_columna(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """``302.5`` junto a ``149.431``, los dos nativos: antes la forma votaba
    «decimal» por uno y «miles» por el otro, la columna quedaba sin convenio y
    ninguna fila se importaba. Un nativo no vota."""
    record = await _subir(
        db_session,
        sample_tenant,
        _planilla(
            [
                ["2024-03-10", "Vela", 302.5, "Consumidor final", "efectivo"],
                ["2024-03-11", "Vela", 149.431, "Consumidor final", "efectivo"],
            ],
            con_otra_hoja=False,
        ),
        _XLSX_MIME,
        "ventas.xlsx",
    )
    resp = await _confirmar(client, auth_headers, record)
    assert resp.status_code == 200, resp.text

    montos = [v.amount for v in await _ventas(db_session, sample_tenant)]
    assert montos == [Decimal("302.50"), Decimal("149.43")]
    assert await _pendientes(db_session, sample_tenant) == []


async def test_el_texto_de_la_columna_sigue_con_su_convenio(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Lo que NO cambia: un ``"1.234"`` escrito como TEXTO sí tiene formato, y con
    ``"12.500,50"`` en la columna (coma decimal) es mil doscientos treinta y
    cuatro. Un nativo al lado no le cambia la lectura."""
    record = await _subir(
        db_session,
        sample_tenant,
        _planilla(
            [
                ["2024-03-10", "Vela", "1.234", "Consumidor final", "efectivo"],
                ["2024-03-11", "Vela", "12.500,50", "Consumidor final", "efectivo"],
                ["2024-03-12", "Vela", 7.5, "Consumidor final", "efectivo"],
            ],
            con_otra_hoja=False,
        ),
        _XLSX_MIME,
        "ventas.xlsx",
    )
    resp = await _confirmar(client, auth_headers, record)
    assert resp.status_code == 200, resp.text

    montos = [v.amount for v in await _ventas(db_session, sample_tenant)]
    assert montos == [Decimal("1234"), Decimal("12500.50"), Decimal("7.50")]


# ── I6-6 ─────────────────────────────────────────────────────────────────────


async def test_una_fecha_iso_con_zona_cae_en_el_dia_argentino(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """``02:30Z`` del 6 de junio son las 23:30 del 5 en Argentina, y así se guarda:
    sin zona y en el día de negocio. ``-03:00`` ya es hora argentina."""
    csv = (
        b"fecha,producto,total,cliente,forma de pago\n"
        b"2026-06-06T02:30:00Z,Vela,1500,Consumidor final,efectivo\n"
        b"2026-06-07T10:15:00-03:00,Vela,2500,Consumidor final,efectivo\n"
    )
    record = await _subir(db_session, sample_tenant, csv, "text/csv", "ventas.csv")
    resp = await _confirmar(client, auth_headers, record)
    assert resp.status_code == 200, resp.text

    ventas = await _ventas(db_session, sample_tenant)
    assert [v.transaction_date for v in ventas] == [
        datetime(2026, 6, 5, 23, 30),
        datetime(2026, 6, 7, 10, 15),
    ]
    assert all(v.transaction_date.tzinfo is None for v in ventas)
