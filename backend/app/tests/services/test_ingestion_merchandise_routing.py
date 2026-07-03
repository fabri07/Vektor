"""FASE 3: routing de compras de mercadería → inventario (no gastos).

Conservador: solo se rerutea a stock si hay señal de mercadería/insumo Y columna
de cantidad. Las compras de servicios/alquiler (sin columnas de inventario) NO se
tocan. Casos ambiguos no se importan mal en silencio (quedan en su tipo + el
usuario confirma).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.application.services.file_parsing import analyze_headers, parse_uploaded_content
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry

# ── Clasificación (parser) ────────────────────────────────────────────────────


def test_merchandise_with_quantity_classified_as_stock() -> None:
    assert analyze_headers(["fecha", "mercaderia", "cantidad", "costo"])["inferred_type"] == "stock"
    assert analyze_headers(["fecha", "insumo", "unidades", "costo"])["inferred_type"] == "stock"


def test_compra_without_quantity_not_stock() -> None:
    # "compra" sin columnas de producto/cantidad → NO se rerutea a stock (conservador).
    assert analyze_headers(["fecha", "compra", "monto", "proveedor"])["inferred_type"] != "stock"


def test_merchandise_without_quantity_not_aggressively_routed() -> None:
    # mercadería SIN cantidad → ambiguo → no se fuerza a stock.
    assert analyze_headers(["fecha", "mercaderia", "monto"])["inferred_type"] != "stock"


def test_service_expense_not_routed_to_stock() -> None:
    # "compra de servicios / categoría / proveedor" → nunca stock.
    assert analyze_headers(["fecha", "categoria", "importe", "proveedor"])["inferred_type"] != (
        "stock"
    )


# ── End-to-end: CSV de compra de mercadería → productos, no gastos ────────────


def test_merchandise_csv_parsed_as_stock() -> None:
    csv = (
        b"fecha,mercaderia,cantidad,costo\n"
        b"2024-01-15,Coca Cola 500ml,24,800\n"
        b"2024-01-15,Agua 1.5L,12,500\n"
    )
    summary = parse_uploaded_content(csv, "text/csv", "compra_mercaderia.csv")
    assert summary["inferred_type"] == "stock"


@pytest.mark.asyncio
async def test_merchandise_import_creates_products_not_expenses(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "stock",
        "has_producto": True,
        "stock_detectado": [
            {"fecha": "2024-01-15", "mercaderia": "Coca Cola 500ml", "cantidad": "24",
             "costo": "800"},
            {"fecha": "2024-01-15", "mercaderia": "Agua 1.5L", "cantidad": "12", "costo": "500"},
        ],
    }
    # DEFAULT (sin stock_treatment) = saldo de apertura: entra al inventario como
    # activo, SIN gasto ni salida de caja. El movimiento es un ajuste, no una compra.
    counts = await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )

    assert counts["productos"] == 2
    assert counts["gastos"] == 0
    products = (await db_session.execute(select(Product))).scalars().all()
    names = {p.name for p in products}
    assert names == {"Coca Cola 500ml", "Agua 1.5L"}  # nombre tomado de "mercaderia"
    coca = next(p for p in products if p.name == "Coca Cola 500ml")
    assert coca.stock_units == 24  # cantidad → stock

    # Saldo de apertura ⇒ NINGÚN gasto (no distorsiona caja).
    expenses = (await db_session.execute(select(ExpenseEntry))).scalars().all()
    assert expenses == []
    # Movimiento estampado catalog_initial_stock, pero como AJUSTE (no compra).
    movements = (await db_session.execute(select(InventoryMovement))).scalars().all()
    assert len(movements) == 2
    assert {m.qty for m in movements} == {24, 12}
    assert {m.source_type for m in movements} == {"catalog_initial_stock"}
    assert {m.movement_type for m in movements} == {"adjustment"}


@pytest.mark.asyncio
async def test_merchandise_import_as_purchase_creates_cogs(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Con stock_treatment='purchase', el catálogo SÍ genera COGS + movimiento de compra."""
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "stock",
        "has_producto": True,
        "stock_detectado": [
            {"fecha": "2024-01-15", "mercaderia": "Coca Cola 500ml", "cantidad": "24",
             "costo": "800"},
            {"fecha": "2024-01-15", "mercaderia": "Agua 1.5L", "cantidad": "12", "costo": "500"},
        ],
    }
    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"productos": True},
        stock_treatment="purchase",
    )

    assert counts["productos"] == 2
    assert counts["gastos"] == 0  # el COGS de catálogo no infla el conteo de gastos
    products = (await db_session.execute(select(Product))).scalars().all()
    coca = next(p for p in products if p.name == "Coca Cola 500ml")
    agua = next(p for p in products if p.name == "Agua 1.5L")

    # Compra ⇒ cada línea con costo genera su ExpenseEntry COGS (INVENTORY/COGS).
    expenses = (await db_session.execute(select(ExpenseEntry))).scalars().all()
    assert len(expenses) == 2
    assert {e.category for e in expenses} == {"INVENTORY"}
    assert {e.expense_type for e in expenses} == {"COGS"}
    by_product = {e.product_id: e for e in expenses}
    assert by_product[coca.id].amount == Decimal("19200")  # 24 × 800
    assert by_product[agua.id].amount == Decimal("6000")  # 12 × 500

    # Movimiento de COMPRA estampado con origen de catálogo.
    movements = (await db_session.execute(select(InventoryMovement))).scalars().all()
    assert len(movements) == 2
    assert {m.movement_type for m in movements} == {"purchase"}
    assert {m.source_type for m in movements} == {"catalog_initial_stock"}
