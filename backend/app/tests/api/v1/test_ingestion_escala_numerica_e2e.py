"""E4 — la escala de los montos, cruzando el archivo real y la persistencia.

`_parse_amount` no tenía rama para "sólo punto", así que un `"12.500"` daba
**12,5**: una planilla argentina con montos sin centavos se importaba dividida
por mil. El unitario del parser no alcanza para probar el arreglo, porque lo que
faltaba no era una rama sino el CONTEXTO — el convenio se decide mirando la
columna entera, y eso sólo existe cuando hay un archivo de verdad.

Acá el `.xlsx` lo arma openpyxl, lo parsea el parser de producción, el confirm
entra por HTTP y lo que se afirma es el monto PERSISTIDO.
"""

from __future__ import annotations

import io
import uuid
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.file_parsing import parse_uploaded_content
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker, cada confirm paga ~5s de reintentos de kombu (fail-safe)."""


def _planilla(filas: list[list[Any]]) -> bytes:
    wb = Workbook()
    hoja = wb.active
    hoja.title = "Ventas"
    hoja.append(["fecha", "producto", "total", "cliente", "forma de pago"])
    for fila in filas:
        hoja.append(fila)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


async def _subir(db_session: AsyncSession, tenant: Tenant, contenido: bytes) -> UploadedFile:
    record = UploadedFile(
        tenant_id=tenant.tenant_id,
        uploaded_by=None,
        original_filename="ventas.xlsx",
        s3_key=f"uploads/test/{uuid.uuid4()}/ventas.xlsx",
        content_type=_XLSX_MIME,
        size_bytes=4096,
        purpose="ingestion",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=parse_uploaded_content(contenido, _XLSX_MIME, "ventas.xlsx"),
    )
    db_session.add(record)
    await db_session.commit()
    return record


def _mapeo() -> list[dict[str, str]]:
    return [
        {"source_column": "fecha", "target_field": "transaction_date"},
        {"source_column": "producto", "target_field": "product_name"},
        {"source_column": "total", "target_field": "amount"},
        {"source_column": "cliente", "target_field": "customer_name"},
        {"source_column": "forma de pago", "target_field": "payment_method"},
    ]


async def _confirmar(
    client: AsyncClient, auth_headers: dict[str, str], record: UploadedFile
) -> Any:
    return await client.post(
        f"/api/v1/ingestion/files/{record.id}/confirm",
        json={"column_mappings": _mapeo(), "confirmed_fields": {"ventas": True}},
        headers=auth_headers,
    )


async def _montos(db_session: AsyncSession, tenant: Tenant) -> list[Decimal]:
    filas = (
        await db_session.execute(
            select(SaleEntry).where(
                SaleEntry.tenant_id == tenant.tenant_id, SaleEntry.voided_at.is_(None)
            )
        )
    ).scalars()
    return sorted(f.amount for f in filas)


@pytest_asyncio.fixture
def _tenant(sample_tenant: Tenant) -> Tenant:
    return sample_tenant


async def test_montos_sin_centavos_no_se_dividen_por_mil(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """El caso que rompía: una columna entera de miles escritos con punto.

    Sin ningún valor inequívoco, el convenio sale de la forma — un grupo final de
    exactamente 3 dígitos es un grupo de miles.
    """
    record = await _subir(
        db_session,
        sample_tenant,
        _planilla(
            [
                ["2024-03-10", "Vela", "12.500", "Consumidor final", "efectivo"],
                ["2024-03-11", "Vela", "1.500", "Consumidor final", "efectivo"],
                ["2024-03-12", "Vela", "350.000", "Consumidor final", "efectivo"],
            ]
        ),
    )
    resp = await _confirmar(client, auth_headers, record)
    assert resp.status_code == 200, resp.text

    assert await _montos(db_session, sample_tenant) == [
        Decimal("1500"),
        Decimal("12500"),
        Decimal("350000"),
    ]


async def test_un_valor_inequivoco_fija_la_columna(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Basta que UNA fila traiga los dos separadores para resolver toda la columna."""
    record = await _subir(
        db_session,
        sample_tenant,
        _planilla(
            [
                ["2024-03-10", "Vela", "12.500,50", "Consumidor final", "efectivo"],
                ["2024-03-11", "Vela", "1.500", "Consumidor final", "efectivo"],
            ]
        ),
    )
    resp = await _confirmar(client, auth_headers, record)
    assert resp.status_code == 200, resp.text

    assert await _montos(db_session, sample_tenant) == [
        Decimal("1500"),
        Decimal("12500.50"),
    ]


async def test_los_centavos_siguen_siendo_centavos(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """La otra mitad: una columna de precios chicos no se multiplica por mil.

    Un grupo final de 1 ó 2 dígitos es la parte decimal — la moneda argentina usa
    2 decimales, no 3.
    """
    record = await _subir(
        db_session,
        sample_tenant,
        _planilla(
            [
                ["2024-03-10", "Vela", "12.50", "Consumidor final", "efectivo"],
                ["2024-03-11", "Vela", "8.90", "Consumidor final", "efectivo"],
            ]
        ),
    )
    resp = await _confirmar(client, auth_headers, record)
    assert resp.status_code == 200, resp.text

    assert await _montos(db_session, sample_tenant) == [Decimal("8.90"), Decimal("12.50")]


async def test_una_columna_contradictoria_no_inventa_la_escala(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """`12.500` junto a `12.50`: elegir un convenio rompería la mitad de las filas.

    Con TODAS las filas sin escala decidible no queda nada que importar, y eso se
    rechaza ruidosamente en vez de dejar el archivo "importado" con cero filas —
    mismo criterio que el guard de import vacío ya aplicaba. Lo que cambia es el
    motivo: decir "no se detectaron las columnas requeridas" sería mentira acá,
    porque se detectaron perfecto.
    """
    record = await _subir(
        db_session,
        sample_tenant,
        _planilla(
            [
                ["2024-03-10", "Vela", "12.500", "Consumidor final", "efectivo"],
                ["2024-03-11", "Vela", "12.50", "Consumidor final", "efectivo"],
            ]
        ),
    )
    resp = await _confirmar(client, auth_headers, record)
    assert resp.status_code == 422, resp.text
    detalle = resp.json()["detail"]
    assert "escala de los montos" in detalle
    assert "12.500" in detalle and "12.50" in detalle, "el mensaje muestra el conflicto"

    assert await _montos(db_session, sample_tenant) == [], (
        "se importó un monto cuya escala el archivo no permitía decidir"
    )


async def test_una_fila_ambigua_no_arrastra_a_las_demas(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """El caso frecuente: la columna resuelve, y una celda suelta no.

    `12.500,50` fija el convenio de la columna (el punto es de miles), así que
    `1.500` y `350.000` entran sin problema. La celda que igual no se puede leer
    —un texto donde va un número— no se lleva puesta la planilla: su fila queda
    para revisar y el resto se importa.
    """
    record = await _subir(
        db_session,
        sample_tenant,
        _planilla(
            [
                ["2024-03-10", "Vela", "12.500,50", "Consumidor final", "efectivo"],
                ["2024-03-11", "Vela", "1.500", "Consumidor final", "efectivo"],
                ["2024-03-12", "Vela", "mil quinientos", "Consumidor final", "efectivo"],
            ]
        ),
    )
    resp = await _confirmar(client, auth_headers, record)
    assert resp.status_code == 200, resp.text

    assert await _montos(db_session, sample_tenant) == [
        Decimal("1500"),
        Decimal("12500.50"),
    ]
    from app.persistence.models.unclassified_record import UnclassifiedRecord

    pendientes = list(
        (
            await db_session.execute(
                select(UnclassifiedRecord).where(
                    UnclassifiedRecord.tenant_id == sample_tenant.tenant_id
                )
            )
        ).scalars()
    )
    assert len(pendientes) == 1, "la fila ilegible tiene que quedar para revisar"
    assert pendientes[0].row_data["total"] == "mil quinientos", "con el original a la vista"


async def test_los_montos_nativos_de_excel_no_se_tocan(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Una celda numérica de Excel llega como float: no tiene formato que interpretar."""
    record = await _subir(
        db_session,
        sample_tenant,
        _planilla(
            [
                ["2024-03-10", "Vela", 12.5, "Consumidor final", "efectivo"],
                ["2024-03-11", "Vela", 1234.56, "Consumidor final", "efectivo"],
            ]
        ),
    )
    resp = await _confirmar(client, auth_headers, record)
    assert resp.status_code == 200, resp.text

    assert await _montos(db_session, sample_tenant) == [Decimal("12.50"), Decimal("1234.56")]
