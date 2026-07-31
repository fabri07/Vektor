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

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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
