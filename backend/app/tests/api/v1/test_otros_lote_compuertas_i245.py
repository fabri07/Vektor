"""I2-4, I2-5, V-4 — «Importar sugeridos» de «Otros» con las compuertas de la
resolución de a una.

Revisión profunda, etapa 2. El lote era la puerta de atrás de lo que el import
mandó a «Otros» a propósito:

- I2-4: releía la fecha ORIGINAL celda por celda. Una fecha futura (que el
  import anuló por ``MOTIVO_FECHA_FUTURA``) entraba como venta o gasto fechado
  en diciembre; y en una columna mm/dd, «04/03/2025» volvía como 4 de marzo.
- I2-5: un gasto vinculado a un producto quedaba COGS sin artículos ni stock
  (el «gasto COGS suelto» que B·5b-2 cerró en el import), y no llevaba
  ``source_row_ref``.
- V-4: la venta del lote no descontaba stock ni miraba si alcanzaba, también la
  que el import mandó a «Otros» por «Venta sin stock que la respalde».
"""

from __future__ import annotations

import io
import json
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import stock_service
from app.application.services.file_parsing import parse_uploaded_content
from app.application.services.ingestion_import_service import CONVENIOS_DE_FECHA_KEY
from app.domain.business_time import today_ar
from app.domain.contrato_de_costo import ContratoDeCosto
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.models.unclassified_record import (
    UnclassifiedRecord,
    unclassified_row_ref,
)
from app.tests._respaldo import producto_con_compra

_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_VELA = "Vela aromatica"


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker, cada confirm paga ~5s de reintentos de kombu (fail-safe)."""


def _libro(headers: list[str], filas: list[list[Any]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Ventas"
    ws.append(headers)
    for fila in filas:
        ws.append(fila)
    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


# `cliente` (sin mapear) está para que el clasificador lea la hoja como ventas.
_H_VENTAS = ["fecha", "cliente", "articulo", "cantidad", "total"]
_MAPEO_VENTAS = {
    "fecha": "transaction_date",
    "articulo": "product_name",
    "cantidad": "quantity",
    "total": "amount",
}


async def _confirmar_ventas(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db: AsyncSession,
    tenant: Tenant,
    filas: list[list[Any]],
) -> uuid.UUID:
    nombre = f"{uuid.uuid4().hex[:8]}.xlsx"
    contenido = _libro(_H_VENTAS, filas)
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
                {"source_column": c, "target_field": t} for c, t in _MAPEO_VENTAS.items()
            ],
            "confirmed_fields": {"ventas": True},
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    db.expunge_all()
    return record.id


async def _archivo(db: AsyncSession, tid: uuid.UUID) -> UploadedFile:
    archivo = UploadedFile(
        tenant_id=tid, uploaded_by=None, original_filename="a.xlsx", s3_key="a",
        content_type=_XLSX_MIME, size_bytes=1, purpose="ingestion", status="uploaded",
    )
    db.add(archivo)
    await db.flush()
    return archivo


def _fila_de_otros(
    tid: uuid.UUID,
    archivo: UploadedFile,
    row: dict[str, str],
    entity: str,
    motivo: str = "Venta sin stock que la respalde",
) -> UnclassifiedRecord:
    return UnclassifiedRecord(
        tenant_id=tid,
        uploaded_file_id=archivo.id,
        source="ingestion",
        context_label=motivo,
        suggested_entity=entity,
        status="PENDING",
        row_data=row,
    )


async def _importar_sugeridos(
    client: AsyncClient, auth_headers: dict[str, str], entity: str | None = None
) -> dict[str, int]:
    r = await client.post(
        "/api/v1/others/bulk-import",
        json={"entity_type": entity} if entity else {},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    return dict(r.json())


async def _todos(db: AsyncSession, modelo: Any, *filtros: Any) -> list[Any]:
    db.expunge_all()
    return list((await db.execute(select(modelo).where(*filtros))).scalars().all())


def _pendiente(tid: uuid.UUID) -> tuple[Any, Any]:
    return (UnclassifiedRecord.tenant_id == tid, UnclassifiedRecord.status == "PENDING")


# ── I2-4: la fecha ───────────────────────────────────────────────────────────


async def test_una_fecha_futura_no_entra_por_el_lote(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """El caso ASTERIA: el import anula la fecha futura y la fila va a «Otros»
    sugerida como venta, con el ORIGINAL guardado. El lote ya no la importa."""
    tid = sample_tenant.tenant_id
    await producto_con_compra(db_session, tid, nombre=_VELA)
    futura = (today_ar() + timedelta(days=40)).strftime("%d/%m/%Y")
    await _confirmar_ventas(
        client, auth_headers, db_session, sample_tenant,
        [["05/03/2025", "Ana", _VELA, 1, 1200], [futura, "Ana", _VELA, 1, 1200]],
    )
    (cargada,) = await _todos(db_session, SaleEntry, SaleEntry.tenant_id == tid)
    (fila,) = await _todos(db_session, UnclassifiedRecord, *_pendiente(tid))
    assert fila.suggested_entity == "sale"

    cuerpo = await _importar_sugeridos(client, auth_headers)
    assert (cuerpo["imported_sales"], cuerpo["needs_manual"]) == (0, 1), cuerpo
    assert [v.id for v in await _todos(db_session, SaleEntry, SaleEntry.tenant_id == tid)] == [
        cargada.id
    ]
    assert [f.id for f in await _todos(db_session, UnclassifiedRecord, *_pendiente(tid))] == [
        fila.id
    ]


@pytest.mark.parametrize("formato", ["%d/%m/%Y", "%Y-%m-%d 00:00:00"])
async def test_una_fila_con_la_fecha_futura_original_no_entra_por_el_lote(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    formato: str,
) -> None:
    """La fila guarda lo que el archivo DECÍA (`_capture_unclassified`): con la
    fecha futura original, el lote antes la leía y creaba el gasto en el futuro."""
    tid = sample_tenant.tenant_id
    archivo = await _archivo(db_session, tid)
    futura = (today_ar() + timedelta(days=40)).strftime(formato)
    db_session.add(
        _fila_de_otros(
            tid, archivo,
            {"fecha": futura, "detalle": "Pago luz", "monto": "15000", "categoria": "Luz"},
            "expense", "Fila sin fecha reconocible",
        )
    )
    await db_session.commit()

    cuerpo = await _importar_sugeridos(client, auth_headers)
    assert (cuerpo["imported_expenses"], cuerpo["needs_manual"]) == (0, 1), cuerpo
    assert await _todos(db_session, ExpenseEntry, ExpenseEntry.tenant_id == tid) == []
    assert len(await _todos(db_session, UnclassifiedRecord, *_pendiente(tid))) == 1


async def test_el_lote_lee_la_fecha_con_el_convenio_que_la_columna_decidio(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Columna mm/dd («04/13/2025» sólo se lee así). La fila con la cantidad
    ilegible va a «Otros» con el convenio de su columna guardado; resuelta la
    cantidad, el lote lee «04/03/2025» como 3 de abril, no como 4 de marzo."""
    tid = sample_tenant.tenant_id
    await producto_con_compra(db_session, tid, nombre=_VELA)
    await _confirmar_ventas(
        client, auth_headers, db_session, sample_tenant,
        [
            ["04/13/2025", "Ana", _VELA, 1, 1200],
            ["04/03/2025", "Ana", _VELA, "dos", 2400],
        ],
    )
    (venta,) = await _todos(db_session, SaleEntry, SaleEntry.tenant_id == tid)
    assert venta.transaction_date.date() == date(2025, 4, 13)
    (fila,) = await _todos(db_session, UnclassifiedRecord, *_pendiente(tid))
    assert fila.row_data["fecha"] == "04/03/2025"
    assert json.loads(fila.row_data[CONVENIOS_DE_FECHA_KEY]) == {"fecha": "mdy"}

    # La cantidad legible (lo que el dueño corrige); el resto, tal cual se capturó.
    fila.row_data = {**fila.row_data, "cantidad": "2"}
    db_session.add(fila)
    await db_session.commit()

    cuerpo = await _importar_sugeridos(client, auth_headers)
    assert cuerpo["imported_sales"] == 1, cuerpo
    fechas = sorted(
        v.transaction_date.date()
        for v in await _todos(db_session, SaleEntry, SaleEntry.tenant_id == tid)
    )
    assert fechas == [date(2025, 4, 3), date(2025, 4, 13)]


@pytest.mark.parametrize(
    ("convenios", "esperada"),
    [
        ({"fecha": "dmy"}, date(2025, 3, 4)),
        ({"fecha": "mdy"}, date(2025, 4, 3)),
        # Capturada antes de guardar el convenio, o columna contradictoria: una
        # fecha corta ambigua no se adivina.
        (None, None),
        ({"fecha": ""}, None),
    ],
)
async def test_fecha_ambigua_segun_el_convenio_guardado(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    convenios: dict[str, str] | None,
    esperada: date | None,
) -> None:
    tid = sample_tenant.tenant_id
    archivo = await _archivo(db_session, tid)
    row = {"fecha": "04/03/2025", "detalle": "Pago luz", "monto": "15000", "categoria": "Luz"}
    if convenios is not None:
        row[CONVENIOS_DE_FECHA_KEY] = json.dumps(convenios)
    db_session.add(_fila_de_otros(tid, archivo, row, "expense", "Fila sin cliente"))
    await db_session.commit()

    cuerpo = await _importar_sugeridos(client, auth_headers)
    gastos = await _todos(db_session, ExpenseEntry, ExpenseEntry.tenant_id == tid)
    if esperada is None:
        assert (cuerpo["imported_expenses"], cuerpo["needs_manual"]) == (0, 1), cuerpo
        assert gastos == []
    else:
        assert cuerpo["imported_expenses"] == 1, cuerpo
        assert [g.transaction_date.date() for g in gastos] == [esperada]


# ── V-4 / I2-5: la venta descuenta stock y tiene que tenerlo ─────────────────


async def test_la_venta_del_lote_descuenta_stock_y_la_que_no_alcanza_queda(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """5 unidades, dos ventas de 3: la primera entra y descuenta (con el archivo
    de la fila en el movimiento); la segunda ya no tiene stock y queda en «Otros»."""
    tid = sample_tenant.tenant_id
    vela = await producto_con_compra(db_session, tid, nombre=_VELA, stock=5)
    archivo = await _archivo(db_session, tid)
    filas = [
        _fila_de_otros(
            tid, archivo,
            {"fecha": f"2025-05-0{d}", "producto": _VELA, "cantidad": "3", "total": "3600"},
            "sale",
        )
        for d in (1, 2)
    ]
    db_session.add_all(filas)
    await db_session.commit()

    cuerpo = await _importar_sugeridos(client, auth_headers, "sale")
    assert (cuerpo["imported_sales"], cuerpo["needs_manual"]) == (1, 1), cuerpo

    (venta,) = await _todos(db_session, SaleEntry, SaleEntry.tenant_id == tid)
    (producto,) = await _todos(db_session, Product, Product.id == vela.id)
    assert producto.stock_units == 2
    (mov,) = await _todos(
        db_session, InventoryMovement,
        InventoryMovement.tenant_id == tid,
        InventoryMovement.movement_type == "sale",
        InventoryMovement.voided_at.is_(None),
    )
    assert (mov.qty, mov.source_upload_id) == (-3, archivo.id)
    assert mov.source_event_id == f"sale:{venta.id}"
    importada = next(f for f in filas if venta.source_row_ref == unclassified_row_ref(f.id))
    (pendiente,) = await _todos(db_session, UnclassifiedRecord, *_pendiente(tid))
    assert pendiente.id != importada.id


# ── I2-5: el gasto nunca queda COGS en lote ──────────────────────────────────


async def test_un_gasto_vinculado_a_un_producto_es_una_compra_y_queda_en_otros(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """«Lampara luz» es el nombre exacto de un producto: en lote sería un COGS sin
    artículos ni stock → queda para `resolve-purchase`. «Pago luz» no vincula por
    parecido: entra OPEX, sin producto y con la referencia de su fila."""
    tid = sample_tenant.tenant_id
    await producto_con_compra(db_session, tid, nombre="Lampara luz")
    archivo = await _archivo(db_session, tid)
    compra = _fila_de_otros(
        tid, archivo,
        {"fecha": "2025-05-01", "detalle": "Lampara luz", "monto": "5000", "categoria": "Luz"},
        "expense", "Fila sin cliente",
    )
    luz = _fila_de_otros(
        tid, archivo,
        {"fecha": "2025-05-01", "detalle": "Pago luz", "monto": "15000", "categoria": "Luz"},
        "expense", "Fila sin cliente",
    )
    db_session.add_all([compra, luz])
    await db_session.commit()

    cuerpo = await _importar_sugeridos(client, auth_headers, "expense")
    assert (cuerpo["imported_expenses"], cuerpo["needs_manual"]) == (1, 1), cuerpo
    (gasto,) = await _todos(db_session, ExpenseEntry, ExpenseEntry.tenant_id == tid)
    assert gasto.amount == Decimal("15000")
    assert (gasto.product_id, gasto.expense_type) == (None, "OPEX")
    assert gasto.source_upload_id == archivo.id
    assert gasto.source_row_ref == unclassified_row_ref(luz.id)
    assert [f.id for f in await _todos(db_session, UnclassifiedRecord, *_pendiente(tid))] == [
        compra.id
    ]
    assert isinstance(gasto.transaction_date, datetime)


# ── La fila pendiente se puede reintentar; los candados, en orden ─────────────


async def test_sin_stock_cargo_stock_y_reintento_el_lote(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Una venta de 3 con 1 unidad queda pendiente. El dueño carga 10 más y
    reintenta: la fila entra. Antes la huella se registraba antes de las
    compuertas y la fila salía «ya importada» (`skipped`) para siempre."""
    tid = sample_tenant.tenant_id
    vela = await producto_con_compra(db_session, tid, nombre=_VELA, stock=1)
    archivo = await _archivo(db_session, tid)
    fila = _fila_de_otros(
        tid, archivo,
        {"fecha": "2025-05-01", "producto": _VELA, "cantidad": "3", "total": "3600"},
        "sale",
    )
    db_session.add(fila)
    await db_session.commit()

    primera = await _importar_sugeridos(client, auth_headers, "sale")
    assert (primera["imported_sales"], primera["needs_manual"]) == (0, 1), primera

    await stock_service.registrar_apertura_manual(
        vela.id, tid, 10, ContratoDeCosto(unit_cost_final=Decimal("10")), db_session,
        fecha=datetime(2025, 4, 1, 9), confirmado_por=None,
    )
    await db_session.commit()

    segunda = await _importar_sugeridos(client, auth_headers, "sale")
    assert (segunda["imported_sales"], segunda["skipped"]) == (1, 0), segunda
    (registro,) = await _todos(db_session, UnclassifiedRecord, UnclassifiedRecord.id == fila.id)
    assert registro.status == "IMPORTED"
    (producto,) = await _todos(db_session, Product, Product.id == vela.id)
    assert producto.stock_units == 8

    # Y la importada sí queda con su huella: una tercera corrida no la duplica.
    registro.status = "PENDING"
    db_session.add(registro)
    await db_session.commit()
    tercera = await _importar_sugeridos(client, auth_headers, "sale")
    assert (tercera["imported_sales"], tercera["skipped"]) == (0, 1), tercera


async def test_el_lote_bloquea_los_productos_juntos_y_en_orden_de_id(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Como la venta del POS: el primer candado de producto del lote los toma a
    todos, ordenados por id. De a uno, en el orden de los registros, se cruzaba
    con una venta de varios productos."""
    tid = sample_tenant.tenant_id
    productos = [
        await producto_con_compra(db_session, tid, nombre=n) for n in (_VELA, "Difusor bambu")
    ]
    archivo = await _archivo(db_session, tid)
    # Los registros en el orden INVERSO al de los ids.
    for p in sorted(productos, key=lambda p: p.id, reverse=True):
        db_session.add(
            _fila_de_otros(
                tid, archivo,
                {"fecha": "2025-05-01", "producto": p.name, "cantidad": "1", "total": "1200"},
                "sale",
            )
        )
    await db_session.commit()

    llamadas: list[list[uuid.UUID]] = []
    original = stock_service.bloquear_productos

    async def _espia(db: AsyncSession, tenant_id: uuid.UUID, ids: Any, **kw: Any) -> Any:
        ids = list(ids)
        llamadas.append(ids)
        return await original(db, tenant_id, ids, **kw)

    monkeypatch.setattr(stock_service, "bloquear_productos", _espia)
    cuerpo = await _importar_sugeridos(client, auth_headers, "sale")
    assert cuerpo["imported_sales"] == 2, cuerpo
    assert sorted(llamadas[0]) == sorted(p.id for p in productos)
