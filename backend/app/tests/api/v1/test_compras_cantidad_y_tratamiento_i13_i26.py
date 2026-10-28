"""I1-3 e I2-6: dos maneras en que una compra del import quedaba mal sin avisar.

- **I1-3:** una compra de mercadería con cantidad «2,5» de un producto POR UNIDAD
  que ya existe pasaba el filtro de B·5b-2 (`_hay_cantidad` acepta decimales por
  los productos por peso), se vinculaba al producto y dejaba un gasto COGS con
  salida de caja pero sin stock ni movimiento: `_apply_purchase_to_stock` la
  descartaba en silencio. Ahora va a «Otros» con el motivo de la cantidad, antes
  de crear el gasto. Un producto por peso con «2,5» kg sigue siendo una compra.
- **I2-6:** en una tabla suelta (context_id ``"table"``) el tratamiento de stock
  por hoja (``{"table": "purchase"}``, el que mandan la relectura y la API) se
  ignoraba y el catálogo entraba como apertura: sin COGS ni salida de caja.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.file_parsing import parse_uploaded_content
from app.application.services.ingestion_import_service import insert_confirmed_data
from app.domain.business_time import today_ar
from app.domain.comprobante_de_compra import MOTIVO_COMPRA_SIN_ARTICULOS
from app.persistence.models.file import PROCESSING_STATUS_NEEDS_CONFIRMATION, UploadedFile
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry
from app.persistence.models.unclassified_record import UnclassifiedRecord

_FECHA = (today_ar() - timedelta(days=3)).isoformat()
_ROTULO_FRACCION = "La cantidad tiene decimales y las unidades son enteras"


@pytest.fixture(autouse=True)
def _sin_broker(mock_score_trigger: Any) -> None:
    """Sin broker, cada import paga reintentos de kombu."""


async def _producto(
    db: AsyncSession, tid: uuid.UUID, nombre: str, *, factor: int = 1, stock: int = 0
) -> Product:
    p = Product(
        tenant_id=tid,
        name=nombre,
        sale_price_ars=Decimal("100"),
        stock_units=stock,
        sale_unit="gram" if factor > 1 else "unit",
        base_units_per_sale_unit=factor,
    )
    db.add(p)
    await db.commit()
    return p


async def _gastos(db: AsyncSession, tid: uuid.UUID) -> list[ExpenseEntry]:
    return list(
        (await db.execute(select(ExpenseEntry).where(ExpenseEntry.tenant_id == tid))).scalars()
    )


async def _movimientos(db: AsyncSession, tid: uuid.UUID) -> list[InventoryMovement]:
    return list(
        (
            await db.execute(
                select(InventoryMovement).where(InventoryMovement.tenant_id == tid)
            )
        ).scalars()
    )


async def _otros(db: AsyncSession, tid: uuid.UUID) -> list[UnclassifiedRecord]:
    return list(
        (
            await db.execute(
                select(UnclassifiedRecord).where(UnclassifiedRecord.tenant_id == tid)
            )
        ).scalars()
    )


async def _stock(db: AsyncSession, p: Product) -> int:
    await db.refresh(p)
    return int(p.stock_units)


# ── I1-3, tabla suelta ────────────────────────────────────────────────────────


def _compras_planas(nombre: str, cantidad: Any) -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "has_gasto": True,
        "gastos_detectados": [
            {
                "fecha": _FECHA,
                "producto": nombre,
                "cantidad": cantidad,
                "gasto": "5000",
                "categoria": "Mercadería",
            }
        ],
    }


async def test_plano_cantidad_con_decimales_de_un_producto_por_unidad_va_a_otros(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    yerba = await _producto(db_session, tid, "Yerba Playadito", stock=4)
    counts = await insert_confirmed_data(
        db_session, tid, _compras_planas("Yerba Playadito", "2,5"), {"gastos": True}
    )
    await db_session.commit()

    # Antes: un COGS de $5.000 con producto, sin stock ni movimiento.
    assert await _gastos(db_session, tid) == []
    assert await _movimientos(db_session, tid) == []
    assert await _stock(db_session, yerba) == 4
    otros = await _otros(db_session, tid)
    assert len(otros) == 1
    assert (otros[0].context_label or "").startswith(_ROTULO_FRACCION)
    assert otros[0].suggested_entity == "expense"
    assert counts["filas_sin_cantidad"] == 1
    assert counts.get("compras_sin_articulos", 0) == 0


async def test_plano_un_cero_escrito_es_una_compra_sin_cantidad(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Sin regresión: un «0» escrito ya no pasaba `_hay_cantidad` y sigue yendo a
    «Otros» como compra sin cantidad, no con el rótulo de los decimales."""
    tid = sample_tenant.tenant_id
    await _producto(db_session, tid, "Yerba Playadito")
    counts = await insert_confirmed_data(
        db_session, tid, _compras_planas("Yerba Playadito", "0"), {"gastos": True}
    )
    await db_session.commit()

    assert await _gastos(db_session, tid) == []
    assert [o.context_label for o in await _otros(db_session, tid)] == [
        MOTIVO_COMPRA_SIN_ARTICULOS
    ]
    assert counts["compras_sin_articulos"] == 1


async def test_plano_kilos_con_decimales_de_un_producto_por_peso_siguen_siendo_compra(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """No es una regresión de los productos por peso: 2,5 kg son 2500 g."""
    tid = sample_tenant.tenant_id
    queso = await _producto(db_session, tid, "Queso cremoso", factor=1000)
    await insert_confirmed_data(
        db_session, tid, _compras_planas("Queso cremoso", "2,5"), {"gastos": True}
    )
    await db_session.commit()

    gastos = await _gastos(db_session, tid)
    assert [(g.expense_type, g.product_id) for g in gastos] == [("COGS", queso.id)]
    assert await _stock(db_session, queso) == 2500
    assert [(m.movement_type, m.qty) for m in await _movimientos(db_session, tid)] == [
        ("purchase", 2500)
    ]
    assert await _otros(db_session, tid) == []


async def test_plano_cantidad_entera_sigue_siendo_compra(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    yerba = await _producto(db_session, tid, "Yerba Playadito", stock=4)
    await insert_confirmed_data(
        db_session, tid, _compras_planas("Yerba Playadito", "3"), {"gastos": True}
    )
    await db_session.commit()

    assert [g.expense_type for g in await _gastos(db_session, tid)] == ["COGS"]
    assert await _stock(db_session, yerba) == 7
    assert await _otros(db_session, tid) == []


# ── I1-3, multihoja ───────────────────────────────────────────────────────────


def _compras_multihoja(nombre: str, cantidad: Any) -> dict[str, Any]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {"context_id": "sheet:Compras", "entity_type": "expense", "source_kind": "sheet",
             "headers": ["fecha", "producto", "cantidad", "monto", "categoria"],
             "fields": None, "preview_rows": [], "row_count": 1},
        ],
        "gastos_detectados": [
            {"fecha": _FECHA, "producto": nombre, "cantidad": cantidad,
             "monto": "5000", "categoria": "Mercadería", "__context__": "sheet:Compras"}
        ],
        "ventas_detectadas": [],
        "stock_detectado": [],
    }


_MAPEO_COMPRAS = {
    "sheet:Compras": {
        "fecha": "expense_date", "producto": "product_name", "cantidad": "quantity",
        "monto": "amount", "categoria": "category",
    },
}


async def test_multihoja_cantidad_con_decimales_de_un_producto_por_unidad_va_a_otros(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    yerba = await _producto(db_session, tid, "Yerba Playadito", stock=4)
    counts = await insert_confirmed_data(
        db_session, tid, _compras_multihoja("Yerba Playadito", Decimal("2.5")),
        {"gastos": True}, context_mappings=_MAPEO_COMPRAS,
    )
    await db_session.commit()

    assert await _gastos(db_session, tid) == []
    assert await _movimientos(db_session, tid) == []
    assert await _stock(db_session, yerba) == 4
    otros = await _otros(db_session, tid)
    assert len(otros) == 1
    assert (otros[0].context_label or "").startswith(_ROTULO_FRACCION)
    assert counts["filas_sin_cantidad"] == 1


async def test_multihoja_kilos_con_decimales_de_un_producto_por_peso_siguen_siendo_compra(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    queso = await _producto(db_session, tid, "Queso cremoso", factor=1000)
    await insert_confirmed_data(
        db_session, tid, _compras_multihoja("Queso cremoso", Decimal("2.5")),
        {"gastos": True}, context_mappings=_MAPEO_COMPRAS,
    )
    await db_session.commit()

    assert [g.expense_type for g in await _gastos(db_session, tid)] == ["COGS"]
    assert await _stock(db_session, queso) == 2500
    assert await _otros(db_session, tid) == []


# ── I2-6, tabla suelta con el tratamiento por hoja ───────────────────────────


_CSV_CATALOGO = b"Producto,Stock,Costo,Precio\nVela,10,1200,2000\n"


async def _catalogo_csv(db: AsyncSession, tid: uuid.UUID) -> UploadedFile:
    summary = parse_uploaded_content(_CSV_CATALOGO, "text/csv", "catalogo.csv")
    record = UploadedFile(
        tenant_id=tid,
        uploaded_by=None,
        original_filename="catalogo.csv",
        s3_key="uploads/test/catalogo.csv",
        content_type="text/csv",
        size_bytes=len(_CSV_CATALOGO),
        purpose="stock",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=summary,
    )
    db.add(record)
    await db.commit()
    return record


def _map(source: str, target: str) -> dict[str, Any]:
    return {
        "source_column": source,
        "target_field": target,
        "context_id": "table",
        "entity_type": "product",
    }


async def _confirmar(
    client: AsyncClient, headers: dict[str, str], archivo: UploadedFile, tratamiento: str
) -> None:
    resp = await client.post(
        f"/api/v1/ingestion/files/{archivo.id}/confirm",
        json={
            "column_mappings": [
                _map("Producto", "name"), _map("Stock", "stock_units"),
                _map("Costo", "unit_cost_ars"), _map("Precio", "sale_price_ars"),
            ],
            "confirmed_fields": {"productos": True},
            "context_confirmed": {"table": True},
            "stock_treatment": {"table": tratamiento},
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text


async def test_tabla_suelta_marcada_compra_por_hoja_es_una_compra(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    archivo = await _catalogo_csv(db_session, tid)
    assert archivo.parsed_summary_json["mapping_contexts"][0]["context_id"] == "table"
    await _confirmar(client, auth_headers, archivo, "purchase")
    await db_session.commit()

    # Antes: un ajuste de apertura, sin COGS ni salida de caja.
    movs = await _movimientos(db_session, tid)
    assert [(m.movement_type, m.qty) for m in movs] == [("purchase", 10)]
    gastos = await _gastos(db_session, tid)
    assert [(g.expense_type, g.amount) for g in gastos] == [("COGS", Decimal("12000.00"))]


async def test_tabla_suelta_marcada_compra_sobre_un_producto_existente_es_una_compra(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """El producto ya está en el catálogo con 2: las 8 que entran son compra."""
    tid = sample_tenant.tenant_id
    vela = await _producto(db_session, tid, "Vela", stock=2)
    archivo = await _catalogo_csv(db_session, tid)
    await _confirmar(client, auth_headers, archivo, "purchase")
    await db_session.commit()

    assert await _stock(db_session, vela) == 10
    movs = await _movimientos(db_session, tid)
    assert [(m.movement_type, m.qty) for m in movs] == [("purchase", 8)]
    gastos = await _gastos(db_session, tid)
    assert [(g.expense_type, g.amount) for g in gastos] == [("COGS", Decimal("9600.00"))]


async def test_tabla_suelta_marcada_apertura_por_hoja_sigue_siendo_apertura(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    archivo = await _catalogo_csv(db_session, tid)
    await _confirmar(client, auth_headers, archivo, "opening_balance")
    await db_session.commit()

    assert [m.movement_type for m in await _movimientos(db_session, tid)] == ["adjustment"]
    assert await _gastos(db_session, tid) == []


async def test_relectura_de_tabla_suelta_sin_mapeo_por_contexto_respeta_la_compra(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """La relectura pasa el `stock_treatment` del borrador tal cual (un dict con la
    clave de la tabla) aunque el mapeo no venga indexado por contexto."""
    tid = sample_tenant.tenant_id
    summary = parse_uploaded_content(_CSV_CATALOGO, "text/csv", "catalogo.csv")
    await insert_confirmed_data(
        db_session, tid, summary, {"productos": True},
        column_mappings={
            "Producto": "name", "Stock": "stock_units",
            "Costo": "unit_cost_ars", "Precio": "sale_price_ars",
        },
        stock_treatment={"table": "purchase"},
    )
    await db_session.commit()

    assert [m.movement_type for m in await _movimientos(db_session, tid)] == ["purchase"]
    assert [g.expense_type for g in await _gastos(db_session, tid)] == ["COGS"]


# ── I1-3 por HTTP: el confirm real, con lo que queda en la base ─────────────


async def _libro_de_compras(
    db: AsyncSession, tid: uuid.UUID, filas: list[tuple[str, str]]
) -> UploadedFile:
    """Hoja «Compras» con una fila por (producto, cantidad)."""
    summary = _compras_multihoja("x", "1")
    summary["has_gasto"] = True
    summary["gastos_detectados"] = [
        {"fecha": _FECHA, "producto": nombre, "cantidad": cantidad, "monto": "5000",
         "categoria": "Mercadería", "__context__": "sheet:Compras"}
        for nombre, cantidad in filas
    ]
    summary["row_count"] = len(filas)
    ctx = summary["mapping_contexts"][0]
    ctx["label"] = "Compras"
    ctx["preview_rows"] = summary["gastos_detectados"]
    ctx["row_count"] = len(filas)
    record = UploadedFile(
        tenant_id=tid,
        uploaded_by=None,
        original_filename="compras.xlsx",
        s3_key=f"uploads/test/{uuid.uuid4()}/compras.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size_bytes=2048,
        purpose="ingestion",
        status="uploaded",
        processing_status=PROCESSING_STATUS_NEEDS_CONFIRMATION,
        parsed_summary_json=summary,
    )
    db.add(record)
    await db.commit()
    return record


async def _confirmar_compras(
    client: AsyncClient, headers: dict[str, str], archivo: UploadedFile
) -> Any:
    return await client.post(
        f"/api/v1/ingestion/files/{archivo.id}/confirm",
        json={
            "column_mappings": [
                {"source_column": src, "target_field": dst, "context_id": "sheet:Compras",
                 "entity_type": "expense"}
                for src, dst in _MAPEO_COMPRAS["sheet:Compras"].items()
            ],
            "confirmed_fields": {"gastos": True},
            "context_confirmed": {"sheet:Compras": True},
        },
        headers=headers,
    )


async def test_http_compra_con_decimales_de_un_producto_por_unidad_va_a_otros(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """La fila con «2,5» va a «Otros» y la entera del mismo archivo entra."""
    tid = sample_tenant.tenant_id
    yerba = await _producto(db_session, tid, "Yerba Playadito", stock=4)
    mate = await _producto(db_session, tid, "Mate de calabaza")
    archivo = await _libro_de_compras(
        db_session, tid, [("Yerba Playadito", "2,5"), ("Mate de calabaza", "3")]
    )
    resp = await _confirmar_compras(client, auth_headers, archivo)
    assert resp.status_code == 200, resp.text
    await db_session.commit()

    # Antes: dos COGS, y el de la yerba sin stock ni movimiento.
    assert [(g.expense_type, g.product_id) for g in await _gastos(db_session, tid)] == [
        ("COGS", mate.id)
    ]
    movs = await _movimientos(db_session, tid)
    assert [(m.product_id, m.movement_type, m.qty) for m in movs] == [(mate.id, "purchase", 3)]
    assert await _stock(db_session, yerba) == 4
    assert await _stock(db_session, mate) == 3
    otros = await _otros(db_session, tid)
    assert len(otros) == 1
    assert (otros[0].context_label or "").startswith(_ROTULO_FRACCION)
    assert otros[0].uploaded_file_id == archivo.id


async def test_http_un_archivo_con_solo_esa_compra_no_deja_ningun_gasto(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Si es la única fila, el confirm no importa nada y lo dice (E4: cantidades
    ilegibles); antes respondía 200 con un COGS sin stock."""
    tid = sample_tenant.tenant_id
    yerba = await _producto(db_session, tid, "Yerba Playadito", stock=4)
    archivo = await _libro_de_compras(db_session, tid, [("Yerba Playadito", "2,5")])
    resp = await _confirmar_compras(client, auth_headers, archivo)
    assert resp.status_code == 422, resp.text
    assert "cantidades no se pudieron leer" in resp.json()["detail"]
    await db_session.commit()

    assert await _gastos(db_session, tid) == []
    assert await _movimientos(db_session, tid) == []
    assert await _stock(db_session, yerba) == 4
