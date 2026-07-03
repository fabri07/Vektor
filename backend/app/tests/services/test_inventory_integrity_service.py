"""Tests de check_tenant_inventory_integrity: reconciliación (ancla catálogo + compras
− ventas) vs stock_units, sobre el mismo patrón que motivó el fix (tenant "don pedro",
2026-07): un ajuste sin procedencia infla stock_units sin que nadie lo note.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.inventory_integrity_service import (
    check_tenant_inventory_integrity,
)
from app.application.services.inventory_movement_origin import SOURCE_CATALOG_INITIAL_STOCK
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry


async def _make_product(
    db: AsyncSession, tenant_id: uuid.UUID, stock_units: int, name: str
) -> Product:
    product = Product(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name=name,
        sale_price_ars=Decimal("2500"),
        unit_cost_ars=Decimal("1500"),
        stock_units=stock_units,
    )
    db.add(product)
    await db.flush()
    return product


def _movement(
    tid: uuid.UUID,
    pid: uuid.UUID,
    qty: int,
    movement_type: str,
    source_type: str | None,
) -> InventoryMovement:
    return InventoryMovement(
        tenant_id=tid,
        product_id=pid,
        movement_type=movement_type,
        qty=qty,
        source_type=source_type,
    )


def _sale(tid: uuid.UUID, pid: uuid.UUID, qty: int) -> SaleEntry:
    return SaleEntry(
        tenant_id=tid,
        product_id=pid,
        amount=Decimal("100"),
        quantity=qty,
        transaction_date=datetime(2026, 6, 1, tzinfo=UTC),
    )


async def test_no_divergence_when_stock_matches_reconciliation(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=54, name="Coca Cola 1.5L")
    db_session.add(_movement(tid, product.id, 36, "adjustment", SOURCE_CATALOG_INITIAL_STOCK))
    db_session.add(_movement(tid, product.id, 30, "purchase", "purchase_import"))
    db_session.add(_sale(tid, product.id, 12))
    await db_session.flush()

    result = await check_tenant_inventory_integrity(db_session, tid)

    assert result["checked"] == 1
    assert result["divergences"] == []


async def test_reports_divergence_matching_the_real_incident_shape(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """36 inicial + 217 compras − 249 ventas = 4 esperado; stock_units=184 (inflado por
    un ajuste sin procedencia) — exactamente la forma del incidente real reconciliado
    a mano para 'don pedro' (ver conversación / memoria del proyecto)."""
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=184, name="Coca Cola 1.5L")
    db_session.add(_movement(tid, product.id, 36, "adjustment", SOURCE_CATALOG_INITIAL_STOCK))
    db_session.add(_movement(tid, product.id, 217, "purchase", "purchase_import"))
    db_session.add(_sale(tid, product.id, 249))
    await db_session.flush()

    result = await check_tenant_inventory_integrity(db_session, tid)

    assert result["checked"] == 1
    assert len(result["divergences"]) == 1
    div = result["divergences"][0]
    assert div["stock_esperado"] == 4
    assert div["stock_units"] == 184
    assert div["diff"] == 180


async def test_skips_product_without_catalog_anchor(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Producto creado por chat/manual, sin movimiento catalog_initial_stock: no se
    evalúa (no hay ancla confiable) — nunca se adivina."""
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=999, name="Producto manual")
    db_session.add(_movement(tid, product.id, 10, "purchase", "purchase_import"))
    await db_session.flush()

    result = await check_tenant_inventory_integrity(db_session, tid)

    assert result["checked"] == 0
    assert result["skipped_no_anchor"] == 1
    assert result["divergences"] == []


async def test_skips_product_with_loss_movements_as_complex_ledger(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Con merma (loss) registrada, la fórmula simple no aplica — se saltea con flag
    separado en vez de reportar un falso positivo."""
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=1000, name="Con merma")
    db_session.add(_movement(tid, product.id, 36, "adjustment", SOURCE_CATALOG_INITIAL_STOCK))
    db_session.add(_movement(tid, product.id, -5, "loss", None))
    await db_session.flush()

    result = await check_tenant_inventory_integrity(db_session, tid)

    assert result["checked"] == 1
    assert result["skipped_complex_ledger"] == 1
    assert result["divergences"] == []


async def test_small_diff_within_threshold_is_not_reported(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Diferencia chica (dentro del umbral) no genera ruido."""
    tid = sample_tenant.tenant_id
    # esperado = 36 + 0 - 0 = 36; stock_units = 38 → diff=2, no supera el piso absoluto (5).
    product = await _make_product(db_session, tid, stock_units=38, name="Diferencia chica")
    db_session.add(_movement(tid, product.id, 36, "adjustment", SOURCE_CATALOG_INITIAL_STOCK))
    await db_session.flush()

    result = await check_tenant_inventory_integrity(db_session, tid)

    assert result["divergences"] == []
