"""I2-1 / I2-2 — lo que el recorrido no escribe no cuenta: ni flete, ni consumo.

* **I2-1:** el flete de un comprobante se cobraba con la hoja entera, sin mirar
  si sus compras entraban. Subir de nuevo el mismo libro (o el acumulado del mes)
  como archivo NUEVO dejaba todas las compras «ya aplicadas» por E6b, pero el
  ancla del cargo lleva el archivo, así que la huella era otra y se creaba otro
  gasto de logística por comprobante: el flete duplicado en Resultado y en caja.
  Lo mismo con un comprobante que iba entero a «Otros» por conflicto.
* **I2-2:** el gate de replay armaba candidatas y créditos con TODAS las filas,
  antes de saber cuáles se iban a saltear. En el acumulado Enero+Febrero, las
  ventas de Enero ya aplicadas consumían el saldo (que ya las tenía descontadas)
  y la venta real de Febrero se iba a «Otros» como «sin stock». En la tabla
  suelta, además, la compra ya aplicada se sumaba otra vez como crédito y dejaba
  pasar una venta sin respaldo.

Se mide lo PERSISTIDO (gastos, ventas, «Otros», stock), por los DOS caminos.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.ingestion_import_service import insert_confirmed_data
from app.application.services.inventory_replay_service import run_inventory_replay
from app.domain.inventory_effect import HISTORICAL_REPLAY
from app.domain.purchase_cost_decision import PurchaseCostDecision
from app.persistence.models.file import UploadedFile
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord
from app.tests._respaldo import producto_con_compra

_CTX = "table"


async def _archivo(db: AsyncSession, tenant_id: uuid.UUID) -> uuid.UUID:
    """Un archivo NUEVO: el ancla de fila y la del flete llevan su id."""
    upload_id = uuid.uuid4()
    db.add(
        UploadedFile(
            id=upload_id,
            tenant_id=tenant_id,
            original_filename=f"{upload_id.hex[:8]}.xlsx",
            s3_key=f"tests/{upload_id}.xlsx",
            content_type="text/csv",
            size_bytes=1,
            purpose="ingestion",
            status="uploaded",
            processing_status="DONE",
        )
    )
    await db.flush()
    return upload_id


async def _otros(db: AsyncSession, tenant_id: uuid.UUID) -> list[UnclassifiedRecord]:
    return list(
        (
            await db.execute(
                select(UnclassifiedRecord).where(UnclassifiedRecord.tenant_id == tenant_id)
            )
        )
        .scalars()
        .all()
    )


# ── I2-1: el flete de compras que no entran ──────────────────────────────────

_COLS_COMPRAS = {
    "fecha": "expense_date",
    "idop": "external_operation_id",
    "sistema": "external_source",
    "nro": "invoice_number",
    "proveedor": "supplier_name",
    "articulo": "product_name",
    "cantidad": "quantity",
    "total": "amount",
    "envio": "shipping_cost",
}


def _compra(
    idop: str, nro: str, proveedor: str, articulo: str, total: str, envio: str
) -> dict[str, Any]:
    return {
        "fecha": "2024-03-05", "idop": idop, "sistema": "Tango", "nro": nro,
        "proveedor": proveedor, "articulo": articulo, "cantidad": "2",
        "total": total, "envio": envio,
    }


#: Dos comprobantes de dos líneas, con el flete repetido en cada línea y un ID de
#: origen por comprobante (identidad fuerte, granularidad de documento).
_LIBRO = [
    _compra("C-123", "0001-00000123", "Sur", "Vela", "6000", "2000"),
    _compra("C-123", "0001-00000123", "Sur", "Difusor", "3600", "2000"),
    _compra("C-124", "0001-00000124", "Norte", "Sahumerio", "5000", "1500"),
    _compra("C-124", "0001-00000124", "Norte", "Portavela", "900", "1500"),
]


async def _importar_compras(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    filas: list[dict[str, Any]],
    *,
    multi: bool,
    decision: str = "una_por_hoja",
) -> dict[str, Any]:
    upload_id = await _archivo(db, tenant_id)
    counts = await insert_confirmed_data(
        db,
        tenant_id,
        {
            "file_type": "spreadsheet",
            "inferred_type": "gastos",
            "multi_sheet": multi,
            "row_count": len(filas),
            "mapping_contexts": [
                {
                    "context_id": _CTX,
                    "entity_type": "expense",
                    "source_kind": "sheet",
                    "headers": list(_COLS_COMPRAS),
                    "row_count": len(filas),
                }
            ],
            "gastos_detectados": [{**f, "__context__": _CTX} for f in filas],
        },
        {},
        context_mappings={_CTX: dict(_COLS_COMPRAS)},
        context_entity={_CTX: "expense"},
        context_confirmed={_CTX: True},
        source="ingestion",
        uploaded_file_id=upload_id,
        shipping_decisions={_CTX: decision},
        purchase_cost_decisions={_CTX: PurchaseCostDecision(context_id=_CTX)},
    )
    await db.commit()
    return counts


async def _logistica(db: AsyncSession, tenant_id: uuid.UUID) -> list[str]:
    return sorted(
        str(g.amount)
        for g in (
            await db.execute(
                select(ExpenseEntry).where(
                    ExpenseEntry.tenant_id == tenant_id,
                    ExpenseEntry.category == "LOGISTICS",
                    ExpenseEntry.voided_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.parametrize("multi", [False, True], ids=["tabla_suelta", "por_hoja"])
async def test_el_mismo_libro_en_otro_archivo_no_vuelve_a_cobrar_el_flete(
    db_session: AsyncSession, sample_tenant: Tenant, multi: bool
) -> None:
    """Las compras quedan «ya aplicadas»; el flete no puede entrar solo."""
    tid = sample_tenant.tenant_id
    primero = await _importar_compras(db_session, tid, _LIBRO, multi=multi)
    assert primero.get("envios") == 2
    assert await _logistica(db_session, tid) == ["1500.00", "2000.00"]

    segundo = await _importar_compras(db_session, tid, _LIBRO, multi=multi)

    assert segundo.get("gastos", 0) == 0, segundo
    assert await _logistica(db_session, tid) == ["1500.00", "2000.00"], (
        f"el flete se cobró de nuevo con las compras ya aplicadas: {segundo}"
    )
    assert segundo.get("envios", 0) == 0
    assert segundo["envios_sin_compra_aplicada"] == 2


@pytest.mark.parametrize("multi", [False, True], ids=["tabla_suelta", "por_hoja"])
async def test_el_acumulado_cobra_solo_el_flete_del_comprobante_nuevo(
    db_session: AsyncSession, sample_tenant: Tenant, multi: bool
) -> None:
    """Control: no es «nunca cobrar en un reimport», es cobrar lo que entra."""
    tid = sample_tenant.tenant_id
    await _importar_compras(db_session, tid, _LIBRO, multi=multi)

    acumulado = [
        *_LIBRO,
        _compra("C-125", "0001-00000125", "Este", "Vela", "1000", "700"),
    ]
    r = await _importar_compras(db_session, tid, acumulado, multi=multi)

    assert await _logistica(db_session, tid) == ["1500.00", "2000.00", "700.00"], r
    assert r.get("envios") == 1
    assert r["envios_sin_compra_aplicada"] == 2


@pytest.mark.parametrize("multi", [False, True], ids=["tabla_suelta", "por_hoja"])
async def test_el_comprobante_que_va_entero_a_otros_no_cobra_flete(
    db_session: AsyncSession, sample_tenant: Tenant, multi: bool
) -> None:
    """Variante dentro del archivo: el C-123 vuelve con otro total (conflicto de
    identidad → «Otros»). Su flete no se cobra: la compra no entró, y si se
    resuelve desde «Otros» el envío viaja con ella."""
    tid = sample_tenant.tenant_id
    await _importar_compras(db_session, tid, _LIBRO[:2], multi=multi)
    assert await _logistica(db_session, tid) == ["2000.00"]

    corregido = [
        _compra("C-123", "0001-00000123", "Sur", "Vela", "6100", "2000"),
        _compra("C-123", "0001-00000123", "Sur", "Difusor", "3600", "2000"),
    ]
    r = await _importar_compras(db_session, tid, corregido, multi=multi)

    assert len(await _otros(db_session, tid)) == 2, r
    assert await _logistica(db_session, tid) == ["2000.00"], (
        f"el flete de un comprobante que fue a «Otros» se cobró igual: {r}"
    )
    assert r["envios_sin_compra_aplicada"] == 1


# Revisión del PR #220: el flete SIN comprobante en su propia fila. La fila que
# trae sólo «Envío: 2000» nunca es una compra; atar el cobro a SUS filas dejaba
# sin cobrar un flete legítimo aunque las compras de la hoja entraran.


def _sin_nro(idop: str, articulo: str, total: str, envio: str = "") -> dict[str, Any]:
    return {
        **_compra(idop, "", "Sur", articulo, total, envio),
        "cantidad": "2" if articulo else "",
    }


#: Dos compras de «Sur» sin número y el flete al pie, en una fila sin artículo ni
#: monto (ni ID de origen: no es una operación).
_HOJA_CON_FLETE_AL_PIE = [
    _sin_nro("C-1", "Vela", "6000"),
    _sin_nro("C-2", "Difusor", "3600"),
    {**_sin_nro("", "", "", "2000"), "sistema": ""},
]


@pytest.mark.parametrize("multi", [False, True], ids=["tabla_suelta", "por_hoja"])
@pytest.mark.parametrize("decision", ["una_por_hoja", "una_por_fila"])
async def test_el_flete_en_fila_propia_sin_comprobante_se_cobra(
    db_session: AsyncSession, sample_tenant: Tenant, multi: bool, decision: str
) -> None:
    """Las compras de la hoja SÍ entran en esta corrida: el flete se cobra."""
    tid = sample_tenant.tenant_id
    r = await _importar_compras(
        db_session, tid, _HOJA_CON_FLETE_AL_PIE, multi=multi, decision=decision
    )
    assert r.get("gastos") == 2, r
    assert await _logistica(db_session, tid) == ["2000.00"], (
        f"el flete de la hoja no se cobró con sus compras adentro: {r}"
    )
    assert not r.get("envios_sin_compra_aplicada"), r


@pytest.mark.parametrize("multi", [False, True], ids=["tabla_suelta", "por_hoja"])
@pytest.mark.parametrize("decision", ["una_por_hoja", "una_por_fila"])
async def test_el_flete_al_pie_no_se_cobra_si_todas_las_compras_ya_estaban(
    db_session: AsyncSession, sample_tenant: Tenant, multi: bool, decision: str
) -> None:
    """La misma hoja como archivo nuevo: las dos compras «ya aplicadas» por E6b.
    El flete al pie no tiene compra que lo acompañe y no se cobra otra vez."""
    tid = sample_tenant.tenant_id
    await _importar_compras(
        db_session, tid, _HOJA_CON_FLETE_AL_PIE, multi=multi, decision=decision
    )
    r = await _importar_compras(
        db_session, tid, _HOJA_CON_FLETE_AL_PIE, multi=multi, decision=decision
    )
    assert r.get("gastos", 0) == 0, r
    assert await _logistica(db_session, tid) == ["2000.00"], (
        f"el flete al pie se cobró de nuevo con las compras ya aplicadas: {r}"
    )
    assert r["envios_sin_compra_aplicada"] == 1


@pytest.mark.parametrize("multi", [False, True], ids=["tabla_suelta", "por_hoja"])
async def test_una_por_fila_el_flete_de_la_fila_ya_aplicada_no_se_recobra(
    db_session: AsyncSession, sample_tenant: Tenant, multi: bool
) -> None:
    """Con ``una_por_fila`` el flete de una fila de compra es de ESA fila: en el
    acumulado, que entre la compra nueva no habilita el flete de la vieja."""
    tid = sample_tenant.tenant_id
    vieja = _sin_nro("C-1", "Vela", "6000", "500")
    await _importar_compras(db_session, tid, [vieja], multi=multi, decision="una_por_fila")
    r = await _importar_compras(
        db_session,
        tid,
        [vieja, _sin_nro("C-2", "Difusor", "3600", "300")],
        multi=multi,
        decision="una_por_fila",
    )
    assert r.get("gastos") == 1, r
    assert await _logistica(db_session, tid) == ["300.00", "500.00"], r
    assert r["envios_sin_compra_aplicada"] == 1


# ── I2-2: el gate de replay no cuenta lo que no se escribe ───────────────────

_PRODUCTO = "Vela aromatica 200g"
_COLS_VENTAS = {
    "fecha": "transaction_date",
    "idop": "external_operation_id",
    "sistema": "external_source",
    "producto": "product_name",
    "cantidad": "quantity",
    "monto": "amount",
}


def _venta(idop: str, fecha: str, cantidad: int) -> dict[str, Any]:
    return {
        "fecha": fecha, "idop": idop, "sistema": "Tango", "producto": _PRODUCTO,
        "cantidad": str(cantidad), "monto": str(cantidad * 1000),
    }


_ENERO = _venta("V-1", "2024-01-15", 10)
_FEBRERO = _venta("V-2", "2024-02-10", 8)


async def _importar_ventas(
    db: AsyncSession,
    tenant_id: uuid.UUID,
    filas: list[dict[str, Any]],
    *,
    multi: bool,
    upload_id: uuid.UUID | None = None,
    cols: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Confirm de ventas + el replay que el confirm aplica después (F-F.4)."""
    upload_id = upload_id or await _archivo(db, tenant_id)
    cols = cols or _COLS_VENTAS
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "mixed" if multi else "ventas",
        "multi_sheet": multi,
        "has_venta": True,
        "row_count": len(filas),
        "mapping_contexts": [
            {
                "context_id": _CTX,
                "label": "Ventas",
                "entity_type": "sale",
                "source_kind": "sheet",
                "headers": list(cols),
                "row_count": len(filas),
            }
        ],
        "ventas_detectadas": [{**f, "__context__": _CTX} for f in filas],
    }
    if multi:
        counts = await insert_confirmed_data(
            db,
            tenant_id,
            summary,
            {"ventas": True},
            context_mappings={_CTX: dict(cols)},
            context_confirmed={_CTX: True},
            inventory_effect={_CTX: HISTORICAL_REPLAY},
            uploaded_file_id=upload_id,
        )
    else:
        counts = await insert_confirmed_data(
            db,
            tenant_id,
            summary,
            {"ventas": True},
            column_mappings=dict(cols),
            inventory_effect={_CTX: HISTORICAL_REPLAY},
            uploaded_file_id=upload_id,
        )
    await db.flush()
    await run_inventory_replay(db, tenant_id, upload_id, context_ids=[_CTX], apply=True)
    await db.commit()
    return counts


async def _sin_stock(db: AsyncSession, tenant_id: uuid.UUID) -> list[UnclassifiedRecord]:
    """Las que el gate mandó a «Otros». Una fila «ya aplicada» de la tabla suelta
    también cae a «Otros», como «sin monto» —defecto previo, ajeno a I2-2 (ver
    `test_identidad_entidad_y_letra_i6._otros`)—, y no es lo que se mide."""
    return [
        r
        for r in await _otros(db, tenant_id)
        if "sin stock que la respalde" in (r.context_label or "")
    ]


async def _ventas(db: AsyncSession, tenant_id: uuid.UUID) -> list[SaleEntry]:
    return list(
        (
            await db.execute(
                select(SaleEntry).where(
                    SaleEntry.tenant_id == tenant_id, SaleEntry.voided_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )


async def _stock(db: AsyncSession, producto: Product) -> int:
    db.expunge_all()
    fresco = await db.get(Product, producto.id)
    assert fresco is not None
    return int(fresco.stock_units)


@pytest.mark.parametrize("multi", [False, True], ids=["tabla_suelta", "por_hoja"])
async def test_el_acumulado_no_manda_a_otros_la_venta_nueva(
    db_session: AsyncSession, sample_tenant: Tenant, multi: bool
) -> None:
    """Stock 20, Enero vende 10 → 10. El acumulado Enero+Febrero: Enero ya está
    aplicada (y descontada); Febrero (8) entra contra las 10 que quedan."""
    tid = sample_tenant.tenant_id
    producto = await producto_con_compra(
        db_session, tid, nombre=_PRODUCTO, stock=20, costo=Decimal("600")
    )
    await _importar_ventas(db_session, tid, [_ENERO], multi=multi)
    assert await _stock(db_session, producto) == 10

    r = await _importar_ventas(db_session, tid, [_ENERO, _FEBRERO], multi=multi)

    assert not await _sin_stock(db_session, tid), (
        f"la venta de Febrero se fue a «Otros» por un saldo que consumió Enero: {r}"
    )
    assert r.get("ventas_sin_stock", 0) == 0
    assert sorted(v.quantity for v in await _ventas(db_session, tid)) == [8, 10]
    assert await _stock(db_session, producto) == 2


@pytest.mark.parametrize("multi", [False, True], ids=["tabla_suelta", "por_hoja"])
async def test_la_fila_tapada_por_su_huella_no_consume_saldo(
    db_session: AsyncSession, sample_tenant: Tenant, multi: bool
) -> None:
    """Sin identidad fuerte: la fila de Enero ya importada por ESTE archivo (la
    corrida anterior, o la venta editada que preserva una relectura) se saltea por
    su huella. Su descuento sigue vivo, así que tampoco puede volver a consumir."""
    tid = sample_tenant.tenant_id
    producto = await producto_con_compra(
        db_session, tid, nombre=_PRODUCTO, stock=20, costo=Decimal("600")
    )
    sin_identidad = {k: v for k, v in _COLS_VENTAS.items() if k not in ("idop", "sistema")}
    upload_id = await _archivo(db_session, tid)
    await _importar_ventas(
        db_session, tid, [_ENERO], multi=multi, upload_id=upload_id, cols=sin_identidad
    )
    assert await _stock(db_session, producto) == 10

    r = await _importar_ventas(
        db_session,
        tid,
        [_ENERO, _FEBRERO],
        multi=multi,
        upload_id=upload_id,
        cols=sin_identidad,
    )

    assert not await _sin_stock(db_session, tid), r
    assert sorted(v.quantity for v in await _ventas(db_session, tid)) == [8, 10]
    assert await _stock(db_session, producto) == 2


async def test_control_la_venta_sin_respaldo_real_sigue_yendo_a_otros(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El gate sigue cortando: con 10 en stock, un Febrero de 12 no entra."""
    tid = sample_tenant.tenant_id
    await producto_con_compra(db_session, tid, nombre=_PRODUCTO, stock=20, costo=Decimal("600"))
    await _importar_ventas(db_session, tid, [_ENERO], multi=True)

    r = await _importar_ventas(
        db_session, tid, [_ENERO, _venta("V-2", "2024-02-10", 12)], multi=True
    )

    assert r.get("ventas_sin_stock") == 1
    assert len(await _sin_stock(db_session, tid)) == 1


# Tabla suelta que da venta y compra (Libro Diario): «egreso» es el monto de la
# compra y «venta» el de la venta, columnas distintas.
_COLS_DIARIO = {
    "fecha": "transaction_date",
    "producto": "product_name",
    "cantidad": "quantity",
}
_HEADERS_DIARIO = ["fecha", "producto", "cantidad", "venta", "egreso"]


async def _importar_diario(
    db: AsyncSession, tenant_id: uuid.UUID, filas: list[dict[str, Any]], upload_id: uuid.UUID
) -> dict[str, Any]:
    counts = await insert_confirmed_data(
        db,
        tenant_id,
        {
            "file_type": "spreadsheet",
            "inferred_type": "general",
            "multi_sheet": False,
            "has_venta": True,
            "row_count": len(filas),
            "headers": _HEADERS_DIARIO,
            "mapping_contexts": [
                {
                    "context_id": _CTX,
                    "label": "Diario",
                    "entity_type": "sale",
                    "source_kind": "table",
                    "headers": _HEADERS_DIARIO,
                    "row_count": len(filas),
                }
            ],
            "ventas_detectadas": [{**f, "__context__": _CTX} for f in filas],
        },
        {"ventas": True, "gastos": True},
        column_mappings=dict(_COLS_DIARIO),
        inventory_effect={_CTX: HISTORICAL_REPLAY},
        uploaded_file_id=upload_id,
    )
    await db.flush()
    await run_inventory_replay(db, tenant_id, upload_id, context_ids=[_CTX], apply=True)
    await db.commit()
    return counts


async def test_tabla_suelta_la_compra_ya_importada_no_es_credito_otra_vez(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Stock 2 + compra de 5 (primera corrida del archivo) = 7. La segunda corrida
    del MISMO archivo trae además una venta de 9: la compra se saltea por su
    huella —sus 5 ya están en el 7—, así que contarla otra vez como crédito daba
    12 y la venta entraba sin respaldo.

    Va por la huella y no por la identidad fuerte a propósito: con identidad, la
    fila de compra también entra como candidata de VENTA en el camino plano (el
    gate no mira el monto de venta, vecino no arreglado acá) y consume las mismas
    5 unidades que el crédito suma, así que el crédito de más no se ve."""
    tid = sample_tenant.tenant_id
    producto = await producto_con_compra(
        db_session, tid, nombre=_PRODUCTO, stock=2, costo=Decimal("600")
    )
    upload_id = await _archivo(db_session, tid)
    compra = {
        "fecha": "2024-01-05", "producto": _PRODUCTO, "cantidad": "5",
        "venta": "", "egreso": "3000",
    }
    r1 = await _importar_diario(db_session, tid, [compra], upload_id)
    assert await _stock(db_session, producto) == 7, r1

    venta = {
        "fecha": "2024-02-01", "producto": _PRODUCTO, "cantidad": "9",
        "venta": "9000", "egreso": "",
    }
    r2 = await _importar_diario(db_session, tid, [compra, venta], upload_id)

    assert r2.get("ventas_sin_stock") == 1, (
        f"la compra ya importada respaldó otra vez una venta sin stock: {r2}"
    )
    assert not await _ventas(db_session, tid)
    assert await _stock(db_session, producto) == 7
