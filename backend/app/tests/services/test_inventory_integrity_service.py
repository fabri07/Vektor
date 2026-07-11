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
from app.application.services.inventory_movement_origin import (
    SOURCE_CATALOG_INITIAL_STOCK,
    SOURCE_MANUAL_ADJUSTMENT,
    SOURCE_RECONCILIATION,
)
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


async def test_purchase_tagged_catalog_counts_as_purchase_not_anchor(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Una COMPRA con source_type=catalog_initial_stock (el stock inicial de catálogo ES
    una compra real) debe contar en purchase_qty, no en anchor_qty. El total no cambia;
    el breakdown sí (antes se absorbía en el ancla y dejaba purchase_qty=0)."""
    tid = sample_tenant.tenant_id
    # esperado = 36 (compra catalog) + 30 (compra import) - 12 (venta) = 54;
    # stock_units = 200 → diff = 146, reportado.
    product = await _make_product(db_session, tid, stock_units=200, name="Compra tag catalog")
    db_session.add(_movement(tid, product.id, 36, "purchase", SOURCE_CATALOG_INITIAL_STOCK))
    db_session.add(_movement(tid, product.id, 30, "purchase", "purchase_import"))
    db_session.add(_sale(tid, product.id, 12))
    await db_session.flush()

    result = await check_tenant_inventory_integrity(db_session, tid)

    assert result["checked"] == 1
    assert len(result["divergences"]) == 1
    div = result["divergences"][0]
    assert div["anchor_qty"] == 0
    assert div["purchase_qty"] == 66
    assert div["stock_esperado"] == 54


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


async def test_loss_movement_is_now_included_in_the_formula(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """La merma (loss) ya NO saltea el producto — se suma (ya viene negativa) a la
    fórmula. Con anchor=36 y loss=-5, esperado=31; stock_units=1000 diverge muy por
    encima del umbral, así que se reporta (antes de esta tarea se salteaba como
    ledger complejo)."""
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=1000, name="Con merma")
    db_session.add(_movement(tid, product.id, 36, "adjustment", SOURCE_CATALOG_INITIAL_STOCK))
    db_session.add(_movement(tid, product.id, -5, "loss", None))
    await db_session.flush()

    result = await check_tenant_inventory_integrity(db_session, tid)

    assert result["checked"] == 1
    assert result["skipped_complex_ledger"] == 0
    assert len(result["divergences"]) == 1
    div = result["divergences"][0]
    assert div["stock_esperado"] == 31
    assert div["loss_qty"] == -5
    assert div["tagged_adjustment_qty"] == 0


async def test_no_divergence_with_tagged_adjustment_and_loss_when_stock_matches(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Ancla + compra + adjustment taggeado `reconciliation` + loss: ya no se
    saltea, y si `stock_units` coincide con la fórmula extendida no hay divergencia."""
    tid = sample_tenant.tenant_id
    # esperado = 36 + 217 + 50 (adjustment reconciliation) - 5 (loss) - 249 (ventas) = 49
    product = await _make_product(db_session, tid, stock_units=49, name="Con ajuste y merma")
    db_session.add(_movement(tid, product.id, 36, "adjustment", SOURCE_CATALOG_INITIAL_STOCK))
    db_session.add(_movement(tid, product.id, 217, "purchase", "purchase_import"))
    db_session.add(_movement(tid, product.id, 50, "adjustment", SOURCE_RECONCILIATION))
    db_session.add(_movement(tid, product.id, -5, "loss", None))
    db_session.add(_sale(tid, product.id, 249))
    await db_session.flush()

    result = await check_tenant_inventory_integrity(db_session, tid)

    assert result["checked"] == 1
    assert result["skipped_complex_ledger"] == 0
    assert result["divergences"] == []


async def test_reports_divergence_with_tagged_adjustment_and_loss_in_payload(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Mismo ledger que el caso anterior (ancla + compra + adjustment taggeado
    `manual_adjustment` + loss) pero `stock_units` no coincide: la divergencia debe
    traer `tagged_adjustment_qty` y `loss_qty` en el payload."""
    tid = sample_tenant.tenant_id
    # esperado = 36 + 217 + 50 - 5 - 249 = 49; stock_units = 300 → diff = 251.
    product = await _make_product(db_session, tid, stock_units=300, name="Con ajuste y merma 2")
    db_session.add(_movement(tid, product.id, 36, "adjustment", SOURCE_CATALOG_INITIAL_STOCK))
    db_session.add(_movement(tid, product.id, 217, "purchase", "purchase_import"))
    db_session.add(_movement(tid, product.id, 50, "adjustment", SOURCE_MANUAL_ADJUSTMENT))
    db_session.add(_movement(tid, product.id, -5, "loss", None))
    db_session.add(_sale(tid, product.id, 249))
    await db_session.flush()

    result = await check_tenant_inventory_integrity(db_session, tid)

    assert result["checked"] == 1
    assert result["skipped_complex_ledger"] == 0
    assert len(result["divergences"]) == 1
    div = result["divergences"][0]
    assert div["stock_esperado"] == 49
    assert div["diff"] == 251
    assert div["tagged_adjustment_qty"] == 50
    assert div["loss_qty"] == -5


async def test_skips_product_with_untagged_adjustment_as_complex_ledger(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Un `adjustment` sin `source_type` (legacy, no auditable) sigue salteando el
    producto — no es lo mismo que un ajuste taggeado `reconciliation`/`manual_adjustment`."""
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=100, name="Ajuste sin tag")
    db_session.add(_movement(tid, product.id, 36, "adjustment", SOURCE_CATALOG_INITIAL_STOCK))
    db_session.add(_movement(tid, product.id, 50, "adjustment", None))
    await db_session.flush()

    result = await check_tenant_inventory_integrity(db_session, tid)

    assert result["checked"] == 1
    assert result["skipped_complex_ledger"] == 1
    assert result["divergences"] == []


async def test_product_with_live_sale_movement_is_evaluated_not_skipped(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """La venta EN VIVO graba un movimiento `sale`, pero la cantidad vendida es la
    fuente de verdad desde `sales_entries`. El movimiento `sale` del ledger se IGNORA
    (no se duplica) y — a diferencia de antes — el producto ya NO se saltea: se evalúa
    y reconcilia. anchor 36 − ventas(sales_entries) 10 = 26 == stock_units 26 → diff 0."""
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=26, name="Vendido en vivo")
    db_session.add(_movement(tid, product.id, 36, "adjustment", SOURCE_CATALOG_INITIAL_STOCK))
    db_session.add(_movement(tid, product.id, -10, "sale", None))
    db_session.add(_sale(tid, product.id, 10))
    await db_session.flush()

    result = await check_tenant_inventory_integrity(db_session, tid)

    assert result["checked"] == 1
    assert result["skipped_complex_ledger"] == 0  # ya no se saltea
    assert result["divergences"] == []  # reconcilia a diff 0


async def test_product_with_sale_movement_but_inflated_stock_is_flagged(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Prueba que el producto vendido se EVALÚA de verdad: si stock_units está inflado
    respecto de anchor − ventas, la divergencia se reporta (antes quedaba oculta por el
    skip). anchor 36 − ventas 10 = 26 esperado, pero stock_units 40 → diff +14."""
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=40, name="Inflado pese a venta")
    db_session.add(_movement(tid, product.id, 36, "adjustment", SOURCE_CATALOG_INITIAL_STOCK))
    db_session.add(_movement(tid, product.id, -10, "sale", None))
    db_session.add(_sale(tid, product.id, 10))
    await db_session.flush()

    result = await check_tenant_inventory_integrity(db_session, tid)

    assert result["checked"] == 1
    assert result["skipped_complex_ledger"] == 0
    assert len(result["divergences"]) == 1
    assert result["divergences"][0]["stock_esperado"] == 26


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
