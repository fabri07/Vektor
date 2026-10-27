"""I6-3 / I6-4 — la identidad de operación distingue la entidad y la letra fiscal.

* **I6-3:** la clave por ID externo no llevaba la entidad. En una tabla suelta
  mixta (Libro Diario: cada fila produce venta y gasto) el plan de ventas
  reclamaba ``k1:ext:{sistema}:{id}`` primero y el de gastos lo perdía contra una
  identidad con la misma huella: TODOS los gastos se omitían como «ya
  aplicados», en silencio y en cada reimport.
* **I6-4:** la letra fiscal se descartaba. La Factura A y la Factura B del mismo
  punto de venta se numeran por separado en AFIP: la segunda de cada par iba a
  «Otros» como conflicto o, con el mismo contenido, se omitía como ya aplicada.

Y la mitad que más importa: en producción ya hay identidades guardadas con el
formato viejo. Reimportar un archivo ya importado NO puede duplicarlo. Para
fabricar ese estado sin escribir claves a mano, el primer import corre con la
regla vieja (``_con_regla_vieja``: la clave legada pasa a ser LA clave, que es
exactamente lo que hacía el código anterior) y el reimport con la de hoy.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from httpx import AsyncClient
from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import operation_identity_service as ois
from app.application.services.file_parsing import parse_uploaded_content
from app.domain import operation_identity as dominio
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.operation_identity import OperationIdentity, OperationIdentityLink
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord
from app.tests._respaldo import productos_con_compra

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker, cada confirm paga ~5s de reintentos de kombu (fail-safe)."""


@contextmanager
def _con_regla_vieja(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """El planificador con la regla de antes de I6-3/I6-4: la clave legada es la
    clave, y no hay búsqueda de legadas. Es el código que escribió lo que hoy
    está guardado en producción."""
    original = dominio.clave_de_operacion

    def vieja(fila: dict[str, Any], cols: dict[str, str], entity: str) -> Any:
        return dominio.clave_legada_k1(fila, cols, entity) or original(fila, cols, entity)

    with monkeypatch.context() as m:
        m.setattr(ois, "clave_de_operacion", vieja)
        m.setattr(ois, "clave_legada_k1", lambda *_a, **_k: None)
        yield


def _libro(headers: list[str], filas: list[list[Any]], hoja: str) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = hoja
    ws.append(headers)
    for fila in filas:
        ws.append(fila)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


async def _confirmar(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db: AsyncSession,
    tenant: Tenant,
    *,
    headers: list[str],
    filas: list[list[Any]],
    mapeo: dict[str, str],
    campos: dict[str, bool],
    hoja: str,
) -> dict[str, Any]:
    nombre = f"{uuid.uuid4().hex[:8]}.xlsx"
    contenido = _libro(headers, filas, hoja)
    record = UploadedFile(
        tenant_id=tenant.tenant_id,
        uploaded_by=None,
        original_filename=nombre,
        s3_key=f"uploads/test/{uuid.uuid4()}/{nombre}",
        content_type=_XLSX_MIME,
        size_bytes=len(contenido),
        purpose="ingestion",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=parse_uploaded_content(contenido, _XLSX_MIME, nombre),
    )
    db.add(record)
    await db.commit()
    resp = await client.post(
        f"/api/v1/ingestion/files/{record.id}/confirm",
        json={
            "column_mappings": [
                {"source_column": c, "target_field": t} for c, t in mapeo.items()
            ],
            "confirmed_fields": campos,
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    cuerpo: dict[str, Any] = resp.json()
    db.expunge_all()
    return cuerpo


async def _ventas(db: AsyncSession, tenant_id: uuid.UUID) -> list[SaleEntry]:
    return list(
        (
            await db.execute(
                select(SaleEntry).where(
                    SaleEntry.tenant_id == tenant_id, SaleEntry.voided_at.is_(None)
                )
            )
        ).scalars().all()
    )


async def _gastos(db: AsyncSession, tenant_id: uuid.UUID) -> list[ExpenseEntry]:
    return list(
        (
            await db.execute(
                select(ExpenseEntry).where(
                    ExpenseEntry.tenant_id == tenant_id, ExpenseEntry.voided_at.is_(None)
                )
            )
        ).scalars().all()
    )


async def _claves(db: AsyncSession, tenant_id: uuid.UUID) -> list[str]:
    return sorted(
        (
            await db.execute(
                select(OperationIdentity.identity_key).where(
                    OperationIdentity.tenant_id == tenant_id
                )
            )
        ).scalars().all()
    )


async def _otros(db: AsyncSession, tenant_id: uuid.UUID) -> list[UnclassifiedRecord]:
    """Las filas que fueron a «Otros» por un CONFLICTO de identidad (llevan su
    clave). Una fila «ya aplicada» del camino plano también cae a «Otros», como
    «sin monto» —defecto previo, ajeno a I6-3/I6-4—, y no es lo que se mide."""
    return [
        r
        for r in (
            await db.execute(
                select(UnclassifiedRecord).where(UnclassifiedRecord.tenant_id == tenant_id)
            )
        ).scalars().all()
        if (r.row_data or {}).get("__identity_key__")
    ]


# ── I6-3: tabla mixta con ID externo ─────────────────────────────────────────
# `cliente` y `concepto` (sin mapear) están para que el clasificador lea la hoja
# como general (ventas Y gastos), que es lo que es un Libro Diario.
_H_MIXTA = ["fecha", "id", "sistema", "cliente", "concepto", "articulo", "cantidad", "total"]
_MAPEO_MIXTA = {
    "fecha": "transaction_date",
    "id": "external_operation_id",
    "sistema": "external_source",
    "articulo": "product_name",
    "cantidad": "quantity",
    "total": "amount",
}
_FILAS_MIXTA = [
    ["2024-03-05", "77", "Tango", "Ana", "Venta", "Vela aromatica", 1, 1200],
    ["2024-03-06", "78", "Tango", "Beto", "Venta", "Difusor bambu", 2, 3000],
]
_AMBAS = {"ventas": True, "gastos": True}


async def _mixta(
    client: AsyncClient, auth_headers: dict[str, str], db: AsyncSession, tenant: Tenant
) -> dict[str, Any]:
    return await _confirmar(
        client, auth_headers, db, tenant,
        headers=_H_MIXTA, filas=_FILAS_MIXTA, mapeo=_MAPEO_MIXTA, campos=_AMBAS,
        hoja="Diario",
    )


async def test_tabla_mixta_con_el_mismo_id_externo_carga_la_venta_y_el_gasto(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """La misma fila produce una venta y un gasto con el mismo ID de origen. Con
    la entidad fuera de la clave, la venta reclamaba la clave del gasto y los
    dos gastos se omitían como «ya aplicados»."""
    tid = sample_tenant.tenant_id
    await productos_con_compra(db_session, tid, ["Vela aromatica", "Difusor bambu"])

    r = await _mixta(client, auth_headers, db_session, sample_tenant)

    assert len(await _ventas(db_session, tid)) == 2, r
    gastos = await _gastos(db_session, tid)
    assert len(gastos) == 2, f"los gastos de la tabla mixta se omitieron: {r.get('counts', r)}"
    assert sorted(g.amount for g in gastos) == [1200, 3000]
    claves = await _claves(db_session, tid)
    assert "k2:ext:sale:tango:77" in claves and "k2:ext:expense:tango:77" in claves, claves

    # Reimport del mismo contenido desde otro archivo: nada se duplica.
    await _mixta(client, auth_headers, db_session, sample_tenant)
    assert len(await _ventas(db_session, tid)) == 2
    assert len(await _gastos(db_session, tid)) == 2


async def test_reimport_de_una_tabla_mixta_importada_con_la_clave_vieja(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """El estado que hay en producción: la tabla se importó con la regla vieja
    (ventas adentro, gastos omitidos, identidades ``k1:ext`` de las ventas). El
    reimport reconoce las ventas por su clave vieja —no las duplica— y carga los
    gastos, que nunca se habían aplicado. Un segundo reimport no agrega nada."""
    tid = sample_tenant.tenant_id
    await productos_con_compra(db_session, tid, ["Vela aromatica", "Difusor bambu"])
    with _con_regla_vieja(monkeypatch):
        await _mixta(client, auth_headers, db_session, sample_tenant)
    assert len(await _ventas(db_session, tid)) == 2
    assert not await _gastos(db_session, tid), "la regla vieja omite los gastos (I6-3)"
    assert await _claves(db_session, tid) == ["k1:ext:tango:77", "k1:ext:tango:78"]

    r = await _mixta(client, auth_headers, db_session, sample_tenant)
    assert len(await _ventas(db_session, tid)) == 2, f"el reimport duplicó las ventas: {r}"
    assert len(await _gastos(db_session, tid)) == 2

    await _mixta(client, auth_headers, db_session, sample_tenant)
    assert len(await _ventas(db_session, tid)) == 2
    assert len(await _gastos(db_session, tid)) == 2


# ── I6-4: Factura A y Factura B con el mismo número ──────────────────────────
# `cliente` (sin mapear) está para que el clasificador lea la hoja como ventas.
_H_VENTAS = ["fecha", "tipo", "pto", "nro", "cliente", "articulo", "cantidad", "total"]
_MAPEO_VENTAS = {
    "fecha": "transaction_date",
    "tipo": "document_type",
    "pto": "document_series",
    "nro": "invoice_number",
    "articulo": "product_name",
    "cantidad": "quantity",
    "total": "amount",
}
_SOLO_VENTAS = {"ventas": True}


def _factura(letra: str, total: int = 1200, tipo: str | None = None) -> list[Any]:
    return [
        "2024-03-05", tipo or f"Factura {letra}", "0001", "00000123", "Ana",
        "Vela aromatica", 1, total,
    ]


async def _vender(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db: AsyncSession,
    tenant: Tenant,
    filas: list[list[Any]],
) -> dict[str, Any]:
    return await _confirmar(
        client, auth_headers, db, tenant,
        headers=_H_VENTAS, filas=filas, mapeo=_MAPEO_VENTAS, campos=_SOLO_VENTAS,
        hoja="Ventas",
    )


async def test_factura_a_y_factura_b_con_el_mismo_numero_entran_las_dos(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Un Responsable Inscripto factura A y B desde el mismo punto de venta: las
    dos secuencias arrancan en 1 y se solapan. Mismo contenido a propósito: con
    la letra descartada la B se omitía como «ya aplicada», en silencio."""
    tid = sample_tenant.tenant_id
    await productos_con_compra(db_session, tid, ["Vela aromatica"])

    await _vender(client, auth_headers, db_session, sample_tenant, [_factura("A")])
    r = await _vender(client, auth_headers, db_session, sample_tenant, [_factura("B")])

    assert len(await _ventas(db_session, tid)) == 2, f"la Factura B se perdió: {r}"
    assert not await _otros(db_session, tid)
    claves = await _claves(db_session, tid)
    assert claves == [
        "k2:doc:propio:factura:a:1:123", "k2:doc:propio:factura:b:1:123"
    ], claves

    # Reimport de las dos: nada se duplica.
    await _vender(
        client, auth_headers, db_session, sample_tenant, [_factura("A"), _factura("B")]
    )
    assert len(await _ventas(db_session, tid)) == 2


async def test_factura_a_y_b_de_un_archivo_viejo_no_se_duplican_al_reimportar(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Con la regla vieja, A y B del mismo número en un archivo eran UN documento
    (una identidad ``k1`` con la huella de los dos renglones). El reimport con la
    regla de hoy tiene que reconocer ese grupo entero: si comparara cada factura
    sola contra la huella del grupo, las mandaría a «Otros» sin necesidad."""
    tid = sample_tenant.tenant_id
    await productos_con_compra(db_session, tid, ["Vela aromatica"])
    filas = [_factura("A", 1200), _factura("B", 900)]
    with _con_regla_vieja(monkeypatch):
        await _vender(client, auth_headers, db_session, sample_tenant, filas)
    assert len(await _ventas(db_session, tid)) == 2
    assert await _claves(db_session, tid) == ["k1:doc:propio:factura:1:123"]

    r = await _vender(client, auth_headers, db_session, sample_tenant, filas)
    assert len(await _ventas(db_session, tid)) == 2, f"el reimport duplicó: {r}"
    assert not await _otros(db_session, tid)
    # No se migra nada: la identidad vieja sigue siendo la que protege, y las
    # reclamaciones nuevas sin efecto se devolvieron al cierre.
    assert await _claves(db_session, tid) == ["k1:doc:propio:factura:1:123"]


async def test_factura_vieja_reimportada_sola_se_reconoce_y_la_otra_letra_va_a_revision(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La A se importó con la regla vieja. Reimportarla no la duplica. La B con
    el mismo número y otro total, contra una identidad vieja que no guardó la
    letra, no se puede distinguir de «la A corregida»: va a «Otros» con la clave
    VIEJA, así que importarla desde ahí exige la decisión explícita (409)."""
    tid = sample_tenant.tenant_id
    await productos_con_compra(db_session, tid, ["Vela aromatica"])
    with _con_regla_vieja(monkeypatch):
        await _vender(client, auth_headers, db_session, sample_tenant, [_factura("A")])

    await _vender(client, auth_headers, db_session, sample_tenant, [_factura("A")])
    assert len(await _ventas(db_session, tid)) == 1, "el reimport de la A la duplicó"

    await _vender(client, auth_headers, db_session, sample_tenant, [_factura("B", 900)])
    assert len(await _ventas(db_session, tid)) == 1
    otros = await _otros(db_session, tid)
    assert len(otros) == 1
    assert (otros[0].row_data or {}).get("__identity_key__") == "k1:doc:propio:factura:1:123"
    assert "letra" in (otros[0].context_label or "")


async def test_factura_sin_letra_de_un_archivo_viejo_no_se_duplica(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """«Factura» sin letra ya no tiene clave (no se sabe si es A o B). Pero el
    archivo viejo sí la tuvo: el reimport tiene que seguir reconociéndola por
    la clave guardada, no caer a «sin clave» y cargarla otra vez."""
    tid = sample_tenant.tenant_id
    await productos_con_compra(db_session, tid, ["Vela aromatica"])
    filas = [_factura("", tipo="Factura")]
    with _con_regla_vieja(monkeypatch):
        await _vender(client, auth_headers, db_session, sample_tenant, filas)
    assert len(await _ventas(db_session, tid)) == 1

    await _vender(client, auth_headers, db_session, sample_tenant, filas)
    assert len(await _ventas(db_session, tid)) == 1, "la factura sin letra se duplicó"


async def test_factura_sin_letra_nueva_no_produce_clave(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Sin letra y sin nada guardado: no hay clave fuerte (ante la duda, no hay
    clave). Se importa por el camino de siempre y no se reclama identidad."""
    tid = sample_tenant.tenant_id
    await productos_con_compra(db_session, tid, ["Vela aromatica"])
    await _vender(
        client, auth_headers, db_session, sample_tenant, [_factura("", tipo="Factura")]
    )
    assert len(await _ventas(db_session, tid)) == 1
    assert not await _claves(db_session, tid)


async def test_el_conflicto_contra_una_clave_vieja_no_se_importa_desde_otros_sin_decidir(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La fila que chocó con la identidad vieja lleva la clave vieja: el candado
    de «Otros» la encuentra tomada. Si llevara la nueva (que se devolvió al
    cierre), clasificarla desde «Otros» la aplicaría sin preguntar."""
    tid = sample_tenant.tenant_id
    await productos_con_compra(db_session, tid, ["Vela aromatica"])
    with _con_regla_vieja(monkeypatch):
        await _vender(client, auth_headers, db_session, sample_tenant, [_factura("A")])
    await _vender(client, auth_headers, db_session, sample_tenant, [_factura("B", 900)])
    (registro,) = await _otros(db_session, tid)

    resp = await client.post(
        f"/api/v1/others/{registro.id}/reclassify",
        json={
            "entity_type": "expense",
            "fields": {
                "amount": "900",
                "category": "OTHER",
                "expense_date": "2024-03-05T00:00:00",
                "description": "Factura B",
            },
        },
        headers=auth_headers,
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["code"] == "IMPORT_IDENTITY_TAKEN"
    db_session.expunge_all()
    assert not await _gastos(db_session, tid)
    assert len(await _ventas(db_session, tid)) == 1
    vinculos = (
        await db_session.execute(
            select(OperationIdentityLink).where(OperationIdentityLink.tenant_id == tid)
        )
    ).scalars().all()
    assert len(vinculos) == 1
