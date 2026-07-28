"""Prevención de inflación de inventario en la ingesta (A2/A4/A5).

- A2: cada movimiento creado por la ingesta queda estampado con su origen
  (``source_type``/``source_upload_id``/``source_row_ref``/``source_row_hash``).
- A4 (guarda RC2): una hoja con columnas de gasto Y producto sobre las MISMAS
  filas produce UN solo movimiento por fila y NO duplica el producto (el caso que
  ``autoflush=False`` enmascara: el producto pendiente no se ve en el ``select``).
- A5: el stock inicial de un catálogo ES una compra real → genera su ExpenseEntry
  COGS.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

import app.application.services.ingestion_import_service as importer
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry
from app.tests.conftest import add_business_profile

# ── A5: stock inicial de catálogo genera COGS ─────────────────────────────────


@pytest.mark.asyncio
async def test_catalog_initial_stock_creates_cogs_and_stamps_movement(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Import de catálogo con stock inicial + costo ⇒ ExpenseEntry COGS ligado al
    producto y movimiento con ``source_type='catalog_initial_stock'`` (A2+A5)."""
    file_id = uuid.uuid4()
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "stock",
        "has_producto": True,
        "stock_detectado": [
            {"producto": "Yerba 1kg", "precio": "2500", "costo": "1500", "stock": "10"},
        ],
    }
    await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"productos": True},
        uploaded_file_id=file_id,
        stock_treatment="purchase",
    )

    product = (await db_session.execute(select(Product))).scalar_one()
    assert product.stock_units == 10

    # A5: marcado como compra → su COGS (10 × 1500 = 15000).
    expense = (await db_session.execute(select(ExpenseEntry))).scalar_one()
    assert expense.category == "INVENTORY"
    assert expense.expense_type == "COGS"
    assert expense.product_id == product.id
    assert expense.amount == Decimal("15000")
    assert expense.source_upload_id == file_id

    # A2: el movimiento queda estampado con su origen de catálogo.
    mv = (await db_session.execute(select(InventoryMovement))).scalar_one()
    assert mv.source_type == "catalog_initial_stock"
    assert mv.source_upload_id == file_id
    assert mv.source_row_hash  # identidad lógica estable presente
    assert mv.source_row_ref  # traza a la fila de origen


@pytest.mark.asyncio
async def test_catalog_without_cost_creates_no_cogs(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Sin costo conocido no se puede valuar la compra → no se crea COGS (el
    producto queda ``requires_completion``). El movimiento igual se estampa."""
    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "stock",
        "has_producto": True,
        "stock_detectado": [{"producto": "Sin costo", "precio": "2500", "stock": "5"}],
    }
    await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"productos": True},
        stock_treatment="purchase",
    )

    assert (await db_session.execute(select(ExpenseEntry))).first() is None
    mv = (await db_session.execute(select(InventoryMovement))).scalar_one()
    assert mv.source_type == "catalog_initial_stock"


@pytest.mark.asyncio
async def test_catalog_restock_only_charges_delta_cogs(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Producto pre-existente cuyo stock sube: el COGS es por el DELTA que entró
    (no por el stock absoluto). Reimportar el mismo nivel (delta 0) no crea COGS."""
    product = Product(
        id=uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        name="Fideos",
        sale_price_ars=Decimal("100"),
        unit_cost_ars=Decimal("60"),
        stock_units=10,
        provenance="REAL",
    )
    db_session.add(product)
    await db_session.commit()

    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "stock",
        "has_producto": True,
        "stock_detectado": [{"producto": "Fideos", "costo": "60", "stock": "15"}],
    }
    await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"productos": True},
        stock_treatment="purchase",
    )

    # Entró un delta de +5 → COGS por 5 × 60 = 300 (no por 15).
    expense = (await db_session.execute(select(ExpenseEntry))).scalar_one()
    assert expense.amount == Decimal("300")
    assert expense.product_id == product.id


# ── A4 (guarda RC2): doble bucket sobre las mismas filas ──────────────────────


def _dual_bucket_summary() -> dict[str, object]:
    """Una hoja 'general' con columnas de GASTO (total) Y de PRODUCTO (nombre +
    cantidad): las MISMAS filas matchean ``wants_gastos`` y ``wants_productos``."""
    return {
        "file_type": "spreadsheet",
        "inferred_type": "general",
        "has_gasto": True,
        "has_producto": True,
        "gastos_detectados": [
            {
                "fecha": "2024-03-10",
                "producto": "Coca Cola 500ml",
                "cantidad": "24",
                "costo": "800",
                "total": "19200",
            },
        ],
    }


@pytest.mark.asyncio
async def test_rc2_dual_bucket_no_duplicate_product_preexisting(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """RC2 con producto PRE-EXISTENTE (el caso que ``autoflush`` no enmascara):
    confirmar gastos Y productos sobre las mismas filas ⇒ UN movimiento por fila,
    sin duplicar el producto. El bloque de productos saltea la fila ya procesada
    como compra de mercadería."""
    product = Product(
        id=uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        name="Coca Cola 500ml",
        sale_price_ars=Decimal("1200"),
        unit_cost_ars=Decimal("800"),
        stock_units=0,
        provenance="REAL",
    )
    db_session.add(product)
    await db_session.commit()

    await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        _dual_bucket_summary(),
        {"gastos": True, "productos": True},
        uploaded_file_id=uuid.uuid4(),
    )

    # Un solo producto (no se duplicó) y stock sumado UNA vez.
    products = (await db_session.execute(select(Product))).scalars().all()
    assert len(products) == 1
    assert products[0].id == product.id
    assert products[0].stock_units == 24  # +24, no +48

    # Un solo movimiento (no dos) para la fila.
    movements = (await db_session.execute(select(InventoryMovement))).scalars().all()
    assert len(movements) == 1
    assert movements[0].qty == 24

    # La compra se registró como UN gasto COGS (el del bloque de gastos).
    expenses = (await db_session.execute(select(ExpenseEntry))).scalars().all()
    assert len(expenses) == 1
    assert expenses[0].expense_type == "COGS"


@pytest.mark.asyncio
async def test_rc2_dual_bucket_no_duplicate_product_autoflush_off(
    isolated_db_engine: AsyncEngine,
) -> None:
    """RC2 en prod (``autoflush=False``): el producto lo CREA el bloque de gastos y
    queda pendiente (no visible al ``select`` del bloque de productos). Sin la
    guarda A4, el bloque de productos crearía un DUPLICADO y escribiría el stock dos
    veces. Con la guarda: un producto, un movimiento."""
    factory = async_sessionmaker(
        isolated_db_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    async with factory() as session:
        tenant = Tenant(
            tenant_id=uuid.uuid4(),
            legal_name="K",
            display_name="K",
            currency="ARS",
            pricing_reference_mode="MEP",
            status="ACTIVE",
        )
        session.add(tenant)
        await session.flush()
        await add_business_profile(session, tenant.tenant_id)
        await session.commit()

        await importer.insert_confirmed_data(
            session,
            tenant.tenant_id,
            _dual_bucket_summary(),
            {"gastos": True, "productos": True},
            uploaded_file_id=uuid.uuid4(),
        )
        await session.commit()

        products = (
            await session.execute(
                select(Product).where(Product.tenant_id == tenant.tenant_id)
            )
        ).scalars().all()
        assert len(products) == 1  # NO se duplicó pese a autoflush=False
        assert products[0].stock_units == 24  # stock escrito una sola vez

        movements = (
            await session.execute(
                select(InventoryMovement).where(
                    InventoryMovement.tenant_id == tenant.tenant_id
                )
            )
        ).scalars().all()
        assert len(movements) == 1
        assert movements[0].qty == 24
