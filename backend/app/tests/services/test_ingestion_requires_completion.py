"""FASE 3 (B2): productos auto-creados incompletos (requires_completion).

Cuando un import auto-crea un producto al que le falta precio o costo, se marca
`requires_completion=True` para que el usuario lo complete. Un producto importado
con precio y costo queda completo.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import app.application.services.ingestion_import_service as importer
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant


def _stock_summary(rows: list[dict[str, object]]) -> dict[str, object]:
    return {
        "file_type": "spreadsheet",
        "inferred_type": "stock",
        "has_producto": True,
        "stock_detectado": rows,
    }


@pytest.mark.asyncio
async def test_complete_product_not_flagged(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    summary = _stock_summary(
        [{"producto": "Yerba 1kg", "precio": "2500", "costo": "1500", "stock": "10"}]
    )
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )
    product = (await db_session.execute(select(Product))).scalar_one()
    assert product.requires_completion is False


@pytest.mark.asyncio
async def test_autocreated_without_cost_is_flagged(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    summary = _stock_summary([{"producto": "Sin costo", "precio": "2500", "stock": "5"}])
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )
    product = (await db_session.execute(select(Product))).scalar_one()
    assert product.requires_completion is True  # falta unit_cost


@pytest.mark.asyncio
async def test_autocreated_without_price_is_flagged(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    summary = _stock_summary([{"producto": "Sin precio", "costo": "800", "stock": "5"}])
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"productos": True}
    )
    product = (await db_session.execute(select(Product))).scalar_one()
    assert product.requires_completion is True  # precio default 0 → incompleto
    assert product.sale_price_ars == Decimal("0")
    # El cierre del flag vía PATCH se prueba en test_products.py
    # (test_patch_completing_data_clears_requires_completion).


@pytest.mark.asyncio
async def test_purchase_book_without_category_creates_product(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Libro de compras: una fila de gasto con NOMBRE de producto + CANTIDAD crea
    el producto (COGS) aunque NO traiga columna de categoría. Antes el
    ``expense_type`` se calculaba antes de crear el producto → un producto nuevo
    quedaba OPEX y nunca se creaba (bug huevo-y-gallina)."""
    from app.persistence.models.transaction import ExpenseEntry  # noqa: PLC0415

    summary = {
        "file_type": "spreadsheet",
        "inferred_type": "gastos",
        "gastos_detectados": [
            {"fecha": "2026-03-10", "producto": "Sprite 500ml", "cantidad": "12", "total": "1100"},
        ],
    }
    await importer.insert_confirmed_data(
        db_session, sample_tenant.tenant_id, summary, {"gastos": True}
    )

    product = (await db_session.execute(select(Product))).scalar_one()
    assert product.name == "Sprite 500ml"
    assert product.requires_completion is True  # compra: sin precio de venta
    assert product.stock_units == 12  # _apply_purchase_to_stock sumó la cantidad

    expense = (await db_session.execute(select(ExpenseEntry))).scalar_one()
    assert expense.expense_type == "COGS"
    assert expense.product_id == product.id

    # occurred_at (fecha de NEGOCIO) = fecha real de la fila del gasto (2026-03-10),
    # NO la fecha de carga (created_at ≈ ahora): así el dedup por fecha de negocio
    # nunca confunde esta compra con una cargada el mismo día pero ocurrida en otro mes.
    movement = (await db_session.execute(select(InventoryMovement))).scalar_one()
    assert movement.occurred_at is not None
    assert movement.occurred_at.replace(tzinfo=None) == expense.transaction_date
    assert movement.occurred_at.replace(tzinfo=None) != movement.created_at.replace(
        tzinfo=None
    )


@pytest.mark.asyncio
async def test_catalog_stock_purchase_sets_occurred_at_from_tx_date(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Catálogo con stock marcado como COMPRA (``stock_treatment=purchase``): el
    ``InventoryMovement`` queda estampado con la misma fecha de negocio (``tx_date``)
    que el gasto COGS que genera — ambos derivan de la misma variable en el import."""
    from app.persistence.models.transaction import ExpenseEntry  # noqa: PLC0415

    summary = _stock_summary(
        [{"producto": "Fernet 750ml", "precio": "3000", "costo": "1800", "stock": "6"}]
    )
    await importer.insert_confirmed_data(
        db_session,
        sample_tenant.tenant_id,
        summary,
        {"productos": True},
        stock_treatment="purchase",
    )

    expense = (await db_session.execute(select(ExpenseEntry))).scalar_one()
    assert expense.expense_type == "COGS"

    movement = (await db_session.execute(select(InventoryMovement))).scalar_one()
    assert movement.movement_type == "purchase"
    assert movement.occurred_at is not None
    assert movement.occurred_at.replace(tzinfo=None) == expense.transaction_date


@pytest.mark.asyncio
async def test_purchase_new_product_gets_stock_without_autoflush(
    isolated_db_engine: AsyncEngine,
) -> None:
    """Reproduce prod (``autoflush=False``): un producto NUEVO de una compra recibe
    su stock vía el ``product_cache``. Sin el cache, ``session.get`` no ve el
    producto pendiente (no flusheado) y el stock quedaba en 0."""
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
        await session.commit()

        summary = {
            "file_type": "spreadsheet",
            "inferred_type": "gastos",
            "gastos_detectados": [
                {
                    "fecha": "2026-03-10",
                    "producto": "Producto Nuevo X",
                    "cantidad": "7",
                    "total": "500",
                },
            ],
        }
        await importer.insert_confirmed_data(
            session, tenant.tenant_id, summary, {"gastos": True}
        )
        await session.commit()

        product = (
            await session.execute(
                select(Product).where(Product.tenant_id == tenant.tenant_id)
            )
        ).scalar_one()
        assert product.name == "Producto Nuevo X"
        assert product.stock_units == 7  # el cache entregó el producto nuevo
