"""Tests del motor de reconstrucción TEMPORAL de stock (inventory_temporal_service).

Verifica la SECUENCIA (ventas datadas antes que las compras que las cubren), no la
magnitud. Casos clave: anclaje que ignora la fecha del snapshot, desempate intra-día
crédito-antes-que-débito (no falso positivo), y el invariante ``ending_balance ==
stock_esperado`` del chequeo agregado.
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
)
from app.application.services.inventory_temporal_service import (
    CAUSE_NO_PURCHASES_OR_OVERSOLD,
    CAUSE_PURCHASES_DATED_AFTER_SALES,
    _Event,
    check_products_temporal_divergence,
    replay_timeline,
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
    occurred_at: datetime | None = None,
) -> InventoryMovement:
    return InventoryMovement(
        tenant_id=tid,
        product_id=pid,
        movement_type=movement_type,
        qty=qty,
        source_type=source_type,
        occurred_at=occurred_at,
    )


def _sale(
    tid: uuid.UUID, pid: uuid.UUID | None, qty: int, transaction_date: datetime
) -> SaleEntry:
    return SaleEntry(
        tenant_id=tid,
        product_id=pid,
        amount=Decimal("100"),
        quantity=qty,
        transaction_date=transaction_date,
    )


def _d(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


# ── Función pura ────────────────────────────────────────────────────────────────


def test_replay_pure_credit_before_debit_same_day() -> None:
    """Mismo día: crédito primero → no toca negativo aunque la venta iguale la compra."""
    from datetime import date

    events = [
        _Event(date(2026, 1, 1), -10, kind_rank=3),  # venta
        _Event(date(2026, 1, 1), 10, kind_rank=0),  # compra (mismo día)
    ]
    result = replay_timeline(opening_anchor_qty=0, events=events)
    assert result.min_balance == 0
    assert result.ending_balance == 0
    assert result.first_negative_at is None


def test_replay_pure_detects_dip_and_dates() -> None:
    from datetime import date

    events = [
        _Event(date(2026, 1, 10), -10, kind_rank=3),  # venta antes
        _Event(date(2026, 2, 1), 20, kind_rank=0),  # compra después
    ]
    result = replay_timeline(opening_anchor_qty=2, events=events)
    assert result.min_balance == -8
    assert result.min_balance_at == date(2026, 1, 10)
    assert result.first_negative_at == date(2026, 1, 10)
    assert result.ending_balance == 12


# ── check_products_temporal_divergence (DB) ──────────────────────────────────────


async def test_purchase_dated_after_sale_is_flagged(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Venta el 10-ene, compra que la cubre el 01-feb → el path cae negativo aunque el
    agregado dé positivo. Causa PURCHASES_DATED_AFTER_SALES."""
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=12, name="Coca 1.5L")
    db_session.add(_movement(tid, product.id, 2, "adjustment", SOURCE_CATALOG_INITIAL_STOCK))
    db_session.add(_movement(tid, product.id, 20, "purchase", "purchase_import", _d(2026, 2, 1)))
    db_session.add(_sale(tid, product.id, 10, _d(2026, 1, 10)))
    await db_session.flush()

    result = await check_products_temporal_divergence(db_session, tid)

    assert result.checked == 1
    assert len(result.divergences) == 1
    div = result.divergences[0]
    assert div.cause == CAUSE_PURCHASES_DATED_AFTER_SALES
    assert div.min_balance == -8
    assert div.ending_balance == 12
    assert div.first_negative_at is not None and div.first_negative_at.isoformat() == "2026-01-10"
    assert div.total_purchases == 20
    assert div.total_sales == 10


async def test_oversold_is_flagged_as_no_purchases(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Ventas superan compras+inicial también en el agregado (ending < 0)."""
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=0, name="Sobrevendido")
    db_session.add(_movement(tid, product.id, 2, "adjustment", SOURCE_CATALOG_INITIAL_STOCK))
    db_session.add(_movement(tid, product.id, 5, "purchase", "purchase_import", _d(2026, 1, 1)))
    db_session.add(_sale(tid, product.id, 20, _d(2026, 1, 2)))
    await db_session.flush()

    result = await check_products_temporal_divergence(db_session, tid)

    assert len(result.divergences) == 1
    div = result.divergences[0]
    assert div.cause == CAUSE_NO_PURCHASES_OR_OVERSOLD
    assert div.ending_balance == -13


async def test_same_day_purchase_covers_sale_no_false_positive(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Compra y venta el MISMO día → crédito primero → sin divergencia."""
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=0, name="Mismo día")
    db_session.add(_movement(tid, product.id, 0, "adjustment", SOURCE_CATALOG_INITIAL_STOCK))
    db_session.add(_movement(tid, product.id, 10, "purchase", "purchase_import", _d(2026, 1, 1)))
    db_session.add(_sale(tid, product.id, 10, _d(2026, 1, 1)))
    await db_session.flush()

    result = await check_products_temporal_divergence(db_session, tid)

    assert result.checked == 1
    assert result.divergences == []


async def test_late_anchor_date_is_ignored_no_false_positive(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El ancla (100) tiene occurred_at posterior a la venta (05-ene): su fecha se
    IGNORA, el opening se siembra desde el inicio → sin divergencia."""
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=90, name="Ancla tardía")
    db_session.add(
        _movement(tid, product.id, 100, "adjustment", SOURCE_CATALOG_INITIAL_STOCK, _d(2026, 2, 1))
    )
    db_session.add(_sale(tid, product.id, 10, _d(2026, 1, 5)))
    await db_session.flush()

    result = await check_products_temporal_divergence(db_session, tid)

    assert result.checked == 1
    assert result.divergences == []


async def test_product_without_anchor_is_skipped(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=5, name="Sin ancla")
    db_session.add(_movement(tid, product.id, 10, "purchase", "purchase_import", _d(2026, 1, 1)))
    db_session.add(_sale(tid, product.id, 30, _d(2026, 1, 2)))
    await db_session.flush()

    result = await check_products_temporal_divergence(db_session, tid)

    assert result.checked == 0
    assert result.skipped_no_anchor == 1
    assert result.divergences == []


async def test_complex_ledger_return_is_skipped(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=5, name="Con return")
    db_session.add(_movement(tid, product.id, 2, "adjustment", SOURCE_CATALOG_INITIAL_STOCK))
    db_session.add(_movement(tid, product.id, 3, "return", None, _d(2026, 1, 1)))
    db_session.add(_sale(tid, product.id, 30, _d(2026, 1, 2)))
    await db_session.flush()

    result = await check_products_temporal_divergence(db_session, tid)

    assert result.checked == 1
    assert result.skipped_complex_ledger == 1
    assert result.divergences == []


async def test_small_dip_within_floor_is_not_reported(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Dip de -2 (dentro del piso 5) → no se reporta."""
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=0, name="Dip chico")
    db_session.add(_movement(tid, product.id, 2, "adjustment", SOURCE_CATALOG_INITIAL_STOCK))
    db_session.add(_sale(tid, product.id, 4, _d(2026, 1, 1)))
    await db_session.flush()

    result = await check_products_temporal_divergence(db_session, tid)

    assert result.divergences == []


async def test_null_product_id_sale_does_not_affect_replay(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Una venta con product_id NULL (no atribuida) no debe afectar el replay del
    producto: sólo se cuentan las ventas de ese product_id."""
    tid = sample_tenant.tenant_id
    product = await _make_product(db_session, tid, stock_units=2, name="Con venta huérfana")
    db_session.add(_movement(tid, product.id, 2, "adjustment", SOURCE_CATALOG_INITIAL_STOCK))
    db_session.add(_sale(tid, None, 100, _d(2026, 1, 1)))  # huérfana
    await db_session.flush()

    result = await check_products_temporal_divergence(db_session, tid)

    assert result.checked == 1
    assert result.divergences == []


async def test_invariant_ending_balance_equals_aggregate_stock_esperado(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El ending_balance del replay temporal debe igualar el stock_esperado del chequeo
    agregado (ancla + compras + tagged + loss − ventas)."""
    tid = sample_tenant.tenant_id
    # stock_units inflado (999) para forzar que el AGREGADO reporte y exponga
    # stock_esperado; el temporal usa stock_units solo como contexto (no en la fórmula).
    product = await _make_product(db_session, tid, stock_units=999, name="Invariante")
    db_session.add(_movement(tid, product.id, 2, "adjustment", SOURCE_CATALOG_INITIAL_STOCK))
    db_session.add(_movement(tid, product.id, 20, "purchase", "purchase_import", _d(2026, 2, 1)))
    db_session.add(_sale(tid, product.id, 10, _d(2026, 1, 10)))
    await db_session.flush()

    temporal = await check_products_temporal_divergence(db_session, tid)
    aggregate = await check_tenant_inventory_integrity(db_session, tid)

    assert len(temporal.divergences) == 1
    assert len(aggregate["divergences"]) == 1
    # Invariante: el ending_balance del replay == stock_esperado del agregado.
    assert temporal.divergences[0].ending_balance == aggregate["divergences"][0]["stock_esperado"]


async def test_confirm_flow_derives_product_ids_by_source_upload_id(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Contrato del consumidor 3 (warning en confirm): las ventas de un import se derivan
    por source_upload_id y se pasan al motor. Ventas datadas antes de la compra tardía →
    divergencia; una venta de OTRO upload no entra en el scope."""
    from sqlalchemy import select

    tid = sample_tenant.tenant_id
    upload_id = uuid.uuid4()
    product = await _make_product(db_session, tid, stock_units=12, name="Importado")
    db_session.add(_movement(tid, product.id, 2, "adjustment", SOURCE_CATALOG_INITIAL_STOCK))
    db_session.add(_movement(tid, product.id, 20, "purchase", "purchase_import", _d(2026, 2, 1)))
    sale = _sale(tid, product.id, 10, _d(2026, 1, 10))
    sale.source_upload_id = upload_id
    db_session.add(sale)
    await db_session.flush()

    affected = (
        (
            await db_session.execute(
                select(SaleEntry.product_id)
                .where(
                    SaleEntry.tenant_id == tid,
                    SaleEntry.source_upload_id == upload_id,
                    SaleEntry.product_id.isnot(None),
                )
                .distinct()
            )
        )
        .scalars()
        .all()
    )
    assert affected == [product.id]

    result = await check_products_temporal_divergence(
        db_session, tid, product_ids=[pid for pid in affected if pid is not None]
    )

    assert len(result.divergences) == 1
    assert result.divergences[0].cause == CAUSE_PURCHASES_DATED_AFTER_SALES


async def test_product_ids_scope_restricts_and_counts_skipped(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Con product_ids acotado (consumidor 3), sólo se evalúan esos; los del set sin
    ancla cuentan como skipped_no_anchor."""
    tid = sample_tenant.tenant_id
    with_anchor = await _make_product(db_session, tid, stock_units=12, name="Con ancla")
    db_session.add(_movement(tid, with_anchor.id, 2, "adjustment", SOURCE_CATALOG_INITIAL_STOCK))
    db_session.add(
        _movement(tid, with_anchor.id, 20, "purchase", "purchase_import", _d(2026, 2, 1))
    )
    db_session.add(_sale(tid, with_anchor.id, 10, _d(2026, 1, 10)))
    without_anchor = await _make_product(db_session, tid, stock_units=5, name="Sin ancla scope")
    await db_session.flush()

    result = await check_products_temporal_divergence(
        db_session, tid, product_ids=[with_anchor.id, without_anchor.id]
    )

    assert result.checked == 1
    assert result.skipped_no_anchor == 1
    assert len(result.divergences) == 1
    assert result.divergences[0].product_id == str(with_anchor.id)
