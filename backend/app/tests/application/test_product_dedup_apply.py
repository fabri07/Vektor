"""Tests de product_dedup_service (F3-T5) — ejecución --apply de las mutaciones.

Consume el plan dry-run de T4 (``persist_dedup_plan``), revalida el fingerprint por
grupo y ejecuta las mutaciones reales (merge de campos, re-apunte de FKs,
consolidación/borrado de balance, desactivación del duplicado) en su propia
transacción por grupo, aplicando el delta de stock group-level UNA sola vez.

Cubre contra SQLite: apply SUM simple, apply catálogo MOST_RECENT (no pairwise),
aborto por fingerprint (PARTIALLY_APPLIED), aborto por rowcount de REPOINT_FK,
guard de balance NUEVO negativo (skip balance_inconsistency), completar SOLO NULLs en
el MERGE (custom_fields shallow sin claves "_"), idempotencia (re-apply de un source
full-APPLIED aborta), reintento seguro (un run PARTIALLY_APPLIED deja el source
re-aplicable), guarda del lease (heartbeat), y el guard de CLI ``--apply`` sin
``--source-run-id``.
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.application.services import product_dedup_service as svc
from app.application.services.inventory_movement_origin import (
    SOURCE_CATALOG_INITIAL_STOCK,
    SOURCE_PURCHASE_IMPORT,
    SOURCE_RECEIPT,
)
from app.persistence.models.inventory import InventoryBalance, InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairItem, DataRepairRun
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry

_BASE = datetime(2026, 1, 1, tzinfo=UTC)
_BACKEND_ROOT = Path(__file__).resolve().parents[3]


@pytest_asyncio.fixture
async def db_session(isolated_db_engine: AsyncEngine):  # type: ignore[no-untyped-def]
    """Shadow del ``db_session`` de conftest con una session de COMMIT REAL sobre un
    engine aislado. El apply commitea por grupo (transacción propia) → releasea la
    barrera exclusive por grupo; el ``db_session`` compartido de conftest engancha un
    listener ``begin`` (raw ``BEGIN``) incompatible con esos ciclos commit/begin. Esto
    espeja producción: el script usa su propia session sobre su propio engine."""
    async with AsyncSession(isolated_db_engine, expire_on_commit=False) as session:
        yield session


# ── Helpers de siembra ───────────────────────────────────────────────────────────


async def _add_product(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    name: str,
    sku: str | None = None,
    barcode: str | None = None,
    stock_units: int = 0,
    unit_cost: Decimal | None = None,
    category: str | None = None,
    custom_fields: dict | None = None,
    sale_price: Decimal = Decimal("100.00"),
) -> Product:
    p = Product(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name=name,
        sku=sku,
        barcode=barcode,
        sale_price_ars=sale_price,
        unit_cost_ars=unit_cost,
        category=category,
        stock_units=stock_units,
        custom_fields=custom_fields or {},
        is_active=True,
    )
    session.add(p)
    await session.flush()
    return p


async def _add_movement(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    product_id: uuid.UUID,
    qty: int,
    source_type: str,
    source_row_hash: str | None = None,
    *,
    movement_type: str = "purchase",
    occurred_offset_days: int | None = None,
) -> InventoryMovement:
    occ = (
        _BASE + timedelta(days=occurred_offset_days)
        if occurred_offset_days is not None
        else None
    )
    m = InventoryMovement(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        product_id=product_id,
        movement_type=movement_type,
        qty=qty,
        source_type=source_type,
        source_row_hash=source_row_hash,
        occurred_at=occ,
    )
    session.add(m)
    await session.flush()
    return m


async def _add_balance(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    product_id: uuid.UUID,
    current_qty: int,
    reserved_qty: int = 0,
) -> InventoryBalance:
    b = InventoryBalance(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        product_id=product_id,
        current_qty=current_qty,
        reserved_qty=reserved_qty,
    )
    session.add(b)
    await session.flush()
    return b


async def _add_sale(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    product_id: uuid.UUID,
    amount: Decimal = Decimal("100.00"),
) -> SaleEntry:
    s = SaleEntry(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        product_id=product_id,
        amount=amount,
        quantity=1,
        transaction_date=_BASE,
        payment_method="cash",
    )
    session.add(s)
    await session.flush()
    return s


async def _add_expense(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    product_id: uuid.UUID,
    amount: Decimal = Decimal("50.00"),
) -> ExpenseEntry:
    e = ExpenseEntry(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        product_id=product_id,
        amount=amount,
        category="INVENTORY",
        transaction_date=_BASE,
        description="compra",
    )
    session.add(e)
    await session.flush()
    return e


async def _plan_and_persist(session: AsyncSession, tenant_id: uuid.UUID) -> uuid.UUID:
    plan = await svc.plan_dedup(session, tenant_id)
    return await svc.persist_dedup_plan(session, tenant_id, plan)


# ── Apply SUM simple (2 productos) ───────────────────────────────────────────────


async def test_apply_simple_sum_group(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    # canónico: gana por barcode (aunque tenga MENOS FKs que el dup).
    canonical = await _add_product(
        db_session, tid, name="Coca", sku="COCA500", barcode="7790011110001", stock_units=10
    )
    dup = await _add_product(
        db_session, tid, name="Coca 500", sku="COCA500", stock_units=7, unit_cost=Decimal("50")
    )
    await _add_movement(db_session, tid, canonical.id, 10, SOURCE_PURCHASE_IMPORT, "h1")
    dup_mov = await _add_movement(db_session, tid, dup.id, 7, SOURCE_RECEIPT, "h2")
    await _add_balance(db_session, tid, canonical.id, 10)
    await _add_balance(db_session, tid, dup.id, 7)
    sale = await _add_sale(db_session, tid, dup.id)
    expense = await _add_expense(db_session, tid, dup.id)
    await db_session.flush()

    source_run_id = await _plan_and_persist(db_session, tid)

    result = await svc.apply_dedup_plan(db_session, tid, source_run_id, lease_id=None)
    assert result.status == "APPLIED"
    assert result.groups_applied == 1
    assert result.groups_skipped == 0
    assert result.groups_failed == 0

    db_session.expunge_all()

    # FKs re-apuntadas al canónico (ventas / gastos / movimientos).
    refreshed_sale = await db_session.get(SaleEntry, sale.id)
    assert refreshed_sale is not None and refreshed_sale.product_id == canonical.id
    refreshed_exp = await db_session.get(ExpenseEntry, expense.id)
    assert refreshed_exp is not None and refreshed_exp.product_id == canonical.id
    refreshed_mov = await db_session.get(InventoryMovement, dup_mov.id)
    assert refreshed_mov is not None and refreshed_mov.product_id == canonical.id

    # Balance del dup borrado; canónico sube por el delta group-level UNA sola vez.
    dup_bal = (
        await db_session.execute(
            select(InventoryBalance).where(InventoryBalance.product_id == dup.id)
        )
    ).scalar_one_or_none()
    assert dup_bal is None
    canon_bal = (
        await db_session.execute(
            select(InventoryBalance).where(InventoryBalance.product_id == canonical.id)
        )
    ).scalar_one()
    assert canon_bal.current_qty == 17  # 10 + delta(7)

    rc = await db_session.get(Product, canonical.id)
    assert rc is not None
    assert rc.stock_units == 17  # 10 + delta(7), una sola vez
    assert rc.is_active is True  # el canónico NO se desactiva
    assert rc.unit_cost_ars == Decimal("50")  # completado del dup (estaba NULL)

    rd = await db_session.get(Product, dup.id)
    assert rd is not None
    assert rd.is_active is False
    assert rd.deactivation_reason == "DUPLICATE"
    assert rd.deactivated_at is not None
    assert rd.stock_units == 7  # NO se pone en 0 (la reversa lo necesita)

    # El source dry-run quedó consumido (APPLIED).
    src = await db_session.get(DataRepairRun, source_run_id)
    assert src is not None and src.status == "APPLIED"


async def test_apply_records_items_for_revert(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    canonical = await _add_product(
        db_session, tid, name="Agua", sku="AGUA1", barcode="7790022220002", stock_units=0
    )
    dup = await _add_product(db_session, tid, name="Agua x", sku="AGUA1", stock_units=5)
    await _add_movement(db_session, tid, dup.id, 5, SOURCE_PURCHASE_IMPORT, "hA")
    await _add_balance(db_session, tid, dup.id, 5)
    await _add_sale(db_session, tid, dup.id)
    await db_session.flush()

    source_run_id = await _plan_and_persist(db_session, tid)
    result = await svc.apply_dedup_plan(db_session, tid, source_run_id, lease_id=None)

    items = (
        (
            await db_session.execute(
                select(DataRepairItem).where(DataRepairItem.run_id == result.run_id)
            )
        )
        .scalars()
        .all()
    )
    actions = [i.action for i in items]
    assert "MERGE_PRODUCT" in actions
    assert "REPOINT_FK" in actions
    assert "DELETE_BALANCE" in actions
    assert "DEACTIVATE_DUPLICATE" in actions
    # El MERGE registra el delta aplicado real (para que T6 lo reste).
    merge = next(i for i in items if i.action == "MERGE_PRODUCT")
    assert merge.after_json["stock_delta_applied"] == 5
    assert merge.after_json["stock_units_before"] == 0
    assert merge.after_json["stock_units_after"] == 5
    # El REPOINT registra las filas y el nuevo product_id (reversa a old_product_id).
    repoint = next(i for i in items if i.action == "REPOINT_FK")
    assert repoint.after_json["new_product_id"] == str(canonical.id)
    assert repoint.before_json["rows"]
    assert all(r["old_product_id"] == str(dup.id) for r in repoint.before_json["rows"])


# ── Apply catálogo 3 miembros (MOST_RECENT, no pairwise) ─────────────────────────


async def test_apply_catalog_three_members_most_recent(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    canonical = await _add_product(
        db_session, tid, name="Yerba", sku="YERBA", barcode="7790033330003", stock_units=5
    )
    d1 = await _add_product(db_session, tid, name="Yerba 1", sku="YERBA", stock_units=8)
    d2 = await _add_product(db_session, tid, name="Yerba 2", sku="YERBA", stock_units=6)
    await _add_movement(
        db_session, tid, canonical.id, 5, SOURCE_CATALOG_INITIAL_STOCK, "c", occurred_offset_days=1
    )
    await _add_movement(
        db_session, tid, d1.id, 8, SOURCE_CATALOG_INITIAL_STOCK, "d1", occurred_offset_days=10
    )
    await _add_movement(
        db_session, tid, d2.id, 6, SOURCE_CATALOG_INITIAL_STOCK, "d2", occurred_offset_days=5
    )
    await _add_balance(db_session, tid, canonical.id, 5)
    await _add_balance(db_session, tid, d1.id, 8)
    await _add_balance(db_session, tid, d2.id, 6)
    await db_session.flush()

    source_run_id = await _plan_and_persist(db_session, tid)
    result = await svc.apply_dedup_plan(db_session, tid, source_run_id, lease_id=None)
    assert result.status == "APPLIED"
    assert result.groups_applied == 1

    db_session.expunge_all()
    rc = await db_session.get(Product, canonical.id)
    assert rc is not None
    # Ancla más reciente (día10, qty8) → NO suma pairwise (que daría 9).
    assert rc.stock_units == 8
    canon_bal = (
        await db_session.execute(
            select(InventoryBalance).where(InventoryBalance.product_id == canonical.id)
        )
    ).scalar_one()
    assert canon_bal.current_qty == 8  # 5 + delta(3)

    for d in (d1, d2):
        rd = await db_session.get(Product, d.id)
        assert rd is not None and rd.is_active is False
        dbal = (
            await db_session.execute(
                select(InventoryBalance).where(InventoryBalance.product_id == d.id)
            )
        ).scalar_one_or_none()
        assert dbal is None


# ── Aborto por fingerprint (estado cambió entre plan y apply) ────────────────────


async def test_apply_fingerprint_abort_skips_group(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    canonical = await _add_product(
        db_session, tid, name="Pan", sku="PAN1", barcode="7790044440004", stock_units=10
    )
    dup = await _add_product(db_session, tid, name="Pan x", sku="PAN1", stock_units=7)
    await _add_movement(db_session, tid, canonical.id, 10, SOURCE_PURCHASE_IMPORT, "h1")
    await _add_movement(db_session, tid, dup.id, 7, SOURCE_RECEIPT, "h2")
    sale = await _add_sale(db_session, tid, dup.id)
    await db_session.flush()
    canonical_id, dup_id, sale_id = canonical.id, dup.id, sale.id

    source_run_id = await _plan_and_persist(db_session, tid)

    # Alguien tocó un producto del grupo entre el dry-run y el apply.
    from sqlalchemy import update

    await db_session.execute(
        update(Product).where(Product.id == canonical_id).values(stock_units=999)
    )
    await db_session.flush()

    result = await svc.apply_dedup_plan(db_session, tid, source_run_id, lease_id=None)
    assert result.status == "PARTIALLY_APPLIED"
    assert result.groups_applied == 0
    assert result.groups_skipped == 1
    assert result.group_results[0]["reason"] == "fingerprint_changed"

    db_session.expunge_all()
    # Negocio intacto para ese grupo: dup activo, venta NO re-apuntada.
    rd = await db_session.get(Product, dup_id)
    assert rd is not None and rd.is_active is True
    refreshed_sale = await db_session.get(SaleEntry, sale_id)
    assert refreshed_sale is not None and refreshed_sale.product_id == dup_id


# ── Aborto por rowcount de REPOINT_FK (guarda de segunda capa) ───────────────────


async def test_apply_repoint_count_mismatch_aborts_group(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    tid = sample_tenant.tenant_id
    canonical = await _add_product(
        db_session, tid, name="Leche", sku="LECHE", barcode="7790055550005", stock_units=0
    )
    dup = await _add_product(db_session, tid, name="Leche x", sku="LECHE", stock_units=5)
    await _add_movement(db_session, tid, dup.id, 5, SOURCE_PURCHASE_IMPORT, "hL")
    sale = await _add_sale(db_session, tid, dup.id)
    await db_session.flush()
    canonical_id, dup_id = canonical.id, dup.id

    source_run_id = await _plan_and_persist(db_session, tid)

    # Fijamos el fingerprint (bypass) para EXPONER la segunda capa: el rowcount de
    # REPOINT_FK. Sin este bypass, borrar la venta cambiaría el fingerprint (fk_count)
    # y el grupo se saltearía en la primera capa; acá probamos que el rowcount guard
    # aborta el grupo aunque el fingerprint pase.
    merge = (
        await db_session.execute(
            select(DataRepairItem).where(
                DataRepairItem.run_id == source_run_id,
                DataRepairItem.action == "MERGE_PRODUCT",
            )
        )
    ).scalar_one()
    planned_fp = merge.before_json["plan"]["fingerprint"]
    monkeypatch.setattr(svc, "compute_group_fingerprint", lambda *a, **k: planned_fp)

    # Borramos una fila de FK planificada → el UPDATE afectará menos de lo esperado.
    await db_session.delete(sale)
    await db_session.flush()

    result = await svc.apply_dedup_plan(db_session, tid, source_run_id, lease_id=None)
    assert result.status == "FAILED"
    assert result.groups_failed == 1
    assert result.groups_applied == 0
    assert "RepointCountMismatch" in result.group_results[0]["reason"]

    db_session.expunge_all()
    # Nada del grupo se aplicó (rollback): dup activo, canónico sin delta.
    rd = await db_session.get(Product, dup_id)
    assert rd is not None and rd.is_active is True
    rc = await db_session.get(Product, canonical_id)
    assert rc is not None and rc.stock_units == 0


# ── Guard de balance negativo (crear balance NUEVO con current_qty<0) ────────────


async def test_apply_skips_group_when_new_balance_would_be_negative(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    # Catálogo puro (MOST_RECENT): el canónico gana por barcode y arrastra stock_units=10,
    # pero el ancla MÁS RECIENTE del grupo es qty=3 → delta = 3 - 10 = -7 (< 0). El canónico
    # NO tiene balance (= 0 implícito) → crear uno con current_qty<0 sería inconsistente.
    canonical = await _add_product(
        db_session, tid, name="Sal", sku="SAL", barcode="7790099990009", stock_units=10
    )
    dup = await _add_product(db_session, tid, name="Sal x", sku="SAL", stock_units=3)
    await _add_movement(
        db_session, tid, canonical.id, 3, SOURCE_CATALOG_INITIAL_STOCK, "c", occurred_offset_days=1
    )
    await _add_movement(
        db_session, tid, dup.id, 3, SOURCE_CATALOG_INITIAL_STOCK, "d", occurred_offset_days=0
    )
    await _add_balance(db_session, tid, dup.id, 3)  # el canónico queda SIN balance a propósito
    await db_session.flush()
    canonical_id, dup_id = canonical.id, dup.id

    source_run_id = await _plan_and_persist(db_session, tid)
    result = await svc.apply_dedup_plan(db_session, tid, source_run_id, lease_id=None)
    assert result.status == "PARTIALLY_APPLIED"
    assert result.groups_applied == 0
    assert result.groups_skipped == 1
    assert result.group_results[0]["reason"] == "balance_inconsistency"

    db_session.expunge_all()
    # Rollback total del grupo: dup activo, canónico sin delta y sin balance nuevo negativo.
    rd = await db_session.get(Product, dup_id)
    assert rd is not None and rd.is_active is True
    rc = await db_session.get(Product, canonical_id)
    assert rc is not None and rc.stock_units == 10
    canon_bal = (
        await db_session.execute(
            select(InventoryBalance).where(InventoryBalance.product_id == canonical_id)
        )
    ).scalar_one_or_none()
    assert canon_bal is None


# ── MERGE_PRODUCT: solo completa NULLs, custom_fields shallow sin "_"-claves ─────


async def test_apply_merge_completes_only_nulls_and_filters_internal_custom_fields(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    canonical = await _add_product(
        db_session,
        tid,
        name="Fideos",
        sku="FIDEO",
        barcode="7790066660006",
        stock_units=0,
        unit_cost=None,
        category=None,
        custom_fields={"color": "rojo"},
        sale_price=Decimal("123.00"),
    )
    await _add_product(
        db_session,
        tid,
        name="Fideos x",
        sku="FIDEO",
        stock_units=0,
        unit_cost=Decimal("50"),
        category="almacen",
        custom_fields={"marca": "Matarazzo", "_sentinel": "true", "color": "azul"},
    )
    await db_session.flush()

    source_run_id = await _plan_and_persist(db_session, tid)
    result = await svc.apply_dedup_plan(db_session, tid, source_run_id, lease_id=None)
    assert result.status == "APPLIED"

    db_session.expunge_all()
    rc = await db_session.get(Product, canonical.id)
    assert rc is not None
    assert rc.unit_cost_ars == Decimal("50")  # completado (estaba NULL)
    assert rc.category == "almacen"  # completado (estaba NULL)
    assert rc.sale_price_ars == Decimal("123.00")  # NO se pisa (no es completable)
    assert rc.custom_fields["marca"] == "Matarazzo"  # agregado del dup
    assert rc.custom_fields["color"] == "rojo"  # canónico gana (no "azul")
    assert "_sentinel" not in rc.custom_fields  # clave interna excluida


# ── Idempotencia / validación del source run ─────────────────────────────────────


async def test_reapply_consumed_source_raises(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    canonical = await _add_product(
        db_session, tid, name="Café", sku="CAFE", barcode="7790077770007", stock_units=0
    )
    await _add_product(db_session, tid, name="Café x", sku="CAFE", stock_units=0)
    await db_session.flush()

    source_run_id = await _plan_and_persist(db_session, tid)
    first = await svc.apply_dedup_plan(db_session, tid, source_run_id, lease_id=None)
    assert first.status == "APPLIED"

    # Re-apply del MISMO source (ya consumido → status APPLIED) aborta en validación.
    with pytest.raises(ValueError, match="status"):
        await svc.apply_dedup_plan(db_session, tid, source_run_id, lease_id=None)
    _ = canonical


async def test_partial_apply_leaves_source_reappliable(
    db_session: AsyncSession, isolated_db_engine: AsyncEngine, sample_tenant: Tenant
) -> None:
    """Un run que termina PARTIALLY_APPLIED (un grupo salteado por fingerprint) deja el
    source dry-run en COMPLETED → re-aplicable. Un 2º apply (con el estado ya estable)
    toma el grupo que había quedado pendiente; el grupo ya aplicado cae solo por skip.
    El 2º apply corre en una session NUEVA (espeja producción: otra corrida del script)."""
    tid = sample_tenant.tenant_id
    # Grupo A (SUM, delta=7).
    canon_a = await _add_product(
        db_session, tid, name="Coca", sku="COCA", barcode="7790011110001", stock_units=10
    )
    dup_a = await _add_product(db_session, tid, name="Coca x", sku="COCA", stock_units=7)
    await _add_movement(db_session, tid, canon_a.id, 10, SOURCE_PURCHASE_IMPORT, "a1")
    await _add_movement(db_session, tid, dup_a.id, 7, SOURCE_RECEIPT, "a2")
    await _add_balance(db_session, tid, canon_a.id, 10)
    await _add_balance(db_session, tid, dup_a.id, 7)
    await _add_sale(db_session, tid, dup_a.id)
    # Grupo B (SUM, delta=5) — independiente.
    await _add_product(
        db_session, tid, name="Agua", sku="AGUA", barcode="7790022220002", stock_units=0
    )
    dup_b = await _add_product(db_session, tid, name="Agua x", sku="AGUA", stock_units=5)
    await _add_movement(db_session, tid, dup_b.id, 5, SOURCE_PURCHASE_IMPORT, "b1")
    await _add_balance(db_session, tid, dup_b.id, 5)
    await _add_sale(db_session, tid, dup_b.id)
    await db_session.flush()
    canon_a_id, dup_a_id, dup_b_id = canon_a.id, dup_a.id, dup_b.id

    source_run_id = await _plan_and_persist(db_session, tid)

    from sqlalchemy import update

    # Alguien tocó el grupo A entre el dry-run y el 1er apply → A se saltea por fingerprint.
    await db_session.execute(
        update(Product).where(Product.id == canon_a_id).values(stock_units=999)
    )
    await db_session.commit()

    first = await svc.apply_dedup_plan(db_session, tid, source_run_id, lease_id=None)
    assert first.status == "PARTIALLY_APPLIED"
    assert first.groups_applied == 1  # B aplicado
    assert first.groups_skipped == 1  # A salteado (fingerprint)

    # El source NO se marca APPLIED (no terminó full-APPLIED) → queda re-aplicable.
    src = await db_session.get(DataRepairRun, source_run_id)
    assert src is not None and src.status == "COMPLETED"

    # 2º apply del MISMO source en una session NUEVA (otra corrida del script): estabilizamos
    # A (vuelve a su estado del plan → su fingerprint matchea) y re-aplicamos. A ahora aplica;
    # B cae solo (ya aplicado en el 1º → su fingerprint cambió).
    async with AsyncSession(isolated_db_engine, expire_on_commit=False) as s2:
        await s2.execute(
            update(Product).where(Product.id == canon_a_id).values(stock_units=10)
        )
        await s2.commit()
        second = await svc.apply_dedup_plan(s2, tid, source_run_id, lease_id=None)
        assert second.status == "PARTIALLY_APPLIED"
        assert second.groups_applied == 1  # A
        assert second.groups_skipped == 1  # B (ya aplicado antes)

        rd_a = await s2.get(Product, dup_a_id)
        assert rd_a is not None and rd_a.is_active is False  # A se aplicó en el 2º apply
        rd_b = await s2.get(Product, dup_b_id)
        assert rd_b is not None and rd_b.is_active is False  # B se aplicó en el 1º apply


async def test_apply_nonexistent_source_raises(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    with pytest.raises(ValueError, match="no existe"):
        await svc.apply_dedup_plan(
            db_session, sample_tenant.tenant_id, uuid.uuid4(), lease_id=None
        )


# ── Guarda del lease (heartbeat pierde el lease → run FAILED, nada aplicado) ──────


async def test_apply_lost_lease_fails_without_mutating(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    canonical = await _add_product(
        db_session, tid, name="Té", sku="TE1", barcode="7790088880008", stock_units=0
    )
    dup = await _add_product(db_session, tid, name="Té x", sku="TE1", stock_units=0)
    await db_session.flush()
    dup_id = dup.id
    _ = canonical

    source_run_id = await _plan_and_persist(db_session, tid)
    # lease_id sin fila de lock viva → renew() falla en el heartbeat → run FAILED.
    result = await svc.apply_dedup_plan(db_session, tid, source_run_id, lease_id=uuid.uuid4())
    assert result.status == "FAILED"
    assert result.groups_applied == 0
    assert result.group_results[0]["reason"] == "lease_lost"

    db_session.expunge_all()
    rd = await db_session.get(Product, dup_id)
    assert rd is not None and rd.is_active is True


# ── Guard de CLI: --apply exige --source-run-id ──────────────────────────────────


def test_cli_apply_requires_source_run_id() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/dedupe_products_by_name.py",
            "--apply",
            "--tenant",
            str(uuid.uuid4()),
        ],
        cwd=_BACKEND_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 2
    assert "source-run-id" in proc.stdout
