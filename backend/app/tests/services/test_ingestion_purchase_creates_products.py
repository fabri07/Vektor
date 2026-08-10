"""Compra de mercadería ruteada a GASTOS que además crea/actualiza el producto.

Una compra de mercadería es conceptualmente AMBAS cosas: gasto (COGS + salida de
caja) Y alta/reposición de inventario. Cuando el clasificador rutea la fila a
``inferred_type="gastos"`` y el SKU/producto es NUEVO (no está en el catálogo), el
import debe:
  - registrar el gasto COGS + caja (uno por fila), y
  - crear el Product incompleto (``requires_completion=True``) + incrementar stock
    + dejar el movimiento de inventario,
sin doble conteo y SIN crear productos basura desde gastos NO-mercadería
(alquiler / servicios / sueldos → OPEX, sin producto).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.application.services.ingestion_import_service as importer
from app.persistence.models.inventory import InventoryBalance, InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry

# ── (a) Compras con SKUs NUEVOS → crea productos + stock + COGS + caja ─────────


async def test_purchase_new_skus_creates_products_stock_cogs_and_cash(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    # Fila de gasto con señal de mercadería (categoría INVENTORY) + nombre + qty.
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "has_gasto": True,
        "gastos_detectados": [
            {
                "fecha": "2024-02-01",
                "categoria": "mercaderia",
                "producto": "Coca Cola 500ml",
                "sku": "COCA-500",
                "cantidad": "24",
                "monto": "19200",
                "costo_unitario": "800",
                "forma_pago": "efectivo",
            },
            {
                "fecha": "2024-02-01",
                "categoria": "mercaderia",
                "producto": "Agua 1.5L",
                "sku": "AGUA-15",
                "cantidad": "12",
                "monto": "6000",
                "costo_unitario": "500",
                "forma_pago": "efectivo",
            },
        ],
    }
    counts = await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"gastos": True}
    )

    # Gasto COGS + caja: uno por fila.
    assert counts["gastos"] == 2
    expenses = (await db_session.execute(select(ExpenseEntry))).scalars().all()
    assert len(expenses) == 2
    assert all(e.expense_type == "COGS" for e in expenses)
    assert all(e.payment_method == "cash" for e in expenses)
    assert all(e.product_id is not None for e in expenses)

    # Producto creado (incompleto) + stock incrementado.
    products = (await db_session.execute(select(Product))).scalars().all()
    assert {p.name for p in products} == {"Coca Cola 500ml", "Agua 1.5L"}
    assert all(p.requires_completion for p in products)
    assert all(p.sale_price_ars == Decimal("0") for p in products)
    coca = next(p for p in products if p.name == "Coca Cola 500ml")
    assert coca.stock_units == 24
    assert coca.unit_cost_ars == Decimal("800")
    assert coca.sku == "COCA-500"
    agua = next(p for p in products if p.name == "Agua 1.5L")
    assert agua.stock_units == 12

    # Cada gasto vinculado a su producto (sin cruces).
    by_pid = {e.product_id for e in expenses}
    assert by_pid == {p.id for p in products}

    # Audit de inventario presente, una entrada por compra (sin doble conteo).
    movements = (await db_session.execute(select(InventoryMovement))).scalars().all()
    assert len(movements) == 2
    assert {m.qty for m in movements} == {24, 12}
    assert all(m.movement_type == "purchase" for m in movements)

    balances = (await db_session.execute(select(InventoryBalance))).scalars().all()
    assert {b.current_qty for b in balances} == {24, 12}


# ── (b) Gastos NO-mercadería → NO crea productos ──────────────────────────────


async def test_service_expenses_do_not_create_products(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    # Alquiler y servicios: OPEX. Aunque tengan "concepto" y montos, NO son
    # mercadería → no deben generar productos ni stock.
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "has_gasto": True,
        "gastos_detectados": [
            {"fecha": "2024-02-01", "categoria": "alquiler", "concepto": "Alquiler local",
             "monto": "150000"},
            {"fecha": "2024-02-02", "categoria": "luz", "concepto": "Factura Edesur",
             "monto": "45000"},
            {"fecha": "2024-02-03", "categoria": "sueldos", "concepto": "Sueldo empleado",
             "monto": "300000"},
        ],
    }
    counts = await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"gastos": True}
    )

    assert counts["gastos"] == 3
    expenses = (await db_session.execute(select(ExpenseEntry))).scalars().all()
    assert len(expenses) == 3
    assert all(e.expense_type == "OPEX" for e in expenses)
    assert all(e.product_id is None for e in expenses)

    # Ningún producto, ningún movimiento, ningún balance.
    products = (await db_session.execute(select(Product))).scalars().all()
    assert products == []
    movements = (await db_session.execute(select(InventoryMovement))).scalars().all()
    assert movements == []


async def test_merchandise_without_quantity_does_not_create_product(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    # Mercadería (COGS) PERO sin cantidad → gate estricto no crea producto.
    # El gasto sigue siendo COGS; simplemente no hay lado de inventario.
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "has_gasto": True,
        "gastos_detectados": [
            {"fecha": "2024-02-01", "categoria": "mercaderia", "producto": "Fardo surtido",
             "monto": "50000"},
        ],
    }
    counts = await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"gastos": True}
    )

    assert counts["gastos"] == 1
    expense = (await db_session.execute(select(ExpenseEntry))).scalar_one()
    assert expense.expense_type == "COGS"  # sigue siendo mercadería
    assert expense.product_id is None  # pero sin cantidad → no se crea producto
    products = (await db_session.execute(select(Product))).scalars().all()
    assert products == []


# ── (c) Compras con SKUs EXISTENTES → no duplica, incrementa stock ────────────


async def test_purchase_existing_sku_does_not_duplicate_increments_stock(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    existing = Product(
        id=uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        name="Yerba 1kg",
        sku="YERBA-1",
        sale_price_ars=Decimal("2500"),
        unit_cost_ars=Decimal("1500"),
        stock_units=10,
        provenance="REAL",
    )
    db_session.add(existing)
    await db_session.commit()

    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "has_gasto": True,
        "gastos_detectados": [
            {"fecha": "2024-02-01", "categoria": "mercaderia", "producto": "Yerba 1kg",
             "sku": "YERBA-1", "cantidad": "5", "monto": "8000", "costo_unitario": "1600"},
        ],
    }
    counts = await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"gastos": True}
    )

    assert counts["gastos"] == 1
    # No se duplicó el producto.
    products = (await db_session.execute(select(Product))).scalars().all()
    assert len(products) == 1
    await db_session.refresh(existing)
    assert existing.stock_units == 15  # 10 + 5
    # Gasto vinculado al producto pre-existente.
    expense = (await db_session.execute(select(ExpenseEntry))).scalar_one()
    assert expense.product_id == existing.id
    assert expense.expense_type == "COGS"


async def test_repeated_new_sku_in_same_file_creates_one_product(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    # Dos filas con el MISMO SKU nuevo en el mismo archivo → un solo producto,
    # stock acumulado (la 1ª fila lo crea y cachea; la 2ª lo reusa).
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "has_gasto": True,
        "gastos_detectados": [
            {"fecha": "2024-02-01", "categoria": "mercaderia", "producto": "Alfajor Jorgito",
             "sku": "ALF-J", "cantidad": "10", "monto": "3000", "costo_unitario": "300"},
            {"fecha": "2024-02-05", "categoria": "mercaderia", "producto": "Alfajor Jorgito",
             "sku": "ALF-J", "cantidad": "8", "monto": "2400", "costo_unitario": "300"},
        ],
    }
    counts = await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"gastos": True}
    )

    assert counts["gastos"] == 2
    products = (await db_session.execute(select(Product))).scalars().all()
    assert len(products) == 1  # no se duplicó
    assert products[0].stock_units == 18  # 10 + 8
    expenses = (await db_session.execute(select(ExpenseEntry))).scalars().all()
    assert {e.product_id for e in expenses} == {products[0].id}


# ── Variante multisheet: _add_expense por contexto ────────────────────────────


async def test_multisheet_purchase_new_sku_creates_product(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {"context_id": "compras", "entity_type": "expense", "label": "Compras"},
        ],
        "gastos_detectados": [
            {
                "__context__": "compras",
                "fecha": "2024-03-01",
                "categoria": "mercaderia",
                "producto": "Galletitas Oreo",
                "sku": "OREO-1",
                "cantidad": "30",
                "monto": "15000",
                "costo_unitario": "500",
                "forma_pago": "efectivo",
            },
        ],
    }
    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"gastos": True},
        context_confirmed={"compras": True},
    )

    assert counts["gastos"] == 1
    expense = (await db_session.execute(select(ExpenseEntry))).scalar_one()
    assert expense.expense_type == "COGS"
    assert expense.product_id is not None

    product = (await db_session.execute(select(Product))).scalar_one()
    assert product.name == "Galletitas Oreo"
    assert product.requires_completion is True
    assert product.sale_price_ars == Decimal("0")
    assert product.stock_units == 30
    assert product.sku == "OREO-1"
    assert expense.product_id == product.id

    movement = (await db_session.execute(select(InventoryMovement))).scalar_one()
    assert movement.qty == 30
    assert movement.movement_type == "purchase"


async def test_multisheet_service_expense_does_not_create_product(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "mixed",
        "multi_sheet": True,
        "mapping_contexts": [
            {"context_id": "fijos", "entity_type": "expense", "label": "Gastos fijos"},
        ],
        "gastos_detectados": [
            {"__context__": "fijos", "fecha": "2024-03-01", "categoria": "alquiler",
             "concepto": "Alquiler", "monto": "200000"},
        ],
    }
    counts = await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"gastos": True},
        context_confirmed={"fijos": True},
    )

    assert counts["gastos"] == 1
    expense = (await db_session.execute(select(ExpenseEntry))).scalar_one()
    assert expense.expense_type == "OPEX"
    assert expense.product_id is None
    products = (await db_session.execute(select(Product))).scalars().all()
    assert products == []
