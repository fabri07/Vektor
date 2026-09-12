"""E6b — el ciclo completo de "Otros": el import la captura, el usuario la
clasifica, y el borrado del archivo tiene que alcanzarla.

Las tres puntas ya estaban probadas por separado, y ahí está el problema: los
tests de procedencia arman el ``UnclassifiedRecord`` A MANO —con su
``uploaded_file_id`` puesto— y los de borrado arman a mano el registro derivado
con su ``source_row_ref``. Cada uno prueba el mundo que construyó. Si el import
REAL capturara la fila sin ligarla al archivo, o si el endpoint de clasificación
no derivara el ref, la cadena se cortaría en silencio y **ninguno de los dos se
enteraría**: uno ya tiene el vínculo puesto por la fixture y el otro también.

Acá la fila llega a "Otros" porque el parser de producción no supo leerla, se
clasifica por el endpoint real y se borra por el endpoint real. Lo que se afirma
es lo único que le importa al usuario: después de borrar el archivo no queda
plata suya adentro, y lo que no se pueda revertir se informa.
"""

from __future__ import annotations

import io
import uuid
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
from app.persistence.models.transaction import ExpenseEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_HEADERS = ["fecha", "articulo", "cantidad", "total", "proveedor"]
_PRODUCTO = "Vela aromatica 200g"


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker, cada confirm paga ~5s de reintentos de kombu (fail-safe)."""


def _libro_con_una_fila_ilegible() -> bytes:
    """Dos compras; la segunda sin fecha reconocible.

    La fila rota lo está por su CONTENIDO, no por un flag del test: el parser no
    puede leer «cuando pueda» como fecha, y el importador manda a "Otros" lo que
    no puede fechar en vez de inventar el día de hoy (invariante 2d).
    """
    wb = Workbook()
    hoja = wb.active
    hoja.title = "Compras"
    hoja.append(_HEADERS)
    hoja.append(["2024-03-05", _PRODUCTO, 5, 6000, "Distribuidora Sur"])
    hoja.append(["cuando pueda", "Difusor bambu", 3, 3600, "Distribuidora Sur"])
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


@pytest_asyncio.fixture
async def archivo(db_session: AsyncSession, sample_tenant: Tenant) -> UploadedFile:
    contenido = _libro_con_una_fila_ilegible()
    record = UploadedFile(
        tenant_id=sample_tenant.tenant_id,
        uploaded_by=None,
        original_filename="compras.xlsx",
        s3_key=f"uploads/test/{uuid.uuid4()}/compras.xlsx",
        content_type=_XLSX_MIME,
        size_bytes=len(contenido),
        purpose="ingestion",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=parse_uploaded_content(contenido, _XLSX_MIME, "compras.xlsx"),
    )
    db_session.add(record)
    await db_session.commit()
    return record


async def _gastos(db: AsyncSession, tenant_id: uuid.UUID) -> list[ExpenseEntry]:
    """Toma el UUID y no el ``Tenant``: después de ``expire_all()`` leerle un
    atributo al ORM dispara IO fuera del contexto async (``MissingGreenlet``)."""
    return list(
        (
            await db.execute(
                select(ExpenseEntry).where(
                    ExpenseEntry.tenant_id == tenant_id,
                    ExpenseEntry.voided_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )


async def _filas_en_otros(
    db: AsyncSession, tenant_id: uuid.UUID
) -> list[UnclassifiedRecord]:
    return list(
        (
            await db.execute(
                select(UnclassifiedRecord).where(
                    UnclassifiedRecord.tenant_id == tenant_id
                )
            )
        )
        .scalars()
        .all()
    )


async def _confirmar(
    client: AsyncClient, auth_headers: dict[str, str], archivo: UploadedFile
) -> None:
    resp = await client.post(
        f"/api/v1/ingestion/files/{archivo.id}/confirm",
        json={
            "column_mappings": [
                {"source_column": "fecha", "target_field": "expense_date"},
                {"source_column": "articulo", "target_field": "product_name"},
                {"source_column": "cantidad", "target_field": "quantity"},
                {"source_column": "total", "target_field": "amount"},
                {"source_column": "proveedor", "target_field": "supplier_name"},
            ],
            "confirmed_fields": {"gastos": True},
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text


async def test_la_fila_capturada_por_el_import_queda_ligada_a_su_archivo(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    archivo: UploadedFile,
) -> None:
    """El eslabón que las fixtures a mano daban por hecho.

    Si el import capturara la fila sin ``uploaded_file_id``, el endpoint de
    clasificación no podría derivar el ``source_row_ref`` —lo hace sólo si hay
    archivo— y el derivado nacería huérfano. Todo lo demás seguiría "pasando".
    """
    tenant_id = sample_tenant.tenant_id
    await _confirmar(client, auth_headers, archivo)

    filas = await _filas_en_otros(db_session, tenant_id)
    assert len(filas) == 1, f"se esperaba una sola fila ilegible en Otros: {filas}"
    assert filas[0].uploaded_file_id == archivo.id, (
        "la fila capturada no quedó ligada al archivo: el derivado va a nacer huérfano"
    )
    assert len(await _gastos(db_session, tenant_id)) == 1, "la fila legible sí entra"


async def test_borrar_el_archivo_alcanza_al_gasto_clasificado_a_mano(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    archivo: UploadedFile,
) -> None:
    """El ciclo entero por HTTP: importar → clasificar → borrar.

    Lo que se afirma no son los contadores sino la plata: después de borrar el
    archivo no puede quedar viva ninguna de las dos compras — ni la que el
    importador leyó, ni la que el usuario rescató de "Otros".
    """
    tenant_id = sample_tenant.tenant_id
    await _confirmar(client, auth_headers, archivo)
    fila = (await _filas_en_otros(db_session, tenant_id))[0]

    resp = await client.post(
        f"/api/v1/others/{fila.id}/reclassify",
        json={
            "entity_type": "expense",
            "fields": {
                "amount": "3600.00",
                "expense_date": "2024-03-06T00:00:00",
                "category": "INVENTORY",
                "description": "Difusor bambu",
            },
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text

    vivos = await _gastos(db_session, tenant_id)
    assert len(vivos) == 2, f"clasificar tiene que crear el gasto: {vivos}"
    derivado = [g for g in vivos if g.source_row_ref and "unclassified:" in g.source_row_ref]
    assert derivado, (
        f"el gasto clasificado nació sin procedencia de su fila: "
        f"{[(g.amount, g.source_upload_id, g.source_row_ref) for g in vivos]}"
    )

    borrado = await client.request(
        "DELETE",
        f"/api/v1/ingestion/files/{archivo.id}?confirm=true",
        headers=auth_headers,
    )
    assert borrado.status_code == 200, borrado.text

    db_session.expire_all()
    quedaron = await _gastos(db_session, tenant_id)
    assert quedaron == [], (
        f"quedó plata del archivo después de borrarlo: "
        f"{[(str(g.amount), g.source_row_ref) for g in quedaron]}"
    )
    cuerpo = borrado.json()
    assert cuerpo["fully_reverted"] is True, (
        f"no quedó nada vivo pero el borrado no lo afirma: {cuerpo}"
    )


async def test_un_gasto_clasificado_sin_archivo_no_se_promete_revertido(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    archivo: UploadedFile,
) -> None:
    """La contracara: una fila clasificada ANTES de que existiera la procedencia.

    Su derivado nació sin ``source_row_ref``, así que el borrado no lo alcanza —y
    está bien, no hay forma de saber que era de este archivo—. Lo que NO puede
    hacer es decir que revirtió todo. Se simula borrándole la procedencia al
    derivado, que es exactamente el estado en que quedaron los clasificados
    históricos.
    """
    tenant_id = sample_tenant.tenant_id
    await _confirmar(client, auth_headers, archivo)
    fila = (await _filas_en_otros(db_session, tenant_id))[0]

    resp = await client.post(
        f"/api/v1/others/{fila.id}/reclassify",
        json={
            "entity_type": "expense",
            "fields": {
                "amount": "3600.00",
                "expense_date": "2024-03-06T00:00:00",
                "category": "INVENTORY",
                "description": "Difusor bambu",
            },
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text

    derivado = [
        g
        for g in await _gastos(db_session, tenant_id)
        if g.source_row_ref and "unclassified:" in g.source_row_ref
    ][0]
    derivado.source_row_ref = None
    derivado.source_upload_id = None
    await db_session.commit()

    borrado = await client.request(
        "DELETE",
        f"/api/v1/ingestion/files/{archivo.id}?confirm=true",
        headers=auth_headers,
    )
    assert borrado.status_code == 200, borrado.text
    cuerpo = borrado.json()

    db_session.expire_all()
    quedaron = await _gastos(db_session, tenant_id)
    assert len(quedaron) == 1, f"el huérfano tiene que sobrevivir: {quedaron}"
    assert cuerpo["fully_reverted"] is False, (
        f"sobrevivió un gasto y el borrado dice haber revertido todo: {cuerpo}"
    )
