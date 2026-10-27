"""I6-7 — resolver una venta o un gasto desde «Otros» reclama la identidad del
comprobante.

Revisión profunda, etapa 2. Una venta sin compra previa (B·5e) se saca a «Otros»
y su import suelta la identidad del comprobante, para que el reintento la pueda
cargar. Pero la fila de «Otros» no la guardaba: el dueño cargaba la compra,
resolvía la fila (reclassify, link-product, bulk-import) y la venta nacía SIN
identidad. Otra exportación con ese comprobante (períodos solapados) lo veía
NUEVO y lo cargaba otra vez: plata y stock duplicados. Lo mismo con la fila de
conflicto aplicada «pese al conflicto»: sin vínculo, borrar el archivo de la
otra operación soltaba la clave y el reimport duplicaba.

Ahora la fila lleva la clave (``IDENTITY_CLAIM_KEY``, que no dispara el 409 del
renglón hermano) y su contenido, y el efecto que nace de resolverla la reclama
con el mismo candado que el import. Y al revés: si mientras la fila esperaba
OTRO archivo ya cargó el comprobante, resolverla exige decidir (409).
"""

from __future__ import annotations

import io
import unittest.mock
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import stock_service
from app.application.services.file_parsing import parse_uploaded_content
from app.application.services.ingestion_import_service import (
    IDENTITY_CLAIM_KEY,
    IDENTITY_HASH_KEY,
    IDENTITY_REF_KEY,
)
from app.application.services.reparacion_ventas_sin_compra_service import reparar, revertir
from app.domain.business_time import today_ar
from app.domain.contrato_de_costo import ContratoDeCosto
from app.persistence.db.session import get_db_session
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.operation_identity import OperationIdentity, OperationIdentityLink
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord
from app.tests._respaldo import producto_con_compra

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker, cada confirm paga ~5s de reintentos de kombu (fail-safe)."""


def _libro(
    headers: list[str], filas: list[list[Any]], hoja: str, *, con_otra_hoja: bool = False
) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = hoja
    ws.append(headers)
    for fila in filas:
        ws.append(fila)
    if con_otra_hoja:
        # Una segunda solapa: el libro va por el camino multihoja del import.
        notas = wb.create_sheet("Notas")
        notas.append(["nota"])
        notas.append(["exportado del sistema de facturación"])
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
    con_otra_hoja: bool = False,
) -> uuid.UUID:
    nombre = f"{uuid.uuid4().hex[:8]}.xlsx"
    contenido = _libro(headers, filas, hoja, con_otra_hoja=con_otra_hoja)
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
                {"source_column": c, "target_field": t}
                # Con dos solapas el mapeo es de la hoja (si no, no la alcanza).
                | ({"context_id": f"sheet:{hoja}"} if con_otra_hoja else {})
                for c, t in mapeo.items()
            ],
            "confirmed_fields": campos,
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    db.expunge_all()
    return record.id


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
_VELA = "Vela aromatica"
_DIFUSOR = "Difusor bambu"


def _renglon(articulo: str = _VELA, total: int = 1200, nro: str = "00000123") -> list[Any]:
    return ["2024-03-05", "Factura B", "0001", nro, "Ana", articulo, 1, total]


async def _vender(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db: AsyncSession,
    tenant: Tenant,
    filas: list[list[Any]],
    *,
    con_otra_hoja: bool = False,
) -> uuid.UUID:
    return await _confirmar(
        client, auth_headers, db, tenant,
        headers=_H_VENTAS, filas=filas, mapeo=_MAPEO_VENTAS, campos={"ventas": True},
        hoja="Ventas", con_otra_hoja=con_otra_hoja,
    )


async def _ventas(db: AsyncSession, tid: uuid.UUID) -> list[SaleEntry]:
    db.expunge_all()
    return list(
        (
            await db.execute(
                select(SaleEntry).where(
                    SaleEntry.tenant_id == tid, SaleEntry.voided_at.is_(None)
                )
            )
        ).scalars().all()
    )


async def _gastos(db: AsyncSession, tid: uuid.UUID) -> list[ExpenseEntry]:
    db.expunge_all()
    return list(
        (
            await db.execute(
                select(ExpenseEntry).where(
                    ExpenseEntry.tenant_id == tid, ExpenseEntry.voided_at.is_(None)
                )
            )
        ).scalars().all()
    )


async def _pendientes(db: AsyncSession, tid: uuid.UUID) -> list[UnclassifiedRecord]:
    db.expunge_all()
    return list(
        (
            await db.execute(
                select(UnclassifiedRecord).where(
                    UnclassifiedRecord.tenant_id == tid,
                    UnclassifiedRecord.status == "PENDING",
                )
            )
        ).scalars().all()
    )


async def _vinculos_de(db: AsyncSession, entity_id: uuid.UUID) -> list[OperationIdentityLink]:
    return list(
        (
            await db.execute(
                select(OperationIdentityLink).where(
                    OperationIdentityLink.entity_id == entity_id
                )
            )
        ).scalars().all()
    )


def _venta_de(product_id: uuid.UUID, total: str = "1200") -> dict[str, Any]:
    return {
        "amount": total,
        "quantity": 1,
        "transaction_date": "2024-03-05T00:00:00",
        "payment_method": "cash",
        "product_id": str(product_id),
    }


async def _reclasificar(
    client: AsyncClient,
    auth_headers: dict[str, str],
    registro: UnclassifiedRecord,
    fields: dict[str, Any],
    *,
    entity_type: str = "sale",
    pese_al_conflicto: bool = False,
) -> Any:
    return await client.post(
        f"/api/v1/others/{registro.id}/reclassify",
        json={
            "entity_type": entity_type,
            "fields": fields,
            "aplicar_pese_al_conflicto": pese_al_conflicto,
        },
        headers=auth_headers,
    )


async def _venta_sin_producto_en_otros(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db: AsyncSession,
    tenant: Tenant,
    filas: list[list[Any]],
) -> UnclassifiedRecord:
    """Un archivo de ventas cuyo producto todavía no existe: la venta va a
    «Otros» (B·5e) y el import suelta su identidad."""
    await _vender(client, auth_headers, db, tenant, filas)
    (registro,) = [
        r for r in await _pendientes(db, tenant.tenant_id) if r.suggested_entity == "sale"
    ]
    return registro


# ── La venta sin compra previa, resuelta, y el reimport ──────────────────────


async def test_la_fila_de_otros_lleva_la_identidad_que_el_import_solto(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    registro = await _venta_sin_producto_en_otros(
        client, auth_headers, db_session, sample_tenant, [_renglon()]
    )
    assert str(registro.row_data[IDENTITY_CLAIM_KEY]).startswith("k2:doc:propio:")
    assert registro.row_data[IDENTITY_HASH_KEY]
    # No es un conflicto: la fila no frena por su propia identidad.
    assert IDENTITY_REF_KEY not in registro.row_data
    # La identidad se soltó (el reintento con la compra cargada puede entrar).
    assert not (
        await db_session.execute(
            select(OperationIdentity).where(OperationIdentity.tenant_id == tid)
        )
    ).scalars().all()


@pytest.mark.parametrize("camino", ["reclassify", "link-product"])
async def test_resolver_la_venta_y_reimportar_el_comprobante_no_la_duplica(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    camino: str,
) -> None:
    """El escenario del hallazgo: venta a «Otros», el dueño carga la compra, la
    resuelve, y después sube otra exportación con el mismo comprobante."""
    tid = sample_tenant.tenant_id
    registro = await _venta_sin_producto_en_otros(
        client, auth_headers, db_session, sample_tenant, [_renglon()]
    )
    vela = await producto_con_compra(db_session, tid, nombre=_VELA)
    if camino == "reclassify":
        r = await _reclasificar(client, auth_headers, registro, _venta_de(vela.id))
    else:
        r = await client.post(
            f"/api/v1/others/{registro.id}/link-product",
            json={"product_id": str(vela.id)},
            headers=auth_headers,
        )
    assert r.status_code == 200, r.text
    (resuelta,) = await _ventas(db_session, tid)
    assert len(await _vinculos_de(db_session, resuelta.id)) == 1

    await _vender(client, auth_headers, db_session, sample_tenant, [_renglon()])
    assert [v.id for v in await _ventas(db_session, tid)] == [resuelta.id], (
        "el reimport del mismo comprobante volvió a cargar la venta resuelta"
    )


async def test_el_renglon_hermano_vivo_no_frena_y_comparten_la_identidad(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Factura de dos renglones: uno con compra (queda), otro sin producto (a
    «Otros»). La identidad sigue tomada por el hermano del MISMO archivo: eso no
    es un duplicado y resolver la fila no da 409."""
    tid = sample_tenant.tenant_id
    await producto_con_compra(db_session, tid, nombre=_VELA)
    registro = await _venta_sin_producto_en_otros(
        client, auth_headers, db_session, sample_tenant,
        [_renglon(_VELA), _renglon(_DIFUSOR, 800)],
    )
    (hermana,) = await _ventas(db_session, tid)
    difusor = await producto_con_compra(db_session, tid, nombre=_DIFUSOR)

    r = await _reclasificar(client, auth_headers, registro, _venta_de(difusor.id, "800"))
    assert r.status_code == 200, r.text
    resuelta = next(v for v in await _ventas(db_session, tid) if v.id != hermana.id)
    (v_hermana,) = await _vinculos_de(db_session, hermana.id)
    (v_resuelta,) = await _vinculos_de(db_session, resuelta.id)
    assert v_hermana.identity_id == v_resuelta.identity_id

    await _vender(
        client, auth_headers, db_session, sample_tenant,
        [_renglon(_VELA), _renglon(_DIFUSOR, 800)],
    )
    assert len(await _ventas(db_session, tid)) == 2


async def test_si_otro_archivo_ya_cargo_el_comprobante_resolver_exige_decidir(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """El orden inverso: mientras la fila esperaba, otra exportación (con la
    compra ya cargada) cargó la venta. Resolver la fila la duplicaría: 409 y
    nada escrito; «pese al conflicto» es la decisión explícita del dueño."""
    tid = sample_tenant.tenant_id
    registro = await _venta_sin_producto_en_otros(
        client, auth_headers, db_session, sample_tenant, [_renglon()]
    )
    vela = await producto_con_compra(db_session, tid, nombre=_VELA)
    await _vender(client, auth_headers, db_session, sample_tenant, [_renglon()])
    (cargada,) = await _ventas(db_session, tid)

    r = await _reclasificar(client, auth_headers, registro, _venta_de(vela.id))
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "IMPORT_IDENTITY_TAKEN"
    assert [v.id for v in await _ventas(db_session, tid)] == [cargada.id]
    assert [p.id for p in await _pendientes(db_session, tid)] == [registro.id]

    r = await client.post(
        f"/api/v1/others/{registro.id}/link-product",
        json={"product_id": str(vela.id)},
        headers=auth_headers,
    )
    assert r.status_code == 409, r.text
    r = await client.post(
        "/api/v1/others/bulk-import", json={"entity_type": "sale"}, headers=auth_headers
    )
    assert r.json()["imported_sales"] == 0, r.text
    assert len(await _ventas(db_session, tid)) == 1

    r = await _reclasificar(
        client, auth_headers, registro, _venta_de(vela.id), pese_al_conflicto=True
    )
    assert r.status_code == 200, r.text
    assert len(await _ventas(db_session, tid)) == 2


async def test_borrar_el_archivo_de_la_fila_suelta_la_identidad_de_lo_resuelto(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """La venta resuelta es del archivo de la fila: borrarlo la revierte y suelta
    la identidad (``liberar_identidades_de``), así que volver a subir el
    comprobante lo carga una vez — ni cero ni dos."""
    tid = sample_tenant.tenant_id
    registro = await _venta_sin_producto_en_otros(
        client, auth_headers, db_session, sample_tenant, [_renglon()]
    )
    vela = await producto_con_compra(db_session, tid, nombre=_VELA)
    r = await _reclasificar(client, auth_headers, registro, _venta_de(vela.id))
    assert r.status_code == 200, r.text

    r = await client.delete(
        f"/api/v1/ingestion/files/{registro.uploaded_file_id}?confirm=true",
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    assert not await _ventas(db_session, tid)
    assert not (
        await db_session.execute(
            select(OperationIdentity).where(OperationIdentity.tenant_id == tid)
        )
    ).scalars().all()

    await _vender(client, auth_headers, db_session, sample_tenant, [_renglon()])
    assert len(await _ventas(db_session, tid)) == 1


# ── La fila de conflicto aplicada pese al conflicto ──────────────────────────


@pytest.mark.parametrize("multihoja", [False, True], ids=["tabla_suelta", "multihoja"])
async def test_el_conflicto_aplicado_igual_queda_colgado_de_la_identidad(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    multihoja: bool,
) -> None:
    """Archivo A carga la factura; el B trae la misma con otro total → «Otros»
    por conflicto. El dueño la aplica igual. Si el efecto quedara sin vínculo,
    borrar A soltaría la clave y reimportar B la cargaría otra vez."""
    tid = sample_tenant.tenant_id
    vela = await producto_con_compra(db_session, tid, nombre=_VELA)
    archivo_a = await _vender(
        client, auth_headers, db_session, sample_tenant, [_renglon()], con_otra_hoja=multihoja
    )
    await _vender(
        client, auth_headers, db_session, sample_tenant, [_renglon(total=900)],
        con_otra_hoja=multihoja,
    )
    # La solapa «Notas» también va a «Otros» (sin entidad): no es la de la venta.
    (registro,) = [r for r in await _pendientes(db_session, tid) if r.suggested_entity]
    assert registro.row_data[IDENTITY_REF_KEY]
    assert registro.row_data[IDENTITY_HASH_KEY]

    r = await _reclasificar(
        client, auth_headers, registro, _venta_de(vela.id, "900"), pese_al_conflicto=True
    )
    assert r.status_code == 200, r.text
    aplicada = next(v for v in await _ventas(db_session, tid) if v.amount == Decimal("900"))
    assert len(await _vinculos_de(db_session, aplicada.id)) == 1

    r = await client.delete(
        f"/api/v1/ingestion/files/{archivo_a}?confirm=true", headers=auth_headers
    )
    assert r.status_code == 200, r.text
    assert [v.id for v in await _ventas(db_session, tid)] == [aplicada.id]

    await _vender(
        client, auth_headers, db_session, sample_tenant, [_renglon(total=900)],
        con_otra_hoja=multihoja,
    )
    assert [v.id for v in await _ventas(db_session, tid)] == [aplicada.id], (
        "el reimport duplicó la venta aplicada pese al conflicto"
    )


async def test_el_conflicto_importado_en_lote_toma_la_identidad(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """La fila de conflicto entra por el lote cuando la otra operación ya no
    existe (se borró su archivo). Lo que el lote importa también toma la
    identidad: reimportar el mismo archivo no lo duplica."""
    tid = sample_tenant.tenant_id
    await producto_con_compra(db_session, tid, nombre=_VELA)
    archivo_a = await _vender(client, auth_headers, db_session, sample_tenant, [_renglon()])
    await _vender(client, auth_headers, db_session, sample_tenant, [_renglon(total=900)])
    r = await client.delete(
        f"/api/v1/ingestion/files/{archivo_a}?confirm=true", headers=auth_headers
    )
    assert r.status_code == 200, r.text
    r = await client.post(
        "/api/v1/others/bulk-import", json={"entity_type": "sale"}, headers=auth_headers
    )
    assert r.json()["imported_sales"] == 1, r.text
    (importada,) = await _ventas(db_session, tid)
    assert len(await _vinculos_de(db_session, importada.id)) == 1

    await _vender(client, auth_headers, db_session, sample_tenant, [_renglon(total=900)])
    assert [v.id for v in await _ventas(db_session, tid)] == [importada.id]


async def test_el_lote_no_importa_la_fila_cuyo_comprobante_cargo_otro_archivo(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Una fila con la identidad que su import soltó, y el comprobante ya cargado
    por OTRO archivo: el lote no decide, queda para el modal (``needs_manual``)."""
    tid = sample_tenant.tenant_id
    vela = await producto_con_compra(db_session, tid, nombre=_VELA)
    otro = await _vender(client, auth_headers, db_session, sample_tenant, [_renglon()])
    (cargada,) = await _ventas(db_session, tid)
    (vinculo,) = await _vinculos_de(db_session, cargada.id)
    assert vinculo.source_upload_id == otro
    clave = (
        await db_session.execute(
            select(OperationIdentity.identity_key).where(
                OperationIdentity.id == vinculo.identity_id
            )
        )
    ).scalar_one()
    archivo = UploadedFile(
        tenant_id=tid, uploaded_by=None, original_filename="v.xlsx", s3_key="v.xlsx",
        content_type=_XLSX_MIME, size_bytes=1, purpose="ingestion", status="uploaded",
    )
    db_session.add(archivo)
    await db_session.flush()
    registro = UnclassifiedRecord(
        tenant_id=tid, uploaded_file_id=archivo.id, source="ingestion",
        context_label="Venta sin compra previa", suggested_entity="sale",
        status="PENDING",
        row_data={
            "fecha": "05/03/2024", "total": "1200", "cantidad": "1", "producto": vela.name,
            IDENTITY_CLAIM_KEY: clave, IDENTITY_HASH_KEY: "H",
            "__identity_entity__": "sale",
        },
    )
    db_session.add(registro)
    await db_session.commit()

    r = await client.post(
        "/api/v1/others/bulk-import", json={"entity_type": "sale"}, headers=auth_headers
    )
    assert r.json() == {
        "imported_sales": 0, "imported_expenses": 0, "skipped": 0, "needs_manual": 1,
    }, r.text
    assert [v.id for v in await _ventas(db_session, tid)] == [cargada.id]


# Libro de compras: `proveedor` está para que el clasificador lo lea como tal.
_H_COMPRAS = [
    "fecha", "tipo", "pto", "nro", "cuit", "proveedor", "articulo", "cantidad", "total",
]
_MAPEO_COMPRAS = {
    "fecha": "expense_date",
    "tipo": "document_type",
    "pto": "document_series",
    "nro": "invoice_number",
    "cuit": "supplier_cuit",
    "proveedor": "supplier_name",
    "articulo": "product_name",
    "cantidad": "quantity",
    "total": "amount",
}


def _compra(total: int) -> list[Any]:
    return [
        "2024-03-05", "Factura A", "0001", "00000077", "30-71234567-8", "Sur", _VELA, 5,
        total,
    ]


async def _comprar(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db: AsyncSession,
    tenant: Tenant,
    total: int,
) -> uuid.UUID:
    return await _confirmar(
        client, auth_headers, db, tenant,
        headers=_H_COMPRAS, filas=[_compra(total)], mapeo=_MAPEO_COMPRAS,
        campos={"gastos": True}, hoja="Compras",
    )


async def test_la_compra_en_conflicto_resuelta_con_articulos_queda_colgada(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Lo mismo por ``resolve-purchase`` (B·5b): la compra que el dueño registra
    pese al conflicto toma la identidad del comprobante."""
    tid = sample_tenant.tenant_id
    vela = await producto_con_compra(db_session, tid, nombre=_VELA)
    archivo_a = await _comprar(client, auth_headers, db_session, sample_tenant, 6000)
    await _comprar(client, auth_headers, db_session, sample_tenant, 5000)
    (registro,) = await _pendientes(db_session, tid)
    assert registro.row_data[IDENTITY_REF_KEY]

    r = await client.post(
        f"/api/v1/others/{registro.id}/resolve-purchase",
        json={
            "target_product_id": str(vela.id),
            "quantity": 5,
            "amount": "5000",
            "transaction_date": "2024-03-05T00:00:00",
            "payment_method": "transfer",
            "aplicar_pese_al_conflicto": True,
        },
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    aplicada = next(g for g in await _gastos(db_session, tid) if g.amount == Decimal("5000"))
    assert len(await _vinculos_de(db_session, aplicada.id)) == 1

    r = await client.delete(
        f"/api/v1/ingestion/files/{archivo_a}?confirm=true", headers=auth_headers
    )
    assert r.status_code == 200, r.text
    await _comprar(client, auth_headers, db_session, sample_tenant, 5000)
    assert [g.id for g in await _gastos(db_session, tid)] == [aplicada.id]


# ── La reparación B·5g: la fila lleva la identidad, y el revert no se confunde ──
_DIA = today_ar() - timedelta(days=10)


async def _venta_importada(
    db: AsyncSession, tid: uuid.UUID, pid: uuid.UUID, file_id: uuid.UUID, ident: uuid.UUID
) -> SaleEntry:
    venta = SaleEntry(
        tenant_id=tid, amount=Decimal("1000"), quantity=1, product_id=pid,
        transaction_date=datetime.combine(_DIA, time(12)), payment_method="cash",
        provenance="REAL", source_upload_id=file_id,
        source_row_ref=f"fila-{uuid.uuid4().hex[:6]}",
    )
    db.add(venta)
    await db.flush()
    await stock_service.decrement_for_sale(venta, db)
    db.add(
        OperationIdentityLink(
            identity_id=ident, tenant_id=tid, entity_type="sale", entity_id=venta.id,
            source_upload_id=file_id,
        )
    )
    await db.flush()
    return venta


async def test_resolver_un_renglon_reparado_no_hace_que_el_revert_pierda_al_hermano(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """La reparación saca a «Otros» los dos renglones de un comprobante sin
    compra previa. El dueño resuelve uno (toma la identidad); el revert tiene
    que restaurar el otro, no darlo por «re-importado» y borrar su fila."""
    tid = sample_tenant.tenant_id
    archivo = UploadedFile(
        tenant_id=tid, uploaded_by=None, original_filename="v.xlsx", s3_key="v.xlsx",
        content_type=_XLSX_MIME, size_bytes=1, purpose="ingestion", status="uploaded",
    )
    vela = Product(tenant_id=tid, name=_VELA, sale_price_ars=Decimal("1000"), stock_units=10)
    db_session.add_all([archivo, vela])
    await db_session.flush()
    ident = OperationIdentity(
        tenant_id=tid, identity_key="k2:doc:propio:factura:b:1:9", content_hash="H",
        entity_type="sale", first_seen_upload_id=archivo.id,
    )
    db_session.add(ident)
    await db_session.flush()
    una = await _venta_importada(db_session, tid, vela.id, archivo.id, ident.id)
    otra = await _venta_importada(db_session, tid, vela.id, archivo.id, ident.id)
    await db_session.commit()

    rep = await reparar(db_session, tid)
    await db_session.commit()
    assert rep.run_id is not None
    filas = await _pendientes(db_session, tid)
    assert len(filas) == 2
    assert all(f.row_data[IDENTITY_CLAIM_KEY] == "k2:doc:propio:factura:b:1:9" for f in filas)
    assert all(f.row_data[IDENTITY_HASH_KEY] == "H" for f in filas)

    # El dueño carga el stock inicial (con costo) y resuelve UN renglón.
    await stock_service.registrar_apertura_manual(
        vela.id, tid, 5, ContratoDeCosto(unit_cost_final=Decimal("10")),
        db_session, fecha=datetime.combine(_DIA - timedelta(days=5), time(9)),
        confirmado_por=None,
    )
    await db_session.commit()
    resuelta_de = filas[0]
    r = await _reclasificar(
        client, auth_headers, resuelta_de,
        {**_venta_de(vela.id, "1000"), "transaction_date": f"{_DIA.isoformat()}T12:00:00"},
    )
    assert r.status_code == 200, r.text

    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        rev = await revertir(db_session, rep.run_id)
    await db_session.commit()
    assert rev.reimportadas == []
    assert len(rev.restauradas) == 1
    vivas = {v.id for v in await _ventas(db_session, tid)}
    assert len(vivas) == 2
    assert {una.id, otra.id} & vivas, "el revert perdió el renglón que seguía pendiente"


_H_SERVICIOS = ["fecha", "tipo", "pto", "nro", "cuit", "proveedor", "concepto", "total"]
_MAPEO_SERVICIOS = {
    "fecha": "expense_date",
    "tipo": "document_type",
    "pto": "document_series",
    "nro": "invoice_number",
    "cuit": "supplier_cuit",
    "proveedor": "supplier_name",
    "concepto": "category",
    "total": "amount",
}


async def test_el_gasto_en_conflicto_reclasificado_queda_colgado(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """La rama gasto de ``reclassify``: el gasto aplicado pese al conflicto toma
    la identidad, y borrar el archivo del otro no la suelta."""
    tid = sample_tenant.tenant_id

    async def _factura_de_luz(total: int) -> uuid.UUID:
        return await _confirmar(
            client, auth_headers, db_session, sample_tenant,
            headers=_H_SERVICIOS,
            filas=[["2024-03-05", "Factura A", "0003", "00000555", "30-71234567-8",
                    "Edesur", "Luz", total]],
            mapeo=_MAPEO_SERVICIOS, campos={"gastos": True}, hoja="Gastos",
        )

    archivo_a = await _factura_de_luz(5000)
    await _factura_de_luz(4000)
    (registro,) = await _pendientes(db_session, tid)
    assert registro.row_data[IDENTITY_REF_KEY]

    r = await _reclasificar(
        client, auth_headers, registro,
        {
            "amount": "4000", "category": "Luz", "expense_date": "2024-03-05T00:00:00",
            "description": "Factura de luz",
        },
        entity_type="expense", pese_al_conflicto=True,
    )
    assert r.status_code == 200, r.text
    aplicado = next(g for g in await _gastos(db_session, tid) if g.amount == Decimal("4000"))
    assert len(await _vinculos_de(db_session, aplicado.id)) == 1

    r = await client.delete(
        f"/api/v1/ingestion/files/{archivo_a}?confirm=true", headers=auth_headers
    )
    assert r.status_code == 200, r.text
    await _factura_de_luz(4000)
    assert [g.id for g in await _gastos(db_session, tid)] == [aplicado.id]


# ── Revisión adversarial de #208 ─────────────────────────────────────────────


async def test_borrar_el_otro_archivo_tras_aplicar_pese_al_conflicto_no_la_da_por_aplicada(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """F1 carga el comprobante por 1200; F2 lo trae por 1500 y el dueño lo aplica
    igual (dos ventas). Borra F1 y lo vuelve a subir: la identidad seguía con la
    huella de F1 y el reimport la daba por «ya aplicada» — la venta de 1200 se
    perdía en silencio. Ahora va a «Otros» como conflicto, para decidir."""
    tid = sample_tenant.tenant_id
    vela = await producto_con_compra(db_session, tid, nombre=_VELA)
    f1 = await _vender(client, auth_headers, db_session, sample_tenant, [_renglon(total=1200)])
    await _vender(client, auth_headers, db_session, sample_tenant, [_renglon(total=1500)])
    (registro,) = await _pendientes(db_session, tid)
    r = await _reclasificar(
        client, auth_headers, registro, _venta_de(vela.id, "1500"), pese_al_conflicto=True
    )
    assert r.status_code == 200, r.text
    r = await client.delete(f"/api/v1/ingestion/files/{f1}?confirm=true", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert [v.amount for v in await _ventas(db_session, tid)] == [Decimal("1500")]

    await _vender(client, auth_headers, db_session, sample_tenant, [_renglon(total=1200)])
    assert [v.amount for v in await _ventas(db_session, tid)] == [Decimal("1500")]
    (vuelta,) = await _pendientes(db_session, tid)
    assert vuelta.row_data[IDENTITY_REF_KEY], "la venta de 1200 no quedó para decidir"


async def test_revert_b5g_tras_resolver_un_renglon_en_lote_restaura_al_hermano(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Como el de arriba, pero el renglón se resuelve con «importar sugeridos»:
    la venta del lote también lleva la referencia de su fila."""
    tid = sample_tenant.tenant_id
    archivo = UploadedFile(
        tenant_id=tid, uploaded_by=None, original_filename="v.xlsx", s3_key="v.xlsx",
        content_type=_XLSX_MIME, size_bytes=1, purpose="ingestion", status="uploaded",
    )
    vela = Product(tenant_id=tid, name=_VELA, sale_price_ars=Decimal("1000"), stock_units=10)
    difusor = Product(
        tenant_id=tid, name=_DIFUSOR, sale_price_ars=Decimal("1000"), stock_units=10
    )
    db_session.add_all([archivo, vela, difusor])
    await db_session.flush()
    ident = OperationIdentity(
        tenant_id=tid, identity_key="k2:doc:propio:factura:b:1:9", content_hash="H",
        entity_type="sale", first_seen_upload_id=archivo.id,
    )
    db_session.add(ident)
    await db_session.flush()
    await _venta_importada(db_session, tid, vela.id, archivo.id, ident.id)
    otra = await _venta_importada(db_session, tid, difusor.id, archivo.id, ident.id)
    await db_session.commit()
    rep = await reparar(db_session, tid)
    await db_session.commit()
    assert rep.run_id is not None
    filas = await _pendientes(db_session, tid)
    assert len(filas) == 2
    # El lote no lee «1000.00» sin el convenio de su columna (vecino, aparte):
    # el monto se deja entero para que la fila de la vela entre.
    for fila in filas:
        db_session.add(fila)
        fila.row_data = {**fila.row_data, "monto": "1000"}
    await db_session.commit()

    # Stock inicial sólo para la vela: el lote resuelve esa fila, la otra espera.
    await stock_service.registrar_apertura_manual(
        vela.id, tid, 5, ContratoDeCosto(unit_cost_final=Decimal("10")),
        db_session, fecha=datetime.combine(_DIA - timedelta(days=5), time(9)),
        confirmado_por=None,
    )
    await db_session.commit()
    r = await client.post(
        "/api/v1/others/bulk-import", json={"entity_type": "sale"}, headers=auth_headers
    )
    assert r.json()["imported_sales"] == 1, r.text

    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        rev = await revertir(db_session, rep.run_id)
    await db_session.commit()
    assert rev.reimportadas == []
    assert otra.id in {v.id for v in await _ventas(db_session, tid)}


async def test_el_lote_no_importa_dos_veces_el_comprobante_de_dos_archivos(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Dos exportaciones solapadas dejaron el mismo comprobante en «Otros». Los
    reclamos del lote van al final: sin mirar el propio lote, entraban las dos.
    La primera entra; la de otro archivo, al modal. Un renglón hermano del MISMO
    archivo sí entra."""
    tid = sample_tenant.tenant_id
    vela = await producto_con_compra(db_session, tid, nombre=_VELA)
    archivos = [
        UploadedFile(
            tenant_id=tid, uploaded_by=None, original_filename=f"{n}.xlsx", s3_key=f"{n}",
            content_type=_XLSX_MIME, size_bytes=1, purpose="ingestion", status="uploaded",
        )
        for n in ("a", "b")
    ]
    db_session.add_all(archivos)
    await db_session.flush()
    clave = "k2:doc:propio:factura:b:1:55"
    for archivo, total in ((archivos[0], "1200"), (archivos[0], "800"), (archivos[1], "1200")):
        db_session.add(
            UnclassifiedRecord(
                tenant_id=tid, uploaded_file_id=archivo.id, source="ingestion",
                context_label="Venta sin compra previa", suggested_entity="sale",
                status="PENDING",
                row_data={
                    # ISO, como la escribe B·5e: una fecha corta ambigua sin el
                    # convenio de su columna no entra en lote (I2-4).
                    "fecha": "2024-03-05", "total": total, "cantidad": "1",
                    "producto": vela.name, IDENTITY_CLAIM_KEY: clave,
                    IDENTITY_HASH_KEY: "H", "__identity_entity__": "sale",
                },
            )
        )
    await db_session.commit()

    r = await client.post(
        "/api/v1/others/bulk-import", json={"entity_type": "sale"}, headers=auth_headers
    )
    cuerpo = r.json()
    assert (cuerpo["imported_sales"], cuerpo["needs_manual"]) == (2, 1), r.text
    ventas = await _ventas(db_session, tid)
    assert {v.source_upload_id for v in ventas} == {archivos[0].id}
    (pendiente,) = await _pendientes(db_session, tid)
    assert pendiente.uploaded_file_id == archivos[1].id


async def test_si_otro_toma_el_comprobante_entre_el_chequeo_y_el_reclamo_da_409(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La carrera: el chequeo previo no ve a nadie y, cuando la fila reclama, otro
    archivo ya tomó el comprobante (otra resolución, otro import). Se simula con
    la respuesta del chequeo: «libre» antes, «tomada» después. El re-chequeo tras
    el reclamo da 409 y no queda nada escrito."""
    from app.api.v1 import others

    tid = sample_tenant.tenant_id
    registro = await _venta_sin_producto_en_otros(
        client, auth_headers, db_session, sample_tenant, [_renglon()]
    )
    vela = await producto_con_compra(db_session, tid, nombre=_VELA)
    registro_id = registro.id
    respuestas = iter([False, True])

    async def _carrera(*_a: Any, **_k: Any) -> bool:
        return next(respuestas)

    monkeypatch.setattr(others, "aplicada_desde_otro_origen", _carrera)

    # La sesión de la request como en producción (`get_db_session`): revierte si
    # el endpoint falla. La del fixture `client` no lo hace.
    async def _sesion_que_revierte() -> AsyncIterator[AsyncSession]:
        try:
            yield db_session
        except Exception:
            await db_session.rollback()
            raise

    app = client._transport.app  # type: ignore[attr-defined]
    app.dependency_overrides[get_db_session] = _sesion_que_revierte
    r = await _reclasificar(client, auth_headers, registro, _venta_de(vela.id))
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "IMPORT_IDENTITY_TAKEN"
    assert not await _ventas(db_session, tid)
    assert [p.id for p in await _pendientes(db_session, tid)] == [registro_id]
    assert not (
        await db_session.execute(
            select(OperationIdentityLink).where(OperationIdentityLink.tenant_id == tid)
        )
    ).scalars().all()
