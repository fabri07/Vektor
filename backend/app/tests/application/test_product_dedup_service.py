"""Tests de product_dedup_service (F3-T4) — detección + planificación dry-run.

Cubre las funciones PURAS (aristas/componentes/canónico/conflicto/stock/fingerprint)
sin DB, y el flujo dry-run (persiste plan, NO muta negocio) contra SQLite.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import product_dedup_service as svc
from app.application.services.ingestion_import_service import ProductIdentityIndexes
from app.application.services.inventory_movement_origin import (
    SOURCE_CATALOG_INITIAL_STOCK,
    SOURCE_PURCHASE_IMPORT,
    SOURCE_RECEIPT,
)
from app.application.services.product_dedup_service import (
    DECISION_VERSION,
    EDGE_MEDIUM,
    EDGE_STRONG,
    EDGE_WEAK,
    GROUP_MERGE,
    GROUP_WEAK_REVIEW,
    STOCK_KEEP_ONE,
    STOCK_MOST_RECENT,
    STOCK_REVIEW,
    STOCK_SUM,
    Edge,
    MovementRow,
    ProductRecord,
    StockDecision,
)
from app.persistence.models.inventory import InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairItem, DataRepairRun
from app.persistence.models.tenant import Tenant

_BASE = datetime(2026, 1, 1, tzinfo=UTC)


# ── Helpers puros ────────────────────────────────────────────────────────────────


def _id(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


def _indexes(
    *,
    by_barcode: dict[str, list[uuid.UUID]] | None = None,
    by_sku: dict[str, list[uuid.UUID]] | None = None,
    by_name_brand: dict[tuple[str, str | None], list[uuid.UUID]] | None = None,
) -> ProductIdentityIndexes:
    return ProductIdentityIndexes(
        by_sku=by_sku or {},
        by_barcode=by_barcode or {},
        by_name_brand=by_name_brand or {},
        by_name={},
        by_id={},
    )


def _record(
    n: int,
    *,
    has_user_edits: bool = False,
    barcode: str | None = None,
    sku: str | None = None,
    created_offset_days: int = 0,
    unit_cost: Decimal | None = None,
    stock_units: int = 0,
    fk_count: int = 0,
    current_qty: int | None = None,
    reserved_qty: int | None = None,
    movements: tuple[MovementRow, ...] = (),
) -> ProductRecord:
    return ProductRecord(
        id=_id(n),
        has_user_edits=has_user_edits,
        barcode_normalized=barcode,
        sku_normalized=sku,
        created_at=_BASE + timedelta(days=created_offset_days),
        unit_cost_ars=unit_cost,
        stock_units=stock_units,
        fk_count=fk_count,
        current_qty=current_qty,
        reserved_qty=reserved_qty,
        movements=movements,
    )


def _mov(
    qty: int,
    *,
    source_type: str | None,
    movement_type: str = "purchase",
    source_row_hash: str | None = None,
    occurred_offset_days: int | None = None,
) -> MovementRow:
    occ = _BASE + timedelta(days=occurred_offset_days) if occurred_offset_days is not None else None
    return MovementRow(
        qty=qty,
        movement_type=movement_type,
        source_type=source_type,
        source_row_hash=source_row_hash,
        occurred_at=occ,
        created_at=_BASE,
    )


# ── Aristas ──────────────────────────────────────────────────────────────────────


def test_build_edges_classifies_strong_medium_weak() -> None:
    edges = svc.build_edges(
        _indexes(
            by_barcode={"7790001": [_id(1), _id(2)]},
            by_sku={"sku-a": [_id(3), _id(4)]},
            by_name_brand={("coca cola", "coca"): [_id(5), _id(6)]},
        )
    )
    kinds = {e.kind for e in edges}
    assert kinds == {EDGE_STRONG, EDGE_MEDIUM, EDGE_WEAK}
    assert all(e.from_id != e.to_id for e in edges)


def test_build_edges_ignores_singletons() -> None:
    edges = svc.build_edges(_indexes(by_barcode={"7790001": [_id(1)]}))
    assert edges == []


# ── Componentes: solo fuerte+medio ───────────────────────────────────────────────


def test_transitive_name_sku_chain_does_not_merge_a_and_c() -> None:
    # A—nombre—B  y  B—sku—C : la arista de nombre NO conecta ⇒ merge = {B, C}.
    edges = [
        Edge(_id(1), _id(2), EDGE_WEAK, "name+brand:x|y"),
        Edge(_id(2), _id(3), EDGE_MEDIUM, "sku:s"),
    ]
    groups = svc.build_groups(edges)
    merge = [g for g in groups if g.kind == GROUP_MERGE]
    assert len(merge) == 1
    assert set(merge[0].member_ids) == {_id(2), _id(3)}
    assert _id(1) not in merge[0].member_ids


def test_strong_medium_form_single_component() -> None:
    edges = [
        Edge(_id(1), _id(2), EDGE_STRONG, "barcode:b"),
        Edge(_id(2), _id(3), EDGE_MEDIUM, "sku:s"),
    ]
    groups = svc.build_groups(edges)
    merge = [g for g in groups if g.kind == GROUP_MERGE]
    assert len(merge) == 1
    assert set(merge[0].member_ids) == {_id(1), _id(2), _id(3)}


def test_weak_only_collision_is_review_group_not_merge() -> None:
    edges = [Edge(_id(1), _id(2), EDGE_WEAK, "name+brand:x|y")]
    groups = svc.build_groups(edges)
    assert len(groups) == 1
    assert groups[0].kind == GROUP_WEAK_REVIEW
    assert groups[0].requires_review is True
    assert set(groups[0].member_ids) == {_id(1), _id(2)}


def test_weak_edge_internal_to_merge_group_does_not_spawn_review() -> None:
    # A y B ya unidos por barcode; la arista débil entre ellos es redundante.
    edges = [
        Edge(_id(1), _id(2), EDGE_STRONG, "barcode:b"),
        Edge(_id(1), _id(2), EDGE_WEAK, "name+brand:x|y"),
    ]
    groups = svc.build_groups(edges)
    assert len(groups) == 1
    assert groups[0].kind == GROUP_MERGE


# ── Canónico ─────────────────────────────────────────────────────────────────────


def test_choose_canonical_user_edits_wins_first() -> None:
    a = _record(1, has_user_edits=True, created_offset_days=10)
    b = _record(2, barcode="7790001", sku="s", fk_count=99, created_offset_days=0)
    assert svc.choose_canonical([a, b]) == a.id


def test_choose_canonical_barcode_then_sku_then_fks() -> None:
    # sin user edits: barcode NOT NULL gana.
    a = _record(1, barcode="7790001")
    b = _record(2, sku="s", fk_count=99)
    assert svc.choose_canonical([a, b]) == a.id
    # empatan en barcode(None)/sku(None): #FKs desempata.
    c = _record(3, fk_count=5)
    d = _record(4, fk_count=50)
    assert svc.choose_canonical([c, d]) == d.id


def test_choose_canonical_created_then_id_tiebreak() -> None:
    a = _record(2, created_offset_days=5)
    b = _record(3, created_offset_days=1)
    assert svc.choose_canonical([a, b]) == b.id  # created ASC
    c = _record(9, created_offset_days=0)
    d = _record(4, created_offset_days=0)
    assert svc.choose_canonical([c, d]) == d.id  # id ASC (int=4 < int=9)


# ── Conflicto de identidad ───────────────────────────────────────────────────────


def test_identity_two_barcodes_is_review() -> None:
    recs = [_record(1, barcode="7790001"), _record(2, barcode="7790002")]
    review, reasons = svc.classify_identity_conflict(recs)
    assert review is True
    assert svc.REVIEW_BARCODE_DIVERGENCE in reasons


def test_identity_two_skus_is_review() -> None:
    recs = [_record(1, sku="sku-a"), _record(2, sku="sku-b")]
    review, reasons = svc.classify_identity_conflict(recs)
    assert review is True
    assert svc.REVIEW_SKU_DIVERGENCE in reasons


def test_identity_two_skus_bridged_by_same_barcode_is_not_review() -> None:
    # Mismo barcode físico compartido, distintas etiquetas de SKU → completable.
    recs = [
        _record(1, barcode="7790001", sku="sku-a"),
        _record(2, barcode="7790001", sku="sku-b"),
    ]
    review, reasons = svc.classify_identity_conflict(recs)
    assert review is False
    assert reasons == []


def test_identity_cost_null_vs_value_is_completable() -> None:
    recs = [_record(1, unit_cost=None), _record(2, unit_cost=Decimal("100.00"))]
    review, reasons = svc.classify_identity_conflict(recs)
    assert review is False


def test_identity_cost_value_vs_value_is_review() -> None:
    recs = [_record(1, unit_cost=Decimal("100.00")), _record(2, unit_cost=Decimal("120.00"))]
    review, reasons = svc.classify_identity_conflict(recs)
    assert review is True
    assert svc.REVIEW_COST_DIVERGENCE in reasons


# ── Decisión de stock ────────────────────────────────────────────────────────────


def test_stock_keep_one_shared_source_row_hash_does_not_sum() -> None:
    shared = (_mov(10, source_type=SOURCE_PURCHASE_IMPORT, source_row_hash="h1"),)
    canonical = _record(1, stock_units=10, movements=shared)
    dup = _record(2, stock_units=10, movements=shared)
    dec = svc.classify_stock_decision(dup, canonical)
    assert dec.kind == STOCK_KEEP_ONE
    assert dec.delta == 0


def test_stock_sum_distinct_purchases() -> None:
    canonical = _record(
        1,
        stock_units=10,
        movements=(_mov(10, source_type=SOURCE_PURCHASE_IMPORT, source_row_hash="h1"),),
    )
    dup = _record(
        2, stock_units=7, movements=(_mov(7, source_type=SOURCE_RECEIPT, source_row_hash="h2"),)
    )
    dec = svc.classify_stock_decision(dup, canonical)
    assert dec.kind == STOCK_SUM
    assert dec.delta == 7


def test_stock_most_recent_catalog_takes_latest_by_occurred_at() -> None:
    canonical = _record(
        1,
        stock_units=5,
        movements=(_mov(5, source_type=SOURCE_CATALOG_INITIAL_STOCK, occurred_offset_days=1),),
    )
    dup = _record(
        2,
        stock_units=8,
        movements=(_mov(8, source_type=SOURCE_CATALOG_INITIAL_STOCK, occurred_offset_days=10),),
    )
    dec = svc.classify_stock_decision(dup, canonical)
    assert dec.kind == STOCK_MOST_RECENT
    # más reciente = dup (día 10, qty 8); delta = 8 − canonical.stock_units(5) = 3
    assert dec.delta == 3


def test_stock_most_recent_can_be_negative_incremental() -> None:
    canonical = _record(
        1,
        stock_units=20,
        movements=(_mov(20, source_type=SOURCE_CATALOG_INITIAL_STOCK, occurred_offset_days=1),),
    )
    dup = _record(
        2,
        stock_units=8,
        movements=(_mov(8, source_type=SOURCE_CATALOG_INITIAL_STOCK, occurred_offset_days=10),),
    )
    dec = svc.classify_stock_decision(dup, canonical)
    assert dec.kind == STOCK_MOST_RECENT
    assert dec.delta == -12  # 8 − 20, incremental (puede ser negativo)


def test_stock_review_residuo_no_ledger() -> None:
    # stock_units=15 pero solo 10 identificables en el ledger → residuo 5 → review.
    dup = _record(
        2, stock_units=15, movements=(_mov(10, source_type=SOURCE_PURCHASE_IMPORT),)
    )
    canonical = _record(1, stock_units=0)
    dec = svc.classify_stock_decision(dup, canonical)
    assert dec.kind == STOCK_REVIEW
    assert dec.delta is None
    assert dec.reason == svc.STOCK_REASON_RESIDUO


def test_stock_review_reserved_qty_nonzero() -> None:
    dup = _record(2, stock_units=0, reserved_qty=3)
    canonical = _record(1, stock_units=0)
    dec = svc.classify_stock_decision(dup, canonical)
    assert dec.kind == STOCK_REVIEW
    assert dec.reason == svc.STOCK_REASON_RESERVED


def test_stock_review_mixed_provenance() -> None:
    dup = _record(
        2,
        stock_units=15,
        movements=(
            _mov(5, source_type=SOURCE_CATALOG_INITIAL_STOCK),
            _mov(10, source_type=SOURCE_PURCHASE_IMPORT),
        ),
    )
    canonical = _record(1, stock_units=0)
    dec = svc.classify_stock_decision(dup, canonical)
    assert dec.kind == STOCK_REVIEW
    assert dec.reason == svc.STOCK_REASON_MIXED


# ── Decisión de stock a NIVEL DE GRUPO (no pairwise) ─────────────────────────────


def test_group_stock_three_catalog_members_takes_latest_not_pairwise_sum() -> None:
    # C día1 qty5, D1 día10 qty8, D2 día5 qty6 (residuo 0 c/u). El verdadero saldo
    # catálogo más reciente del grupo es 8. La decisión pairwise sumaría deltas
    # (3 + 1) → 5+4 = 9 (sobre-conteo). La group-level da UN delta = 3 → total 8.
    canonical = _record(
        1,
        stock_units=5,
        movements=(_mov(5, source_type=SOURCE_CATALOG_INITIAL_STOCK, occurred_offset_days=1),),
    )
    d1 = _record(
        2,
        stock_units=8,
        movements=(_mov(8, source_type=SOURCE_CATALOG_INITIAL_STOCK, occurred_offset_days=10),),
    )
    d2 = _record(
        3,
        stock_units=6,
        movements=(_mov(6, source_type=SOURCE_CATALOG_INITIAL_STOCK, occurred_offset_days=5),),
    )
    dec = svc.classify_group_stock_decision(canonical, [d1, d2])
    assert dec.kind == STOCK_MOST_RECENT
    assert dec.delta == 3  # ancla más reciente (día10, qty8) − canonical(5) = 3


def test_group_stock_sum_shared_hash_between_duplicates_counts_once() -> None:
    # hS aparece en D1 y D2 pero no en el canónico → cuenta UNA sola vez (dedup global
    # del grupo). Pairwise contaría hS dos veces (9 + 11 = 20) → sobre-conteo.
    canonical = _record(1, stock_units=0)
    d1 = _record(
        2,
        stock_units=9,
        movements=(
            _mov(5, source_type=SOURCE_PURCHASE_IMPORT, source_row_hash="hA"),
            _mov(4, source_type=SOURCE_RECEIPT, source_row_hash="hS"),
        ),
    )
    d2 = _record(
        3,
        stock_units=11,
        movements=(
            _mov(4, source_type=SOURCE_RECEIPT, source_row_hash="hS"),
            _mov(7, source_type=SOURCE_PURCHASE_IMPORT, source_row_hash="hB"),
        ),
    )
    dec = svc.classify_group_stock_decision(canonical, [d1, d2])
    assert dec.kind == STOCK_SUM
    assert dec.delta == 16  # hA(5) + hS(4, una vez) + hB(7)


def test_group_stock_mixed_catalog_and_purchase_across_members_is_review() -> None:
    # Un miembro catálogo (MOST_RECENT) y otro compra (SUM) en el mismo grupo → ambiguo.
    canonical = _record(1, stock_units=0)
    d1 = _record(
        2, stock_units=5, movements=(_mov(5, source_type=SOURCE_CATALOG_INITIAL_STOCK),)
    )
    d2 = _record(
        3, stock_units=7, movements=(_mov(7, source_type=SOURCE_PURCHASE_IMPORT),)
    )
    dec = svc.classify_group_stock_decision(canonical, [d1, d2])
    assert dec.kind == STOCK_REVIEW
    assert dec.reason == svc.STOCK_REASON_MIXED


def test_group_stock_decision_and_fingerprint_stable_under_movement_reorder() -> None:
    # Dos anclas con el MISMO timestamp; el desempate por qty hace el delta (y el
    # fingerprint) independiente del orden de la lista de movimientos (MINOR 2).
    canonical = _record(
        1,
        stock_units=5,
        movements=(
            _mov(5, source_type=SOURCE_CATALOG_INITIAL_STOCK, occurred_offset_days=1,
                 source_row_hash="c"),
        ),
    )
    m_a = _mov(8, source_type=SOURCE_CATALOG_INITIAL_STOCK, occurred_offset_days=10,
               source_row_hash="a")
    m_b = _mov(3, source_type=SOURCE_CATALOG_INITIAL_STOCK, occurred_offset_days=10,
               source_row_hash="b")
    dup = _record(2, stock_units=11, movements=(m_a, m_b))
    dup_rev = _record(2, stock_units=11, movements=(m_b, m_a))

    dec = svc.classify_group_stock_decision(canonical, [dup])
    dec_rev = svc.classify_group_stock_decision(canonical, [dup_rev])
    assert dec == dec_rev
    assert dec.kind == STOCK_MOST_RECENT
    assert dec.delta == 3  # ancla más reciente día10, mayor qty(8) − canonical(5)

    fp = svc.compute_group_fingerprint([canonical, dup], _id(1), dec)
    fp_rev = svc.compute_group_fingerprint([canonical, dup_rev], _id(1), dec_rev)
    assert fp == fp_rev


# ── Conflicto de identidad — excepción SKU-bridge "todos" (MINOR 3) ──────────────


def test_identity_sku_divergence_not_bridged_when_a_member_lacks_barcode() -> None:
    # A(sku=a, barcode=B1), B(sku=b, SIN barcode): B no está puenteado → review.
    recs = [
        _record(1, barcode="7790001", sku="sku-a"),
        _record(2, sku="sku-b"),
    ]
    review, reasons = svc.classify_identity_conflict(recs)
    assert review is True
    assert svc.REVIEW_SKU_DIVERGENCE in reasons


# ── Doble pertenencia a grupo (MINOR 4) ──────────────────────────────────────────


def test_weak_edge_touching_merge_member_is_not_double_counted() -> None:
    # A y B unidos por barcode (merge). A—nombre—X (débil) hacia afuera: A ya está en
    # un grupo de merge → NO debe formar además un WEAK_REVIEW (doble conteo).
    edges = [
        Edge(_id(1), _id(2), EDGE_STRONG, "barcode:b"),
        Edge(_id(1), _id(3), EDGE_WEAK, "name+brand:x|y"),
    ]
    groups = svc.build_groups(edges)
    assert len(groups) == 1
    assert groups[0].kind == GROUP_MERGE
    assert set(groups[0].member_ids) == {_id(1), _id(2)}
    plan = svc.DedupPlan(groups=groups, records={})
    assert plan.coverage()["products_in_groups"] == 2  # X no infla el conteo


# ── Fingerprint ──────────────────────────────────────────────────────────────────


def test_fingerprint_is_deterministic_same_input_same_hash() -> None:
    recs = [_record(1, stock_units=10, fk_count=3), _record(2, stock_units=5)]
    decision = StockDecision(STOCK_SUM, 5, "x")
    h1 = svc.compute_group_fingerprint(recs, _id(1), decision)
    h2 = svc.compute_group_fingerprint(list(reversed(recs)), _id(1), decision)
    assert h1 == h2  # orden de records no importa (se ordena internamente)


def test_fingerprint_changes_when_input_changes() -> None:
    recs = [_record(1, stock_units=10), _record(2, stock_units=5)]
    decision = StockDecision(STOCK_SUM, 5, "x")
    base = svc.compute_group_fingerprint(recs, _id(1), decision)

    recs2 = [_record(1, stock_units=11), _record(2, stock_units=5)]  # stock cambió
    assert svc.compute_group_fingerprint(recs2, _id(1), decision) != base

    decision2 = StockDecision(STOCK_SUM, 6, "x")  # delta cambió
    assert svc.compute_group_fingerprint(recs, _id(1), decision2) != base

    assert svc.compute_group_fingerprint(recs, _id(1), decision, decision_version=2) != base


# ── Dry-run (SQLite) ─────────────────────────────────────────────────────────────


async def _add_product(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    name: str,
    sku: str | None = None,
    barcode: str | None = None,
    stock_units: int = 0,
    unit_cost: Decimal | None = None,
) -> Product:
    p = Product(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        name=name,
        sku=sku,
        barcode=barcode,
        sale_price_ars=Decimal("100.00"),
        unit_cost_ars=unit_cost,
        stock_units=stock_units,
        is_active=True,
    )
    session.add(p)
    await session.flush()
    return p


async def test_plan_dedup_detects_sku_group_and_persists_plan(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    canonical = await _add_product(
        db_session, tid, name="Coca 500", sku="COCA500", stock_units=10, unit_cost=Decimal("50")
    )
    dup = await _add_product(
        db_session, tid, name="Coca 500ml", sku="COCA500", stock_units=0
    )
    # movimiento vivo del canónico (para que residuo del dup=0 y no sea review).
    db_session.add(
        InventoryMovement(
            id=uuid.uuid4(),
            tenant_id=tid,
            product_id=canonical.id,
            movement_type="purchase",
            qty=10,
            source_type=SOURCE_PURCHASE_IMPORT,
            source_row_hash="hh",
        )
    )
    await db_session.flush()

    plan = await svc.plan_dedup(db_session, tid)
    assert len(plan.merge_groups) == 1
    group = plan.merge_groups[0]
    assert group.canonical_id == canonical.id
    assert dup.id != group.canonical_id
    assert group.stock_decision is not None  # UNA decisión por grupo
    assert group.fingerprint is not None

    run_id = await svc.persist_dedup_plan(db_session, tid, plan)

    run = await db_session.get(DataRepairRun, run_id)
    assert run is not None
    assert run.repair_type == "PRODUCT_DEDUP"
    assert run.dry_run is True
    assert run.status == "COMPLETED"

    items = (
        await db_session.execute(select(DataRepairItem).where(DataRepairItem.run_id == run_id))
    ).scalars().all()
    actions = {i.action for i in items}
    assert "MERGE_PRODUCT" in actions
    assert "DEACTIVATE_DUPLICATE" in actions
    # el fingerprint viaja en el bloque plan de cada item.
    merge_item = next(i for i in items if i.action == "MERGE_PRODUCT")
    assert merge_item.before_json is not None
    assert merge_item.before_json["plan"]["fingerprint"] == group.fingerprint
    assert merge_item.before_json["plan"]["decision_version"] == DECISION_VERSION


async def test_dry_run_does_not_mutate_business_tables(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    canonical = await _add_product(
        db_session, tid, name="Agua", sku="AGUA1", stock_units=10
    )
    dup = await _add_product(db_session, tid, name="Agua", sku="AGUA1", stock_units=0)
    db_session.add(
        InventoryMovement(
            id=uuid.uuid4(),
            tenant_id=tid,
            product_id=canonical.id,
            movement_type="purchase",
            qty=10,
            source_type=SOURCE_PURCHASE_IMPORT,
        )
    )
    await db_session.flush()

    before_active = (
        await db_session.execute(
            select(func.count()).select_from(Product).where(
                Product.tenant_id == tid, Product.is_active.is_(True)
            )
        )
    ).scalar_one()

    plan = await svc.plan_dedup(db_session, tid)
    await svc.persist_dedup_plan(db_session, tid, plan)

    # Ningún producto se desactivó ni cambió stock.
    after_active = (
        await db_session.execute(
            select(func.count()).select_from(Product).where(
                Product.tenant_id == tid, Product.is_active.is_(True)
            )
        )
    ).scalar_one()
    assert after_active == before_active
    refreshed_dup = await db_session.get(Product, dup.id)
    assert refreshed_dup is not None
    assert refreshed_dup.is_active is True
    assert refreshed_dup.stock_units == 0
    refreshed_canon = await db_session.get(Product, canonical.id)
    assert refreshed_canon is not None
    assert refreshed_canon.stock_units == 10


async def test_review_group_generates_no_items(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    # Dos barcodes distintos que comparten SKU → grupo merge pero review por identidad.
    await _add_product(db_session, tid, name="X", sku="SKUX", barcode="77900010001", stock_units=0)
    await _add_product(db_session, tid, name="X2", sku="SKUX", barcode="77900010002", stock_units=0)
    await db_session.flush()

    plan = await svc.plan_dedup(db_session, tid)
    assert len(plan.merge_groups) == 1
    assert plan.merge_groups[0].requires_review is True
    assert plan.mergeable_groups == []

    run_id = await svc.persist_dedup_plan(db_session, tid, plan)
    items = (
        await db_session.execute(select(DataRepairItem).where(DataRepairItem.run_id == run_id))
    ).scalars().all()
    assert items == []  # grupos review NO generan items
    run = await db_session.get(DataRepairRun, run_id)
    assert run is not None
    assert run.details_json is not None
    assert len(run.details_json["review_groups"]) == 1


async def test_missing_identity_precondition_count(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    assert await svc.count_active_products_missing_identity(db_session, tid) == 0
    # Forzar name_normalized NULL (el listener lo setea; lo pisamos con UPDATE).
    p = await _add_product(db_session, tid, name="Sin norm", stock_units=0)
    from sqlalchemy import update

    await db_session.execute(
        update(Product).where(Product.id == p.id).values(name_normalized=None)
    )
    await db_session.flush()
    assert await svc.count_active_products_missing_identity(db_session, tid) == 1
