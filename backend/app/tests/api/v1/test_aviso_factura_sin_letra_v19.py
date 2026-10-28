"""V-19 — «Factura A 0001-123» y «FC 0001-123» en el mismo archivo: entran las
dos, con un aviso de posible duplicado.

La A tiene clave ``k2`` (con la letra) y la FC la ``k1`` de siempre (sin): son
claves distintas y entre hermanos del mismo archivo nadie bloquea a nadie
(I6-4). Pueden ser dos ventas, y colapsarlas borraría plata; también pueden ser
la misma factura escrita dos veces. Decisión del dueño (2026-10-28): no se
colapsa nada, el confirm AVISA — mismo punto de venta y número, misma entidad,
mismo monto, y a una le falta la letra. Dos letras explícitas distintas (A y B)
son dos comprobantes y no se avisan.
"""

from __future__ import annotations

import io
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.file_parsing import parse_uploaded_content
from app.application.services.ingestion_import_service import insert_confirmed_data
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.tests._respaldo import productos_con_compra

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker, cada confirm paga ~5s de reintentos de kombu (fail-safe)."""


@pytest.fixture
def counts_del_import(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Los ``counts`` que devolvió el importador en cada confirm (la respuesta
    HTTP sólo trae los avisos)."""
    vistos: list[dict[str, Any]] = []
    async def espia(*args: Any, **kwargs: Any) -> Any:
        counts = await insert_confirmed_data(*args, **kwargs)
        vistos.append(counts)
        return counts

    monkeypatch.setattr("app.api.v1.ingestion.insert_confirmed_data", espia)
    return vistos


def _xlsx(
    headers: list[str], filas: list[list[Any]], hoja: str, *, con_otra_hoja: bool
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
) -> dict[str, Any]:
    nombre = f"{uuid.uuid4().hex[:8]}.xlsx"
    contenido = _xlsx(headers, filas, hoja, con_otra_hoja=con_otra_hoja)
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


def _aviso(cuerpo: dict[str, Any]) -> list[str]:
    return [w for w in cuerpo.get("warnings") or [] if "pueden estar repetidos" in w]


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


def _factura(tipo: str, total: int = 1200, nro: str = "00000123") -> list[Any]:
    return ["2024-03-05", tipo, "0001", nro, "Ana", "Vela aromatica", 1, total]


async def _vender(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db: AsyncSession,
    tenant: Tenant,
    filas: list[list[Any]],
    *,
    con_otra_hoja: bool = False,
) -> dict[str, Any]:
    return await _confirmar(
        client, auth_headers, db, tenant,
        headers=_H_VENTAS, filas=filas, mapeo=_MAPEO_VENTAS, campos={"ventas": True},
        hoja="Ventas", con_otra_hoja=con_otra_hoja,
    )


@pytest.mark.parametrize("con_otra_hoja", [False, True], ids=["una_hoja", "multihoja"])
async def test_factura_a_y_fc_con_el_mismo_numero_y_monto_entran_las_dos_y_avisa(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    counts_del_import: list[dict[str, Any]],
    con_otra_hoja: bool,
) -> None:
    """Las dos se cargan (no se pierde ninguna) y el confirm dice qué par
    revisar, con la fila y el comprobante como los escribe el archivo. Por los
    dos caminos del import: tabla suelta y libro de varias hojas."""
    tid = sample_tenant.tenant_id
    await productos_con_compra(db_session, tid, ["Vela aromatica"])
    filas = [_factura("Factura A"), _factura("FC")]

    cuerpo = await _vender(
        client, auth_headers, db_session, sample_tenant, filas, con_otra_hoja=con_otra_hoja
    )

    assert len(await _ventas(db_session, tid)) == 2, "se perdió una de las dos"
    avisos = _aviso(cuerpo)
    assert len(avisos) == 1, cuerpo.get("warnings")
    assert avisos[0].startswith("1 par(es)")
    assert "venta: fila 1 («Factura A 0001-00000123»)" in avisos[0]
    assert "fila 2 («FC 0001-00000123»)" in avisos[0]
    assert "Se cargaron los dos" in avisos[0]
    assert ("de la hoja «Ventas»" in avisos[0]) is con_otra_hoja
    assert counts_del_import[-1]["posibles_duplicados_sin_letra"] == 1
    assert len(counts_del_import[-1]["posibles_duplicados_sin_letra_muestras"]) == 1


async def test_factura_a_y_factura_b_con_el_mismo_numero_no_avisan(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    counts_del_import: list[dict[str, Any]],
) -> None:
    """Dos letras explícitas distintas son dos comprobantes (AFIP numera por
    letra): no hay nada que revisar."""
    tid = sample_tenant.tenant_id
    await productos_con_compra(db_session, tid, ["Vela aromatica"])
    filas = [_factura("Factura A"), _factura("Factura B")]

    cuerpo = await _vender(client, auth_headers, db_session, sample_tenant, filas)

    assert len(await _ventas(db_session, tid)) == 2
    assert not _aviso(cuerpo), cuerpo.get("warnings")
    assert "posibles_duplicados_sin_letra" not in counts_del_import[-1]


async def test_mismo_numero_con_otro_monto_no_avisa(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    counts_del_import: list[dict[str, Any]],
) -> None:
    """Con otro monto no es «la misma escrita dos veces»."""
    tid = sample_tenant.tenant_id
    await productos_con_compra(db_session, tid, ["Vela aromatica"])
    filas = [_factura("Factura A", 1200), _factura("FC", 900)]

    cuerpo = await _vender(client, auth_headers, db_session, sample_tenant, filas)

    assert len(await _ventas(db_session, tid)) == 2
    assert not _aviso(cuerpo), cuerpo.get("warnings")
    assert "posibles_duplicados_sin_letra" not in counts_del_import[-1]


async def test_el_aviso_cuenta_todos_los_pares_y_muestra_tres(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    counts_del_import: list[dict[str, Any]],
) -> None:
    """Cuatro pares: el total es 4, se muestran tres y se dice cuántos más."""
    tid = sample_tenant.tenant_id
    await productos_con_compra(db_session, tid, ["Vela aromatica"])
    filas: list[list[Any]] = []
    for n in range(4):
        nro = f"0000012{n}"
        filas += [_factura("Factura A", nro=nro), _factura("FC", nro=nro)]

    cuerpo = await _vender(client, auth_headers, db_session, sample_tenant, filas)

    assert len(await _ventas(db_session, tid)) == 8
    avisos = _aviso(cuerpo)
    assert len(avisos) == 1
    assert avisos[0].startswith("4 par(es)")
    assert avisos[0].count("fila ") == 6
    assert "y 1 más" in avisos[0]
    assert counts_del_import[-1]["posibles_duplicados_sin_letra"] == 4
    assert len(counts_del_import[-1]["posibles_duplicados_sin_letra_muestras"]) == 3


async def test_el_reimport_del_mismo_archivo_no_duplica_ni_vuelve_a_avisar(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """El aviso es sobre lo que ESTE confirm cargó: si las dos ya estaban
    aplicadas, no entró nada nuevo que revisar."""
    tid = sample_tenant.tenant_id
    await productos_con_compra(db_session, tid, ["Vela aromatica"])
    filas = [_factura("Factura A"), _factura("FC")]
    assert _aviso(await _vender(client, auth_headers, db_session, sample_tenant, filas))

    cuerpo = await _vender(client, auth_headers, db_session, sample_tenant, filas)

    assert len(await _ventas(db_session, tid)) == 2
    assert not _aviso(cuerpo), cuerpo.get("warnings")


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


def _compra(tipo: str) -> list[Any]:
    return [
        "2024-03-05", tipo, "0001", "00000077", "30-71234567-8", "Sur", "Vela aromatica",
        5, 5000,
    ]


async def test_compras_factura_a_y_fc_del_mismo_proveedor_avisan_y_entran_las_dos(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Lo mismo del lado de las compras (emisor = CUIT del proveedor)."""
    tid = sample_tenant.tenant_id
    cuerpo = await _confirmar(
        client, auth_headers, db_session, sample_tenant,
        headers=_H_COMPRAS, filas=[_compra("Factura A"), _compra("FC")],
        mapeo=_MAPEO_COMPRAS, campos={"gastos": True}, hoja="Compras",
    )

    assert len(await _gastos(db_session, tid)) == 2
    avisos = _aviso(cuerpo)
    assert len(avisos) == 1, cuerpo.get("warnings")
    assert "«FC 0001-00000077»" in avisos[0]


# ── Sólo se avisa de lo que entró ─────────────────────────────────────────────
async def test_si_las_dos_van_a_otros_no_avisa(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    counts_del_import: list[dict[str, Any]],
) -> None:
    """Sin compra previa del producto (B·5e) las dos van a «Otros»: «se cargaron
    los dos, anulá uno» sería falso."""
    tid = sample_tenant.tenant_id
    filas = [_factura("Factura A"), _factura("FC")]

    cuerpo = await _vender(client, auth_headers, db_session, sample_tenant, filas)

    assert not await _ventas(db_session, tid)
    assert not _aviso(cuerpo), cuerpo.get("warnings")
    assert "posibles_duplicados_sin_letra" not in counts_del_import[-1]


@pytest.mark.parametrize("con_otra_hoja", [False, True], ids=["una_hoja", "multihoja"])
async def test_si_una_entra_y_la_otra_va_a_otros_no_avisa(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    counts_del_import: list[dict[str, Any]],
    con_otra_hoja: bool,
) -> None:
    """La FC sin fecha va a «Otros» (F6-A2). Si el aviso mandara a anular una,
    el usuario anularía la ÚNICA venta cargada."""
    tid = sample_tenant.tenant_id
    await productos_con_compra(db_session, tid, ["Vela aromatica"])
    sin_fecha = _factura("FC")
    sin_fecha[0] = None
    filas = [_factura("Factura A"), sin_fecha]

    cuerpo = await _vender(
        client, auth_headers, db_session, sample_tenant, filas, con_otra_hoja=con_otra_hoja
    )

    assert len(await _ventas(db_session, tid)) == 1
    assert not _aviso(cuerpo), cuerpo.get("warnings")
    assert "posibles_duplicados_sin_letra" not in counts_del_import[-1]


# ── Tabla mixta: el mismo par como venta y como compra ───────────────────────
# `cliente` y `concepto` (sin mapear) están para que el clasificador lea la hoja
# como general (ventas Y gastos), que es lo que es un Libro Diario.
_H_DIARIO = [
    "fecha", "tipo", "pto", "nro", "cliente", "concepto", "articulo", "cantidad", "total",
    "cuit",
]
_MAPEO_DIARIO = {
    "fecha": "transaction_date",
    "tipo": "document_type",
    "pto": "document_series",
    "nro": "invoice_number",
    "articulo": "product_name",
    "cantidad": "quantity",
    "total": "amount",
    "cuit": "supplier_cuit",
}


def _asiento(tipo: str) -> list[Any]:
    return [
        "2024-03-05", tipo, "0001", "00000123", "Ana", "Venta", "Vela aromatica", 1, 1200,
        "30-71234567-8",
    ]


async def test_tabla_mixta_cuenta_el_par_una_vez_con_las_dos_entidades(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    counts_del_import: list[dict[str, Any]],
) -> None:
    """Un Libro Diario con CUIT produce venta Y gasto desde las mismas filas: el
    par sale de los dos planes, pero son las mismas dos filas — un par."""
    tid = sample_tenant.tenant_id
    await productos_con_compra(db_session, tid, ["Vela aromatica"])

    cuerpo = await _confirmar(
        client, auth_headers, db_session, sample_tenant,
        headers=_H_DIARIO, filas=[_asiento("Factura A"), _asiento("FC")],
        mapeo=_MAPEO_DIARIO, campos={"ventas": True, "gastos": True}, hoja="Diario",
    )

    assert len(await _ventas(db_session, tid)) == 2
    assert len(await _gastos(db_session, tid)) == 2
    avisos = _aviso(cuerpo)
    assert len(avisos) == 1, cuerpo.get("warnings")
    assert avisos[0].startswith("1 par(es)")
    assert "venta y compra: fila 1" in avisos[0]
    assert counts_del_import[-1]["posibles_duplicados_sin_letra"] == 1
