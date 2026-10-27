"""I2-3: en una tabla suelta con «ventas» y «productos» tildados, la cantidad
vendida de cada fila no fija el stock del producto, ni su precio unitario el
precio de venta del catálogo.

El bucle de productos recorre las MISMAS filas de venta, y la heurística de
stock (`_STOCK_COLS`) contiene "cantidad": cada fila pisaba el stock absoluto con
su cantidad —la última ganaba— y esa apertura inventada respaldaba las ventas en
B·5e. Stock, precio y costo de una tabla transaccional salen sólo de un mapeo
explícito (hoy sólo por API); lo que se descarta se avisa en el confirm.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.ingestion_import_service import insert_confirmed_data
from app.application.services.inventory_movement_origin import SOURCE_CATALOG_INITIAL_STOCK
from app.application.services.respaldo_service import MOTIVO_VENTA_SIN_COMPRA
from app.domain.business_time import today_ar
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.inventory import InventoryMovement
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


async def _archivo(db: AsyncSession, tid: uuid.UUID) -> UploadedFile:
    f = UploadedFile(
        id=uuid.uuid4(), tenant_id=tid, uploaded_by=None, original_filename="ventas.csv",
        s3_key="ventas.csv", content_type="text/csv", size_bytes=100, purpose="ingestion",
    )
    db.add(f)
    await db.commit()
    return f


def _tabla(filas: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "ventas",
        "has_venta": True,
        "has_producto": True,
        "ventas_detectadas": filas,
    }


def _filas(nombre: str) -> list[dict[str, Any]]:
    return [
        {"Fecha": hace(5).isoformat(), "Producto": nombre, "Cantidad": "3", "Total": "3000"},
        {"Fecha": hace(4).isoformat(), "Producto": nombre, "Cantidad": "7", "Total": "7000"},
    ]


async def _producto(db: AsyncSession, tid: uuid.UUID, nombre: str) -> Product:
    await db.commit()
    return (
        await db.execute(select(Product).where(Product.tenant_id == tid, Product.name == nombre))
    ).scalar_one()


async def _aperturas_del_archivo(
    db: AsyncSession, tid: uuid.UUID, file_id: uuid.UUID
) -> list[InventoryMovement]:
    return list(
        (
            await db.execute(
                select(InventoryMovement).where(
                    InventoryMovement.tenant_id == tid,
                    InventoryMovement.source_upload_id == file_id,
                    InventoryMovement.source_type == SOURCE_CATALOG_INITIAL_STOCK,
                )
            )
        ).scalars()
    )


async def _ventas(db: AsyncSession, tid: uuid.UUID) -> list[SaleEntry]:
    return list(
        (await db.execute(select(SaleEntry).where(SaleEntry.tenant_id == tid))).scalars()
    )


async def _otros(db: AsyncSession, tid: uuid.UUID) -> list[UnclassifiedRecord]:
    return list(
        (
            await db.execute(
                select(UnclassifiedRecord).where(UnclassifiedRecord.tenant_id == tid)
            )
        ).scalars()
    )


async def test_sin_mapeo_la_cantidad_vendida_no_crea_stock_ni_respalda_la_venta(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    f = await _archivo(db_session, tid)
    await insert_confirmed_data(
        db_session, tid, _tabla(_filas("Vela")), {"ventas": True, "productos": True},
        uploaded_file_id=f.id,
    )
    await db_session.commit()

    vela = await _producto(db_session, tid, "Vela")
    assert vela.stock_units == 0
    assert await _aperturas_del_archivo(db_session, tid, f.id) == []
    # Sin apertura inventada, las ventas no tienen compra previa: a «Otros».
    assert await _ventas(db_session, tid) == []
    motivos = [(o.context_label or "").split(":")[0] for o in await _otros(db_session, tid)]
    assert motivos.count(MOTIVO_VENTA_SIN_COMPRA) == 2


async def test_con_cantidad_mapeada_no_pisa_el_stock_de_un_producto_respaldado(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    await producto_con_compra(db_session, tid, nombre="Yerba", stock=100, dia=hace(60))
    f = await _archivo(db_session, tid)
    await insert_confirmed_data(
        db_session, tid, _tabla(_filas("Yerba")), {"ventas": True, "productos": True},
        uploaded_file_id=f.id,
        column_mappings={
            "Fecha": "transaction_date", "Producto": "product_name",
            "Cantidad": "quantity", "Total": "amount",
        },
    )
    await db_session.commit()

    yerba = await _producto(db_session, tid, "Yerba")
    # Antes: la última fila dejaba el stock en 7 con un ajuste de catálogo.
    assert await _aperturas_del_archivo(db_session, tid, f.id) == []
    # El descuento de las ventas lo aplica el reproceso, no esta pasada.
    assert yerba.stock_units == 100
    assert sorted(v.quantity for v in await _ventas(db_session, tid)) == [3, 7]


async def test_una_columna_de_stock_mapeada_a_mano_si_se_respeta(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El catálogo con su historial en una sola tabla sigue funcionando cuando el
    usuario dice cuál es la columna del stock."""
    tid = sample_tenant.tenant_id
    f = await _archivo(db_session, tid)
    filas = [
        {**fila, "Stock": "20", "Costo": "400"} for fila in _filas("Mate")
    ]
    await insert_confirmed_data(
        db_session, tid, _tabla(filas), {"ventas": True, "productos": True},
        uploaded_file_id=f.id,
        column_mappings={
            "Fecha": "transaction_date", "Producto": "product_name",
            "Cantidad": "quantity", "Total": "amount",
            "Stock": "stock_units", "Costo": "unit_cost_ars",
        },
        stock_treatment="opening_balance",
    )
    await db_session.commit()

    mate = await _producto(db_session, tid, "Mate")
    assert mate.stock_units == 20
    assert len(await _aperturas_del_archivo(db_session, tid, f.id)) == 1
    assert sorted(v.quantity for v in await _ventas(db_session, tid)) == [3, 7]


async def test_una_columna_de_stock_no_mapeada_se_descarta_y_se_avisa(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El mapeador de una hoja de ventas no ofrece `stock_units`: el «Stock» de la
    tabla no se carga, pero el confirm lo dice (antes se perdía en silencio)."""
    tid = sample_tenant.tenant_id
    f = await _archivo(db_session, tid)
    filas = [
        {"Fecha": hace(5).isoformat(), "Producto": "Mate", "Total": "3000", "Stock": "20"},
        {"Fecha": hace(4).isoformat(), "Producto": "Mate", "Total": "7000", "Stock": "18"},
    ]
    counts = await insert_confirmed_data(
        db_session, tid, _tabla(filas), {"ventas": True, "productos": True},
        uploaded_file_id=f.id,
        column_mappings={
            "Fecha": "transaction_date", "Producto": "product_name", "Total": "amount",
        },
    )
    await db_session.commit()

    assert (await _producto(db_session, tid, "Mate")).stock_units == 0
    assert counts["catalogo_descartado_tabla_transaccional"] == 1
    assert counts["columnas_catalogo_descartadas"] == ["Stock"]


async def test_la_cantidad_usada_por_la_venta_no_se_avisa_como_descartada(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    f = await _archivo(db_session, tid)
    counts = await insert_confirmed_data(
        db_session, tid, _tabla(_filas("Vela")), {"ventas": True, "productos": True},
        uploaded_file_id=f.id,
    )
    assert "catalogo_descartado_tabla_transaccional" not in counts


async def test_el_precio_unitario_de_la_venta_no_pisa_el_precio_del_catalogo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    await producto_con_compra(db_session, tid, nombre="Yerba", stock=100, dia=hace(60))
    f = await _archivo(db_session, tid)
    filas = [
        {"Fecha": hace(5).isoformat(), "Producto": "Yerba", "Cantidad": "2",
         "Precio unitario": "500", "Total": "1000"},
        {"Fecha": hace(4).isoformat(), "Producto": "Yerba", "Cantidad": "2",
         "Precio unitario": "450", "Total": "900"},
    ]
    await insert_confirmed_data(
        db_session, tid, _tabla(filas), {"ventas": True, "productos": True},
        uploaded_file_id=f.id,
        column_mappings={
            "Fecha": "transaction_date", "Producto": "product_name",
            "Cantidad": "quantity", "Precio unitario": "unit_price", "Total": "amount",
        },
    )
    await db_session.commit()

    yerba = await _producto(db_session, tid, "Yerba")
    # Antes: 450, el de la última venta, y el POS vendía a eso.
    assert yerba.sale_price_ars == Decimal("1000")
    assert sorted(str(v.amount) for v in await _ventas(db_session, tid)) == ["1000.00", "900.00"]


async def test_un_precio_de_venta_no_mapeado_se_descarta_y_se_avisa(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Sin mapeo, la heurística de precio de venta tomaba una columna «Precio de
    venta» de la tabla: no pisa el catálogo y se avisa."""
    tid = sample_tenant.tenant_id
    await producto_con_compra(db_session, tid, nombre="Yerba", stock=100, dia=hace(60))
    f = await _archivo(db_session, tid)
    filas = [
        {"Fecha": hace(5).isoformat(), "Producto": "Yerba", "Cantidad": "1",
         "Precio de venta": "450", "Total": "450"},
    ]
    counts = await insert_confirmed_data(
        db_session, tid, _tabla(filas), {"ventas": True, "productos": True},
        uploaded_file_id=f.id,
        column_mappings={
            "Fecha": "transaction_date", "Producto": "product_name",
            "Cantidad": "quantity", "Total": "amount",
        },
    )
    await db_session.commit()

    assert (await _producto(db_session, tid, "Yerba")).sale_price_ars == Decimal("1000")
    assert counts["columnas_catalogo_descartadas"] == ["Precio de venta"]


async def test_el_confirm_avisa_el_stock_descartado(
    client: AsyncClient, auth_headers: dict[str, str], db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """El caso del revisor por HTTP: el aviso llega en `warnings`, el mismo canal
    que la pantalla muestra al terminar el confirm."""
    tid = sample_tenant.tenant_id
    filas = [
        {"Fecha": hace(5).isoformat(), "Producto": "Mate", "Total": "3000", "Stock": "18"},
    ]
    record = UploadedFile(
        tenant_id=tid, uploaded_by=None, original_filename="ventas.csv",
        s3_key=f"uploads/test/{uuid.uuid4()}/ventas.csv", content_type="text/csv",
        size_bytes=100, purpose="ingestion", status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json={
            **_tabla(filas), "confidence": "HIGH", "row_count": 1,
            "headers": list(filas[0]), "preview_rows": filas,
        },
    )
    db_session.add(record)
    await db_session.commit()

    r = await client.post(
        f"/api/v1/ingestion/files/{record.id}/confirm",
        json={
            "column_mappings": [
                {"source_column": c, "target_field": t}
                for c, t in (
                    ("Fecha", "transaction_date"), ("Producto", "product_name"),
                    ("Total", "amount"),
                )
            ],
            "confirmed_fields": {"ventas": True, "productos": True},
        },
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    avisos = [w for w in r.json()["warnings"] if "«Stock»" in w]
    assert len(avisos) == 1
    assert "subí el catálogo en una hoja o archivo de productos" in avisos[0]
    assert (await _producto(db_session, tid, "Mate")).stock_units == 0
