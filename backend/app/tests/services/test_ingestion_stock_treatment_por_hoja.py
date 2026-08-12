"""El origen del stock se declara POR HOJA, no una vez para todo el archivo.

`stock_treatment` era un único valor por archivo. En el archivo de ASTERIA (un
catálogo de 1258 productos + dos libros diarios con sus egresos) eso obliga a
mentir: elegir «Lo compré» genera un gasto COGS por cada producto del catálogo,
y esos costos ya están cargados como egresos en el libro diario → el costo de la
mercadería se cuenta dos veces.

Un archivo puede traer legítimamente un catálogo que el negocio ya tenía y, en
otra hoja, las compras del mes. Cada hoja declara lo suyo.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.domain.inventory_effect import CURRENT_SNAPSHOT, HISTORICAL_REPLAY
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry


def _summary_dos_hojas() -> dict[str, Any]:
    """Dos hojas de productos: una es stock que ya se tenía, otra una compra."""
    filas = [
        {
            "producto": "Vela aromática",
            "cantidad": "10",
            "costo": "1200",
            "__context__": "sheet:catalogo",
        },
        {
            "producto": "Sahumerio lavanda",
            "cantidad": "5",
            "costo": "800",
            "__context__": "sheet:compras julio",
        },
    ]
    return {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "has_producto": True,
        "stock_detectado": filas,
        "mapping_contexts": [
            {
                "context_id": "sheet:catalogo",
                "label": "catalogo",
                "source_kind": "sheet",
                "entity_type": "product",
                "headers": ["producto", "cantidad", "costo"],
                "fields": None,
                "preview_rows": filas[:1],
                "row_count": 1,
            },
            {
                "context_id": "sheet:compras julio",
                "label": "compras julio",
                "source_kind": "sheet",
                "entity_type": "product",
                "headers": ["producto", "cantidad", "costo"],
                "fields": None,
                "preview_rows": filas[1:],
                "row_count": 1,
            },
        ],
    }


_MAPEO = {
    "producto": "name",
    "cantidad": "stock_units",
    "costo": "unit_cost_ars",
}


async def test_cada_hoja_declara_su_propio_origen_de_stock(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """La hoja marcada como compra genera COGS; la de apertura no."""
    await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _summary_dos_hojas(),
        {"productos": True},
        context_mappings={
            "sheet:catalogo": _MAPEO,
            "sheet:compras julio": _MAPEO,
        },
        context_confirmed={"sheet:catalogo": True, "sheet:compras julio": True},
        stock_treatment={
            "sheet:catalogo": "opening_balance",
            "sheet:compras julio": "purchase",
        },
    )

    productos = {p.name: p for p in (await db_session.execute(select(Product))).scalars()}
    assert set(productos) == {"Vela aromática", "Sahumerio lavanda"}

    movimientos = {
        m.product_id: m
        for m in (await db_session.execute(select(InventoryMovement))).scalars()
    }
    # Apertura → ajuste (activo que ya se tenía, no toca caja).
    assert movimientos[productos["Vela aromática"].id].movement_type == "adjustment"
    # Compra → movimiento de compra.
    assert movimientos[productos["Sahumerio lavanda"].id].movement_type == "purchase"

    # Y el COGS existe SOLO para la hoja declarada como compra.
    gastos = (await db_session.execute(select(ExpenseEntry))).scalars().all()
    assert len(gastos) == 1


async def test_string_global_sigue_valiendo_para_todas_las_hojas(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Compatibilidad: un confirm viejo manda un string, no un dict."""
    await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _summary_dos_hojas(),
        {"productos": True},
        context_mappings={
            "sheet:catalogo": _MAPEO,
            "sheet:compras julio": _MAPEO,
        },
        context_confirmed={"sheet:catalogo": True, "sheet:compras julio": True},
        stock_treatment="purchase",
    )

    movimientos = (await db_session.execute(select(InventoryMovement))).scalars().all()
    assert {m.movement_type for m in movimientos} == {"purchase"}
    gastos = (await db_session.execute(select(ExpenseEntry))).scalars().all()
    assert len(gastos) == 2


async def test_sin_declarar_nada_el_default_sigue_siendo_apertura(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El default no toca caja: equivocarse ahí no inventa un gasto."""
    await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _summary_dos_hojas(),
        {"productos": True},
        context_mappings={
            "sheet:catalogo": _MAPEO,
            "sheet:compras julio": _MAPEO,
        },
        context_confirmed={"sheet:catalogo": True, "sheet:compras julio": True},
    )

    movimientos = (await db_session.execute(select(InventoryMovement))).scalars().all()
    assert {m.movement_type for m in movimientos} == {"adjustment"}
    assert (await db_session.execute(select(ExpenseEntry))).scalars().all() == []


async def test_el_eje_de_ventas_no_frena_la_apertura_ni_la_compra(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El efecto de inventario es de la HOJA DE VENTAS, no del archivo (**V16**).

    Un libro con catálogo + compras + ventas: la apertura y la compra aplican
    SIEMPRE, gobierne lo que gobierne el eje — que sólo manda sobre las ventas.

    Y el descuento de la venta no lo hace el importador: lo hace la segunda pasada
    del confirm (F-F.3), que acá no corre porque se llama al servicio directo. Por
    eso el ledger queda con apertura + compra y ningún movimiento `sale`.
    """
    fila_catalogo = {
        "producto": "Vela aromática",
        "cantidad": "10",
        "costo": "1200",
        "__context__": "sheet:catalogo",
    }
    fila_compra = {
        "fecha": "2024-03-05",
        "producto": "Vela aromática",
        "cantidad": "5",
        "monto": "6000",
        "categoria": "mercaderia",
        "__context__": "sheet:compras",
    }
    fila_venta = {
        "fecha": "2024-03-10",
        "producto": "Vela aromática",
        "cantidad": "4",
        "monto": "8400",
        "__context__": "sheet:ventas",
    }
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "has_producto": True,
        "has_gasto": True,
        "has_venta": True,
        "stock_detectado": [fila_catalogo],
        "gastos_detectados": [fila_compra],
        "ventas_detectadas": [fila_venta],
        "mapping_contexts": [
            {
                "context_id": "sheet:catalogo",
                "label": "catalogo",
                "source_kind": "sheet",
                "entity_type": "product",
                "headers": ["producto", "cantidad", "costo"],
                "fields": None,
                "preview_rows": [fila_catalogo],
                "row_count": 1,
            },
            {
                "context_id": "sheet:compras",
                "label": "compras",
                "source_kind": "sheet",
                "entity_type": "expense",
                "headers": ["fecha", "producto", "cantidad", "monto", "categoria"],
                "fields": None,
                "preview_rows": [fila_compra],
                "row_count": 1,
            },
            {
                "context_id": "sheet:ventas",
                "label": "ventas",
                "source_kind": "sheet",
                "entity_type": "sale",
                "headers": ["fecha", "producto", "cantidad", "monto"],
                "fields": None,
                "preview_rows": [fila_venta],
                "row_count": 1,
            },
        ],
    }

    await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"productos": True, "gastos": True, "ventas": True},
        context_mappings={
            "sheet:catalogo": _MAPEO,
            "sheet:compras": {
                "fecha": "expense_date",
                "producto": "product_name",
                "cantidad": "quantity",
                "monto": "amount",
                "categoria": "category",
            },
            "sheet:ventas": {
                "fecha": "transaction_date",
                "producto": "product_name",
                "cantidad": "quantity",
                "monto": "amount",
            },
        },
        context_confirmed={
            "sheet:catalogo": True,
            "sheet:compras": True,
            "sheet:ventas": True,
        },
        stock_treatment={"sheet:catalogo": "opening_balance"},
        inventory_effect={
            "sheet:catalogo": CURRENT_SNAPSHOT,
            "sheet:compras": HISTORICAL_REPLAY,
            "sheet:ventas": HISTORICAL_REPLAY,
        },
    )
    await db_session.flush()

    producto = (
        (await db_session.execute(select(Product).where(Product.name == "Vela aromática")))
        .scalars()
        .one()
    )
    # Apertura 10 + compra 5, y la venta NO descontó: 15, no 11.
    assert int(producto.stock_units) == 15
    movimientos = sorted(
        (m.movement_type, int(m.qty))
        for m in (await db_session.execute(select(InventoryMovement))).scalars()
    )
    assert movimientos == [("adjustment", 10), ("purchase", 5)]
    # La venta igual entró a los libros: el eje decide qué le pasa al STOCK,
    # nunca si la hoja se importa.
    from app.persistence.models.transaction import SaleEntry  # noqa: PLC0415

    ventas = (await db_session.execute(select(SaleEntry))).scalars().all()
    assert len(ventas) == 1


async def test_hoja_sin_declarar_cae_al_default_no_a_la_otra_hoja(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Un dict parcial no contagia el tratamiento de una hoja a las demás."""
    await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _summary_dos_hojas(),
        {"productos": True},
        context_mappings={
            "sheet:catalogo": _MAPEO,
            "sheet:compras julio": _MAPEO,
        },
        context_confirmed={"sheet:catalogo": True, "sheet:compras julio": True},
        stock_treatment={"sheet:compras julio": "purchase"},
    )

    productos = {p.name: p for p in (await db_session.execute(select(Product))).scalars()}
    movimientos = {
        m.product_id: m
        for m in (await db_session.execute(select(InventoryMovement))).scalars()
    }
    # La no declarada usa el default (apertura), no "purchase" de la otra.
    assert movimientos[productos["Vela aromática"].id].movement_type == "adjustment"
    assert movimientos[productos["Sahumerio lavanda"].id].movement_type == "purchase"
