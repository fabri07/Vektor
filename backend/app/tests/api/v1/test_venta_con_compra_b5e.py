"""B·5e: toda venta con producto y con compra previa (o stock inicial con costo).

Dueño (2026-10-20/21): «toda venta conlleva una compra previa; de lo contrario a
Otros» y «no se puede vender algo que no se compró».

- A mano (``POST /sales``, ``/sales/bulk``, ``/sales/manual-batch``), al editar
  producto o fecha, al clasificar desde «Otros» y por el chat: sin producto →
  422 ``SALE_NEEDS_PRODUCT``; sin compra a su fecha → 422 ``SALE_WITHOUT_PURCHASE``.
- Import: la venta sin compra (o sin producto) no queda como venta, va a «Otros»
  con su motivo. El stock inicial del MISMO archivo cuenta aunque su fecha esté
  por confirmar.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.ingestion_import_service import insert_confirmed_data
from app.application.services.respaldo_service import (
    MOTIVO_VENTA_SIN_COMPRA,
    MOTIVO_VENTA_SIN_PRODUCTO,
    VentaSinCompraError,
)
from app.domain.business_time import today_ar
from app.persistence.models.file import UploadedFile
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord
from app.tests._respaldo import producto_con_compra


@pytest.fixture(autouse=True)
def _sin_celery(mock_score_trigger: Any) -> None:
    pass


def hace(dias: int) -> date:
    return today_ar() - timedelta(days=dias)


def _iso(d: Any, hora: int = 12) -> str:
    return datetime(d.year, d.month, d.day, hora).isoformat()


async def _producto_sin_compra(db: AsyncSession, tid: uuid.UUID, nombre: str = "Vela") -> Product:
    p = Product(tenant_id=tid, name=nombre, sale_price_ars=Decimal("1000"), stock_units=50)
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


async def _ventas(db: AsyncSession, tid: uuid.UUID) -> list[SaleEntry]:
    await db.commit()
    return list(
        (await db.execute(select(SaleEntry).where(SaleEntry.tenant_id == tid))).scalars()
    )


def _venta(product_id: Any, dia: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "amount": "1000.00",
        "quantity": 1,
        "transaction_date": _iso(dia),
        "payment_method": "cash",
    }
    if product_id is not None:
        body["product_id"] = str(product_id)
    return body


# ── A mano ────────────────────────────────────────────────────────────────────


async def test_sin_producto_da_422(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    r = await client.post("/api/v1/sales", json=_venta(None, hace(1)), headers=auth_headers)
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "SALE_NEEDS_PRODUCT"
    assert await _ventas(db_session, sample_tenant.tenant_id) == []


async def test_sin_compra_da_422_y_con_compra_entra(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    p = await _producto_sin_compra(db_session, tid)
    r = await client.post("/api/v1/sales", json=_venta(p.id, hace(1)), headers=auth_headers)
    assert r.status_code == 422, r.text
    d = r.json()["detail"]
    assert (d["code"], d["product_id"], d["product_name"]) == (
        "SALE_WITHOUT_PURCHASE", str(p.id), "Vela"
    )
    assert "no se puede vender algo que no se compró" in d["message"]

    con = await producto_con_compra(db_session, tid, nombre="Yerba", dia=hace(30))
    r = await client.post("/api/v1/sales", json=_venta(con.id, hace(1)), headers=auth_headers)
    assert r.status_code == 201, r.text


async def test_una_compra_posterior_a_la_venta_no_la_respalda(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    p = await producto_con_compra(db_session, sample_tenant.tenant_id, dia=hace(5))
    r = await client.post("/api/v1/sales", json=_venta(p.id, hace(10)), headers=auth_headers)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "SALE_WITHOUT_PURCHASE"
    # El mismo día de la compra, sí.
    r = await client.post("/api/v1/sales", json=_venta(p.id, hace(5)), headers=auth_headers)
    assert r.status_code == 201, r.text


async def test_la_carga_en_lote_rechaza_toda_la_venta(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    con = await producto_con_compra(db_session, tid, nombre="Yerba", dia=hace(30))
    sin = await _producto_sin_compra(db_session, tid)
    r = await client.post(
        "/api/v1/sales/manual-batch",
        json={
            "transaction_date": _iso(hace(1)),
            "payment_method": "cash",
            "items": [
                {"product_id": str(con.id), "quantity": 1, "unit_price": "1000"},
                {"product_id": str(sin.id), "quantity": 1, "unit_price": "1000"},
            ],
        },
        headers=auth_headers,
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["product_id"] == str(sin.id)
    assert await _ventas(db_session, tid) == []


async def test_el_total_del_periodo_sin_productos_ya_no_se_acepta(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    r = await client.post(
        "/api/v1/sales/bulk",
        json={
            "period_type": "daily", "period_date": hace(1).isoformat(), "total_amount_ars": "5000",
        },
        headers=auth_headers,
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "SALE_NEEDS_PRODUCT"


async def test_mover_la_fecha_antes_de_la_compra_da_422(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    p = await producto_con_compra(db_session, sample_tenant.tenant_id, dia=hace(10))
    r = await client.post("/api/v1/sales", json=_venta(p.id, hace(1)), headers=auth_headers)
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    r = await client.patch(
        f"/api/v1/sales/{sid}", json={"transaction_date": _iso(hace(20))}, headers=auth_headers
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "SALE_WITHOUT_PURCHASE"
    # Editar otra cosa no la vuelve a juzgar.
    r = await client.patch(
        f"/api/v1/sales/{sid}", json={"notes": "corregida"}, headers=auth_headers
    )
    assert r.status_code == 200, r.text


async def test_el_chat_dice_por_que(
    db_session: AsyncSession, sample_tenant: Tenant, sample_user: Any
) -> None:
    from app.application.services.cash_service import save_sale

    p = await _producto_sin_compra(db_session, sample_tenant.tenant_id)
    with pytest.raises(VentaSinCompraError) as exc:
        await save_sale(
            {"amount": "1000", "product_id": str(p.id), "payment_method": "cash",
             "transaction_date": hace(1).isoformat()},
            sample_tenant.tenant_id, sample_user.user_id, db_session,
        )
    assert "Vela" in exc.value.user_message


async def test_clasificar_desde_otros_como_venta_exige_la_compra(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    p = await _producto_sin_compra(db_session, tid)
    rec = UnclassifiedRecord(
        tenant_id=tid, source="ingestion", context_label=MOTIVO_VENTA_SIN_COMPRA,
        row_data={"monto": "1000"}, suggested_entity="sale", status="PENDING",
    )
    db_session.add(rec)
    await db_session.commit()
    r = await client.post(
        f"/api/v1/others/{rec.id}/reclassify",
        json={"entity_type": "sale", "fields": _venta(p.id, hace(1))},
        headers=auth_headers,
    )
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["code"] == "SALE_WITHOUT_PURCHASE"


# ── Import ────────────────────────────────────────────────────────────────────


async def _archivo(db: AsyncSession, tid: uuid.UUID) -> UploadedFile:
    f = UploadedFile(
        id=uuid.uuid4(), tenant_id=tid, uploaded_by=None, original_filename="ventas.xlsx",
        s3_key="ventas.xlsx", content_type="application/vnd.ms-excel", size_bytes=100,
        purpose="ingestion",
    )
    db.add(f)
    await db.commit()
    return f


def _ventas_planas(filas: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "has_venta": True,
        "ventas_detectadas": filas,
    }


async def test_el_import_manda_a_otros_la_venta_sin_compra_y_sin_producto(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    await _producto_sin_compra(db_session, tid, "Vela")
    await producto_con_compra(db_session, tid, nombre="Yerba", dia=hace(60))
    f = await _archivo(db_session, tid)
    counts = await insert_confirmed_data(
        db_session,
        tid,
        _ventas_planas(
            [
                {"fecha": hace(3).isoformat(), "venta": "1000", "producto": "Yerba"},
                {"fecha": hace(3).isoformat(), "venta": "2000", "producto": "Vela"},
                {"fecha": hace(3).isoformat(), "venta": "3000", "producto": "Algo que no existe"},
            ]
        ),
        {"ventas": True},
        uploaded_file_id=f.id,
    )
    await db_session.commit()
    ventas = await _ventas(db_session, tid)
    assert [str(v.amount) for v in ventas] == ["1000.00"]
    assert counts["ventas"] == 1
    otros = list(
        (await db_session.execute(
            select(UnclassifiedRecord).where(UnclassifiedRecord.tenant_id == tid)
        )).scalars()
    )
    motivos = sorted((o.context_label or "").split(":")[0] for o in otros)
    assert MOTIVO_VENTA_SIN_COMPRA in motivos
    assert len(otros) == 2
    vela = next(o for o in otros if (o.context_label or "").startswith(MOTIVO_VENTA_SIN_COMPRA))
    assert "Vela" in (vela.context_label or "")
    assert vela.row_data["monto"] == "2000.00"
    assert vela.uploaded_file_id == f.id
    sin_producto = next(
        o for o in otros if (o.context_label or "").startswith(MOTIVO_VENTA_SIN_PRODUCTO)
    )
    # Lo que decía el ARCHIVO: sin esto la fila no dice qué se vendió y no se
    # puede resolver desde «Otros» (hotfix de B·5e).
    assert sin_producto.row_data["producto"] == "Algo que no existe"


async def test_el_stock_inicial_del_mismo_archivo_respalda_su_historial(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El primer import típico: un catálogo con el stock que «ya tenía» (su fecha
    la confirma el usuario después, M·2d-1b) y el historial de ventas. Las ventas
    quedan: el stock inicial del MISMO archivo las respalda."""
    tid = sample_tenant.tenant_id
    f = await _archivo(db_session, tid)
    cat = "sheet:Catalogo"
    ven = "sheet:Ventas"
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "has_producto": True,
        "has_venta": True,
        "mapping_contexts": [
            {"context_id": cat, "entity_type": "product", "source_kind": "sheet",
             "headers": ["Producto", "Precio", "Costo", "Stock"], "fields": None,
             "preview_rows": [], "row_count": 1},
            {"context_id": ven, "entity_type": "sale", "source_kind": "sheet",
             "headers": ["Fecha", "Producto", "Cantidad", "Monto"], "fields": None,
             "preview_rows": [], "row_count": 1},
        ],
        "stock_detectado": [
            {"Producto": "Vela", "Precio": "1000", "Costo": "400", "Stock": "20",
             "__context__": cat},
        ],
        "ventas_detectadas": [
            {"Fecha": hace(90).isoformat(), "Producto": "Vela", "Cantidad": "2",
             "Monto": "2000", "__context__": ven},
        ],
    }
    await insert_confirmed_data(
        db_session,
        tid,
        summary,
        {"productos": True, "ventas": True},
        uploaded_file_id=f.id,
        context_mappings={
            cat: {"Producto": "name", "Precio": "sale_price_ars", "Costo": "unit_cost_ars",
                  "Stock": "stock_units"},
            ven: {"Fecha": "transaction_date", "Producto": "product_name",
                  "Cantidad": "quantity", "Monto": "amount"},
        },
        stock_treatment={cat: "opening_balance"},
    )
    await db_session.commit()
    ventas = await _ventas(db_session, tid)
    assert [str(v.amount) for v in ventas] == ["2000.00"]
    assert ventas[0].product_id is not None


# ── Revisión de B·5e-1 ────────────────────────────────────────────────────────


async def test_un_archivo_con_todas_las_ventas_sin_compra_no_es_un_import_vacio(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Revisión: el primer archivo de un local que no cargó compras daba 422
    «no se importó ninguna fila» y el rollback se llevaba lo de «Otros»."""
    from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION

    tid = sample_tenant.tenant_id
    await _producto_sin_compra(db_session, tid, "Vela")
    filas = [
        {"fecha": hace(3).isoformat(), "venta": "1000", "producto": "Vela", "__context__": "table"},
        {"fecha": hace(2).isoformat(), "venta": "1500", "producto": "Vela", "__context__": "table"},
    ]
    f = UploadedFile(
        id=uuid.uuid4(), tenant_id=tid, uploaded_by=None, original_filename="v.xlsx",
        s3_key="v.xlsx", content_type="application/vnd.ms-excel", size_bytes=100,
        purpose="ventas", status="uploaded", processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json={
            "confidence": "HIGH", "file_type": "spreadsheet", "inferred_type": "ventas",
            "has_venta": True, "row_count": 2, "ventas_detectadas": filas,
            "headers": ["fecha", "venta", "producto"], "preview_rows": filas,
        },
    )
    db_session.add(f)
    await db_session.commit()
    r = await client.post(
        f"/api/v1/ingestion/files/{f.id}/confirm",
        json={"confirmed_fields": {"ventas": True}},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    assert any("2 venta(s) fueron a «Otros»" in w for w in r.json()["warnings"])
    await db_session.commit()
    assert await _ventas(db_session, tid) == []
    otros = (
        await db_session.execute(
            select(UnclassifiedRecord).where(UnclassifiedRecord.tenant_id == tid)
        )
    ).scalars().all()
    assert len(otros) == 2


@pytest.mark.parametrize("tratamiento", ["opening_balance", "purchase"])
async def test_el_catalogo_del_archivo_respalda_como_apertura_y_como_compra(
    db_session: AsyncSession, sample_tenant: Tenant, tratamiento: str
) -> None:
    """Revisión: tratado como COMPRA, el stock del catálogo toma la fecha de
    carga y no respaldaba el historial del mismo archivo."""
    tid = sample_tenant.tenant_id
    f = await _archivo(db_session, tid)
    cat, ven = "sheet:Catalogo", "sheet:Ventas"
    await insert_confirmed_data(
        db_session,
        tid,
        {
            "file_type": "spreadsheet", "inferred_type": "mixed", "multi_sheet": True,
            "has_producto": True, "has_venta": True,
            "mapping_contexts": [
                {"context_id": cat, "entity_type": "product", "source_kind": "sheet",
                 "headers": ["Producto", "Precio", "Costo", "Stock"], "fields": None,
                 "preview_rows": [], "row_count": 1},
                {"context_id": ven, "entity_type": "sale", "source_kind": "sheet",
                 "headers": ["Fecha", "Producto", "Cantidad", "Monto"], "fields": None,
                 "preview_rows": [], "row_count": 1},
            ],
            "stock_detectado": [
                {"Producto": "Vela", "Precio": "1000", "Costo": "400", "Stock": "20",
                 "__context__": cat},
            ],
            "ventas_detectadas": [
                {"Fecha": hace(90).isoformat(), "Producto": "Vela", "Cantidad": "2",
                 "Monto": "2000", "__context__": ven},
            ],
        },
        {"productos": True, "ventas": True},
        uploaded_file_id=f.id,
        context_mappings={
            cat: {"Producto": "name", "Precio": "sale_price_ars", "Costo": "unit_cost_ars",
                  "Stock": "stock_units"},
            ven: {"Fecha": "transaction_date", "Producto": "product_name",
                  "Cantidad": "quantity", "Monto": "amount"},
        },
        stock_treatment={cat: tratamiento},
    )
    await db_session.commit()
    assert [str(v.amount) for v in await _ventas(db_session, tid)] == ["2000.00"]


async def test_importar_sugeridos_no_saltea_la_regla(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Revisión: «Importar todos los sugeridos» volvía a dejar como venta lo que
    B·5e mandó a «Otros»."""
    from app.application.services.ingestion_import_service import bulk_import_unclassified

    tid = sample_tenant.tenant_id
    await _producto_sin_compra(db_session, tid, "Vela")
    for detalle in ("Vela", "Algo sin producto"):
        db_session.add(
            UnclassifiedRecord(
                tenant_id=tid, source="ingestion", context_label=MOTIVO_VENTA_SIN_COMPRA,
                row_data={"fecha": hace(2).isoformat(), "monto": "1000", "producto": detalle},
                suggested_entity="sale", status="PENDING",
            )
        )
    await db_session.commit()
    counts = await bulk_import_unclassified(db_session, tid)
    await db_session.commit()
    assert counts["imported_sales"] == 0
    assert counts["needs_manual"] == 2
    assert await _ventas(db_session, tid) == []
