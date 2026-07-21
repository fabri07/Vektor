"""Tests de product_dedup_service (F3-T6) — reversa ``--revert-run`` de un run de APPLY.

Revierte SIEMPRE un run de APPLY (``dry_run=False``, ``source_run_id`` seteado), nunca
el source dry-run. Aplica la inversa EXACTA e incremental de cada mutación (resta el
delta group-level de stock/current_qty, re-inserta el balance del dup, mueve las FKs
registradas de vuelta al dup, reactiva el dup, revierte los campos completados y quita
las custom_fields agregadas), validando ANTES de tocar cada registro que su estado
ACTUAL coincide con el ``after_json`` que el apply dejó. Divergencia → aborta ESE grupo
(el resto del run se revierte igual). Excepción de tolerancia: reactivar un dup ya
activo es un no-op idempotente (no aborta).

Cubre contra SQLite: round-trip SUM simple, round-trip catálogo MOST_RECENT, bloqueo por
campo fusionado modificado, bloqueo por FK nueva en el canónico, tolerancia de dup
reactivado a mano, reversa parcial (un grupo bloqueado, el resto se revierte),
validación del run (dry-run / ya REVERTED / inexistente) y auditoría.
"""

from __future__ import annotations

import subprocess
import sys
import uuid
from collections.abc import AsyncGenerator
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
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.inventory import InventoryBalance, InventoryMovement
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairItem, DataRepairRun
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry

_BASE = datetime(2026, 1, 1, tzinfo=UTC)
_BACKEND_ROOT = Path(__file__).resolve().parents[3]


@pytest_asyncio.fixture
async def db_session(isolated_db_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    """Shadow del ``db_session`` de conftest con una session de COMMIT REAL sobre un
    engine aislado — mismo motivo que en el test de apply: el revert commitea por grupo
    (transacción propia) → releasea la barrera exclusive por grupo. Espeja producción."""
    async with AsyncSession(isolated_db_engine, expire_on_commit=False) as session:
        yield session


# ── Helpers de siembra (espejo de test_product_dedup_apply) ──────────────────────


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


async def _plan_apply(
    session: AsyncSession, tenant_id: uuid.UUID
) -> tuple[svc.ApplyResult, list[uuid.UUID]]:
    """Plan + apply, devolviendo además los canónicos REALES que eligió el plan.

    Los ids salen de ``DedupGroup.canonical_id`` (``choose_canonical``), no de una
    variable del test: cuál producto queda canónico lo decide el planner, y hay que
    capturarlo ANTES del apply (después los grupos ya están fusionados y
    ``plan_dedup`` no los vuelve a detectar).
    """
    plan = await svc.plan_dedup(session, tenant_id)
    canonicals = [g.canonical_id for g in plan.groups if g.is_mergeable and g.canonical_id]
    source_run_id = await svc.persist_dedup_plan(session, tenant_id, plan)
    applied = await svc.apply_dedup_plan(session, tenant_id, source_run_id, lease_id=None)
    return applied, canonicals


async def _plan_and_apply(session: AsyncSession, tenant_id: uuid.UUID) -> svc.ApplyResult:
    applied, _ = await _plan_apply(session, tenant_id)
    return applied


async def _free_identities(session: AsyncSession, product_ids: list[uuid.UUID]) -> None:
    """Libera la clave fuerte de los productos indicados (los canónicos del plan).

    Desde F5 el revert rebota con ``_REVERT_IDENTITY_COLLISION`` cuando el dup
    comparta ``barcode``/``sku`` con un producto que va a seguir ACTIVO después de la
    reversa: el canónico que YA tenía esa clave antes del merge, un tercer producto, u
    otro dup del mismo grupo. NO rebota por la clave que el canónico recibió DEL dup
    durante el merge (``fields_completed``): esa colisión es transitoria, porque
    MERGE⁻¹ la nulea.

    Eso NO vuelve automática la reversa. Todo grupo mergeable comparte una clave que el
    canónico ya tenía pre-merge (es la arista que lo formó), así que siempre hace falta
    que un humano la libere primero — que es lo que este helper simula. Lo único que la
    tolerancia transitoria evita es exigirle al humano que borre ADEMÁS la clave del
    duplicado. Ambas variantes tienen sus propios tests más abajo.

    Muta por ORM a propósito: dispara ``before_update``, que es la fuente única de
    recálculo de las columnas ``*_normalized`` —las que consulta el guard—. Con
    ``update()`` Core los listeners NO corren y habría que nulear las cuatro columnas
    a mano, duplicando la lógica de normalización en el test.
    """
    for pid in product_ids:
        product = await session.get(Product, pid)
        if product is None:  # pragma: no cover — defensivo
            continue
        product.sku = None
        product.barcode = None
    # COMMIT, no flush: ``revert_dedup_run`` hace ``session.rollback()`` cuando saltea
    # un grupo (``product_dedup_service.py:2196``), y ese rollback descartaría un
    # cambio sin commitear —restaurando la clave justo antes de procesar los grupos
    # siguientes—. Commitear también espeja la realidad: la resolución humana de la
    # colisión es una transacción confirmada, no trabajo en vuelo.
    await session.commit()


# ── Round-trip SUM simple ────────────────────────────────────────────────────────


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_revert_simple_sum_roundtrip(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
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
    canonical_id, dup_id = canonical.id, dup.id
    sale_id, expense_id, dup_mov_id = sale.id, expense.id, dup_mov.id

    applied, canonicals = await _plan_apply(db_session, tid)
    assert applied.status == "APPLIED"
    apply_run_id = applied.run_id

    await _free_identities(db_session, canonicals)
    reverted = await svc.revert_dedup_run(db_session, tid, apply_run_id, lease_id=None)
    assert reverted.status == "REVERTED"
    assert reverted.groups_reverted == 1
    assert reverted.groups_skipped == 0
    assert reverted.groups_failed == 0

    db_session.expunge_all()

    # FKs de vuelta al duplicado.
    rs = await db_session.get(SaleEntry, sale_id)
    assert rs is not None and rs.product_id == dup_id
    re = await db_session.get(ExpenseEntry, expense_id)
    assert re is not None and re.product_id == dup_id
    rm = await db_session.get(InventoryMovement, dup_mov_id)
    assert rm is not None and rm.product_id == dup_id

    # Balance del dup re-insertado; canónico restado exacto.
    dup_bal = (
        await db_session.execute(
            select(InventoryBalance).where(InventoryBalance.product_id == dup_id)
        )
    ).scalar_one()
    assert dup_bal.current_qty == 7
    assert dup_bal.reserved_qty == 0
    canon_bal = (
        await db_session.execute(
            select(InventoryBalance).where(InventoryBalance.product_id == canonical_id)
        )
    ).scalar_one()
    assert canon_bal.current_qty == 10  # 17 − delta(7)

    rc = await db_session.get(Product, canonical_id)
    assert rc is not None
    assert rc.stock_units == 10  # 17 − delta(7)
    assert rc.unit_cost_ars is None  # el campo completado vuelve a NULL
    assert rc.is_active is True

    rd = await db_session.get(Product, dup_id)
    assert rd is not None
    assert rd.is_active is True  # reactivado
    assert rd.deactivated_at is None
    assert rd.deactivation_reason is None
    assert rd.stock_units == 7  # nunca se tocó

    run = await db_session.get(DataRepairRun, apply_run_id)
    assert run is not None and run.status == "REVERTED"

    # Auditoría de la reversa.
    audits = (
        await db_session.execute(
            select(DecisionAuditLog).where(DecisionAuditLog.decision_type == "PRODUCT_DEDUP")
        )
    ).scalars().all()
    assert any((a.decision_data or {}).get("action") == "revert" for a in audits)


# ── Round-trip catálogo MOST_RECENT (3 miembros) ─────────────────────────────────


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_revert_catalog_most_recent_roundtrip(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    canonical = await _add_product(
        db_session, tid, name="Yerba", sku="YERBA", barcode="7790033330003", stock_units=5
    )
    # d1 entra al grupo por el BARCODE y d2 por el SKU, para que los dos duplicados no
    # compartan clave ENTRE SÍ. Con ambos en sku="YERBA" (como estaba antes de F5-B),
    # revertirlos dejaría dos productos ACTIVOS con el mismo SKU: el guard intra-grupo
    # lo bloquea, y con razón — en Postgres eso es una violación de
    # ``uq_products_tenant_sku_norm``, no un round-trip válido. Este test cubre la
    # aritmética de stock del ancla MOST_RECENT, no la colisión; la colisión tiene su
    # propio test más abajo.
    d1 = await _add_product(
        db_session, tid, name="Yerba 1", barcode="7790033330003", stock_units=8
    )
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
    canonical_id, d1_id, d2_id = canonical.id, d1.id, d2.id

    applied, canonicals = await _plan_apply(db_session, tid)
    assert applied.status == "APPLIED"

    # Post-apply: canónico quedó en 8 (ancla más reciente).
    await _free_identities(db_session, canonicals)
    reverted = await svc.revert_dedup_run(db_session, tid, applied.run_id, lease_id=None)
    assert reverted.status == "REVERTED"
    assert reverted.groups_reverted == 1

    db_session.expunge_all()
    rc = await db_session.get(Product, canonical_id)
    assert rc is not None and rc.stock_units == 5  # 8 − delta(3)
    canon_bal = (
        await db_session.execute(
            select(InventoryBalance).where(InventoryBalance.product_id == canonical_id)
        )
    ).scalar_one()
    assert canon_bal.current_qty == 5  # 8 − delta(3)

    for did, qty in ((d1_id, 8), (d2_id, 6)):
        rd = await db_session.get(Product, did)
        assert rd is not None and rd.is_active is True
        dbal = (
            await db_session.execute(
                select(InventoryBalance).where(InventoryBalance.product_id == did)
            )
        ).scalar_one()
        assert dbal.current_qty == qty  # balance del dup re-insertado


# ── Bloqueo: campo fusionado modificado tras el apply ────────────────────────────


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_revert_blocked_by_modified_merged_field(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    canonical = await _add_product(
        db_session, tid, name="Pan", sku="PAN1", barcode="7790044440004", stock_units=10
    )
    dup = await _add_product(
        db_session, tid, name="Pan x", sku="PAN1", stock_units=7, unit_cost=Decimal("50")
    )
    await _add_movement(db_session, tid, canonical.id, 10, SOURCE_PURCHASE_IMPORT, "h1")
    await _add_movement(db_session, tid, dup.id, 7, SOURCE_RECEIPT, "h2")
    await _add_balance(db_session, tid, canonical.id, 10)
    await _add_balance(db_session, tid, dup.id, 7)
    sale = await _add_sale(db_session, tid, dup.id)
    await db_session.flush()
    canonical_id, dup_id, sale_id = canonical.id, dup.id, sale.id

    applied = await _plan_and_apply(db_session, tid)
    assert applied.status == "APPLIED"

    # Alguien editó el campo completado (unit_cost) después del apply.
    from sqlalchemy import update

    await db_session.execute(
        update(Product).where(Product.id == canonical_id).values(unit_cost_ars=Decimal("99"))
    )
    await db_session.commit()

    reverted = await svc.revert_dedup_run(db_session, tid, applied.run_id, lease_id=None)
    assert reverted.status == "PARTIALLY_REVERTED"
    assert reverted.groups_reverted == 0
    assert reverted.groups_skipped == 1

    db_session.expunge_all()
    # Negocio del grupo intacto: dup inactivo, venta NO movida de vuelta.
    rd = await db_session.get(Product, dup_id)
    assert rd is not None and rd.is_active is False
    rs = await db_session.get(SaleEntry, sale_id)
    assert rs is not None and rs.product_id == canonical_id
    run = await db_session.get(DataRepairRun, applied.run_id)
    assert run is not None and run.status != "REVERTED"
    assert (run.details_json or {}).get("revert", {}).get("skipped") == 1


# ── Bloqueo: FK nueva apuntando al canónico tras el apply ────────────────────────


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_revert_blocked_by_new_fk_on_canonical(
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
    canonical_id, dup_id = canonical.id, dup.id

    applied = await _plan_and_apply(db_session, tid)
    assert applied.status == "APPLIED"

    # FK NUEVA (un gasto) apuntando al canónico DESPUÉS del merge — no la creó el apply.
    # Un gasto no descuenta stock → aísla la detección por conteo de FKs.
    await _add_expense(db_session, tid, canonical_id)
    await db_session.commit()

    reverted = await svc.revert_dedup_run(db_session, tid, applied.run_id, lease_id=None)
    assert reverted.status == "PARTIALLY_REVERTED"
    assert reverted.groups_reverted == 0
    assert reverted.groups_skipped == 1
    assert reverted.group_results[0]["reason"] == svc._REVERT_FK_ACTIVITY

    db_session.expunge_all()
    rd = await db_session.get(Product, dup_id)
    assert rd is not None and rd.is_active is False  # sin revertir


# ── Tolerancia: dup reactivado a mano → reversa idempotente (no aborta) ───────────


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_revert_tolerates_manually_reactivated_dup(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    canonical = await _add_product(
        db_session, tid, name="Sal", sku="SAL1", barcode="7790055550005", stock_units=10
    )
    dup = await _add_product(db_session, tid, name="Sal x", sku="SAL1", stock_units=7)
    await _add_movement(db_session, tid, canonical.id, 10, SOURCE_PURCHASE_IMPORT, "s1")
    await _add_movement(db_session, tid, dup.id, 7, SOURCE_RECEIPT, "s2")
    await _add_balance(db_session, tid, canonical.id, 10)
    await _add_balance(db_session, tid, dup.id, 7)
    sale = await _add_sale(db_session, tid, dup.id)
    await db_session.flush()
    canonical_id, dup_id, sale_id = canonical.id, dup.id, sale.id

    applied = await _plan_and_apply(db_session, tid)
    assert applied.status == "APPLIED"

    # El usuario reactivó el duplicado a mano (ya activo). NO recrea su balance.
    from sqlalchemy import update

    await db_session.execute(
        update(Product)
        .where(Product.id == dup_id)
        .values(is_active=True, deactivated_at=None, deactivation_reason=None)
    )
    await db_session.commit()

    reverted = await svc.revert_dedup_run(db_session, tid, applied.run_id, lease_id=None)
    assert reverted.status == "REVERTED"  # reactivar un dup ya activo es no-op idempotente
    assert reverted.groups_reverted == 1

    db_session.expunge_all()
    rd = await db_session.get(Product, dup_id)
    assert rd is not None and rd.is_active is True
    rs = await db_session.get(SaleEntry, sale_id)
    assert rs is not None and rs.product_id == dup_id  # FK igual volvió al dup
    dup_bal = (
        await db_session.execute(
            select(InventoryBalance).where(InventoryBalance.product_id == dup_id)
        )
    ).scalar_one()
    assert dup_bal.current_qty == 7  # balance re-insertado igual
    rc = await db_session.get(Product, canonical_id)
    assert rc is not None and rc.stock_units == 10  # 17 − delta(7)


# ── Reversa parcial: un grupo bloqueado, el resto se revierte ─────────────────────


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_revert_partial_one_group_blocked_rest_reverts(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    # Grupo A (SUM, canónico completa unit_cost desde el dup).
    canon_a = await _add_product(
        db_session, tid, name="Coca", sku="COCA", barcode="7790011110001", stock_units=10
    )
    dup_a = await _add_product(
        db_session, tid, name="Coca x", sku="COCA", stock_units=7, unit_cost=Decimal("50")
    )
    await _add_movement(db_session, tid, canon_a.id, 10, SOURCE_PURCHASE_IMPORT, "a1")
    await _add_movement(db_session, tid, dup_a.id, 7, SOURCE_RECEIPT, "a2")
    await _add_balance(db_session, tid, canon_a.id, 10)
    await _add_balance(db_session, tid, dup_a.id, 7)
    await _add_sale(db_session, tid, dup_a.id)
    # Grupo B (SUM independiente).
    await _add_product(
        db_session, tid, name="Agua", sku="AGUA", barcode="7790022220002", stock_units=0
    )
    dup_b = await _add_product(db_session, tid, name="Agua x", sku="AGUA", stock_units=5)
    await _add_movement(db_session, tid, dup_b.id, 5, SOURCE_PURCHASE_IMPORT, "b1")
    await _add_balance(db_session, tid, dup_b.id, 5)
    await _add_sale(db_session, tid, dup_b.id)
    await db_session.flush()
    canon_a_id, dup_a_id, dup_b_id = canon_a.id, dup_a.id, dup_b.id

    applied, canonicals = await _plan_apply(db_session, tid)
    assert applied.status == "APPLIED"
    assert applied.groups_applied == 2

    # Bloqueamos SOLO el grupo A (editan el campo completado del canónico A).
    from sqlalchemy import update

    await db_session.execute(
        update(Product).where(Product.id == canon_a_id).values(unit_cost_ars=Decimal("77"))
    )
    await db_session.commit()

    # Ambos canónicos liberados (ver F5): B queda revertible; A sigue bloqueado por
    # el campo editado, que se chequea ANTES que la identidad.
    await _free_identities(db_session, canonicals)
    reverted = await svc.revert_dedup_run(db_session, tid, applied.run_id, lease_id=None)
    assert reverted.status == "PARTIALLY_REVERTED"
    assert reverted.groups_reverted == 1  # B
    assert reverted.groups_skipped == 1  # A

    db_session.expunge_all()
    rd_a = await db_session.get(Product, dup_a_id)
    assert rd_a is not None and rd_a.is_active is False  # A bloqueado (sigue inactivo)
    rd_b = await db_session.get(Product, dup_b_id)
    assert rd_b is not None and rd_b.is_active is True  # B revertido (reactivado)
    run = await db_session.get(DataRepairRun, applied.run_id)
    assert run is not None and run.status != "REVERTED"


# ── Validación del run: dry-run / ya REVERTED / inexistente ──────────────────────


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_revert_dry_run_source_raises(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    await _add_product(
        db_session, tid, name="Café", sku="CAFE", barcode="7790077770007", stock_units=0
    )
    await _add_product(db_session, tid, name="Café x", sku="CAFE", stock_units=0)
    await db_session.flush()

    plan = await svc.plan_dedup(db_session, tid)
    source_run_id = await svc.persist_dedup_plan(db_session, tid, plan)
    await db_session.commit()

    with pytest.raises(ValueError, match="dry-run"):
        await svc.revert_dedup_run(db_session, tid, source_run_id, lease_id=None)


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_revert_bloqueado_por_identidad_es_atomico(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """F5: si reactivar el dup colisionaría con el canónico, NO se toca nada.

    Este es el caso NORMAL desde F5, y el contraste con los round-trips de arriba es
    lo que importa documentar:

    - **Acá** (sin intervención humana): el dup comparte la clave fuerte con el
      canónico —por eso el dedup los fusionó—, así que el revert SIEMPRE rebota. La
      reversa automática del dedup dejó de existir con el índice único.
    - **Round-trips de arriba**: revierten sólo porque ``_free_identities`` simula que
      un humano resolvió antes la colisión liberando la clave. Es un escenario
      excepcional y asistido, no reversibilidad automática.

    El bloqueo se chequea en la fase de guards, antes de cualquier mutación, para que
    el revert sea atómico: si rebota, no quedó nada a medio revertir.
    """
    tid = sample_tenant.tenant_id
    canonical = await _add_product(
        db_session, tid, name="Yerba", sku="YERBA", barcode="7790033330003", stock_units=5
    )
    dup = await _add_product(
        db_session, tid, name="Yerba x", sku="YERBA", stock_units=8, unit_cost=Decimal("40")
    )
    await _add_movement(db_session, tid, canonical.id, 5, SOURCE_PURCHASE_IMPORT, "y1")
    await _add_movement(db_session, tid, dup.id, 8, SOURCE_RECEIPT, "y2")
    await _add_balance(db_session, tid, canonical.id, 5)
    await _add_balance(db_session, tid, dup.id, 8)
    sale = await _add_sale(db_session, tid, dup.id)
    await db_session.flush()
    canonical_id, dup_id, sale_id = canonical.id, dup.id, sale.id

    applied = await _plan_and_apply(db_session, tid)
    assert applied.status == "APPLIED"
    canon_stock_post_apply = (await db_session.get(Product, canonical_id)).stock_units  # type: ignore[union-attr]
    await db_session.commit()

    # Sin liberar la identidad del canónico: el revert tiene que rebotar.
    reverted = await svc.revert_dedup_run(db_session, tid, applied.run_id, lease_id=None)
    assert reverted.status == "PARTIALLY_REVERTED"
    assert reverted.groups_reverted == 0
    assert reverted.groups_skipped == 1
    assert reverted.group_results[0]["reason"] == svc._REVERT_IDENTITY_COLLISION

    db_session.expunge_all()
    # Atomicidad: nada reactivado, ningún balance restaurado, canónico intacto.
    rd = await db_session.get(Product, dup_id)
    assert rd is not None and rd.is_active is False
    assert rd.deactivation_reason == "DUPLICATE"
    rc = await db_session.get(Product, canonical_id)
    assert rc is not None and rc.stock_units == canon_stock_post_apply
    dup_balance = (
        await db_session.execute(
            select(InventoryBalance).where(
                InventoryBalance.tenant_id == tid, InventoryBalance.product_id == dup_id
            )
        )
    ).scalar_one_or_none()
    assert dup_balance is None  # el apply lo borró y el revert NO lo recreó
    rs = await db_session.get(SaleEntry, sale_id)
    assert rs is not None and rs.product_id == canonical_id  # venta sigue en el canónico
    run = await db_session.get(DataRepairRun, applied.run_id)
    assert run is not None and run.status != "REVERTED"


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_revert_already_reverted_raises(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    await _add_product(
        db_session, tid, name="Té", sku="TE1", barcode="7790088880008", stock_units=0
    )
    await _add_product(db_session, tid, name="Té x", sku="TE1", stock_units=0)
    await db_session.flush()

    applied, canonicals = await _plan_apply(db_session, tid)
    await _free_identities(db_session, canonicals)
    first = await svc.revert_dedup_run(db_session, tid, applied.run_id, lease_id=None)
    assert first.status == "REVERTED"

    with pytest.raises(ValueError, match="REVERTED"):
        await svc.revert_dedup_run(db_session, tid, applied.run_id, lease_id=None)


async def test_revert_nonexistent_run_raises(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    with pytest.raises(ValueError, match="no existe"):
        await svc.revert_dedup_run(
            db_session, sample_tenant.tenant_id, uuid.uuid4(), lease_id=None
        )


# ── Guard de CLI: --revert-run es mutuamente excluyente con --apply ──────────────


def test_cli_revert_run_excludes_apply() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/dedupe_products_by_name.py",
            "--revert-run",
            str(uuid.uuid4()),
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
    assert "revert-run" in proc.stdout.lower()


# ── F5-B: colisión de identidad — persistente vs TRANSITORIA ─────────────────────
#
# Alcance real de estos tests, que conviene no exagerar: la reversa AUTOMÁTICA de un
# merge sigue bloqueada SIEMPRE. Todo grupo mergeable se forma por arista fuerte
# (barcode compartido) o media (sku compartido), y en las dos los DOS extremos poseen
# la clave — o sea que el canónico siempre la tenía ANTES del merge y siempre hay un
# dup que colisiona persistentemente con él. Eso es estructural y no lo cambia nada
# de acá.
#
# Lo que estos tests cubren es el camino ASISTIDO: después de que un humano liberó la
# clave COMPARTIDA del canónico, la reversa no debe además exigirle que borre la clave
# que el merge le COPIÓ desde el duplicado. Esa segunda clave la devuelve MERGE⁻¹ a
# NULL sola, así que su colisión es transitoria; pedirle al humano que la borre a mano
# es pedirle que destruya el dato que la reversa iba a restaurar.


async def _seed_transient_group(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    canonical_barcode: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Canónico SIN barcode + dup CON barcode, unidos por el sku que comparten.

    ``has_user_edits=True`` en el canónico porque es el primer criterio de
    ``choose_canonical``: sin eso gana el dup (tiene barcode) y el merge no completa
    nada, que es justo el escenario que estos tests NO quieren.

    ``canonical_barcode`` siembra el caso de control: si el canónico YA trae barcode
    propio, el merge no se lo completa y la colisión pasa a ser persistente.
    """
    canonical = await _add_product(
        session, tenant_id, name="Yerba", sku="YERBA", barcode=canonical_barcode, stock_units=5
    )
    canonical.has_user_edits = True
    dup = await _add_product(
        session, tenant_id, name="Yerba x", sku="YERBA", barcode="7790033330003", stock_units=8
    )
    await _add_movement(session, tenant_id, canonical.id, 5, SOURCE_PURCHASE_IMPORT, "y1")
    await _add_movement(session, tenant_id, dup.id, 8, SOURCE_RECEIPT, "y2")
    await _add_balance(session, tenant_id, canonical.id, 5)
    await _add_balance(session, tenant_id, dup.id, 8)
    await session.flush()
    return canonical.id, dup.id


async def _release_key(session: AsyncSession, product_id: uuid.UUID, field: str) -> None:
    """El humano libera UNA clave del canónico (por ORM, para que corra before_update)."""
    product = await session.get(Product, product_id)
    assert product is not None
    setattr(product, field, None)
    await session.commit()


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_revert_permite_colision_transitoria_de_barcode(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """El barcode que el merge COPIÓ al canónico no bloquea: MERGE⁻¹ lo nulea."""
    tid = sample_tenant.tenant_id
    canonical_id, dup_id = await _seed_transient_group(db_session, tid)

    applied, canonicals = await _plan_apply(db_session, tid)
    assert applied.status == "APPLIED"
    assert canonicals == [canonical_id], "el canónico debe ser el que NO tiene barcode"
    await db_session.commit()

    # El merge le copió el barcode del dup al canónico → es un campo completable.
    merge_item = (
        await db_session.execute(
            select(DataRepairItem).where(
                DataRepairItem.run_id == applied.run_id,
                DataRepairItem.action == "MERGE_PRODUCT",
            )
        )
    ).scalars().one()
    assert (merge_item.after_json or {})["fields_completed"] == {"barcode": "7790033330003"}

    # Sin intervención: el sku COMPARTIDO (que el canónico ya tenía) bloquea.
    blocked = await svc.revert_dedup_run(db_session, tid, applied.run_id, lease_id=None)
    assert blocked.group_results[0]["reason"] == svc._REVERT_IDENTITY_COLLISION

    # El humano libera SOLO el sku compartido. El barcode NO lo toca: es del dup.
    await _release_key(db_session, canonical_id, "sku")

    reverted = await svc.revert_dedup_run(db_session, tid, applied.run_id, lease_id=None)
    assert reverted.status == "REVERTED", reverted.group_results
    db_session.expunge_all()
    canon = await db_session.get(Product, canonical_id)
    dup = await db_session.get(Product, dup_id)
    assert canon is not None and canon.barcode is None, "MERGE⁻¹ devolvió el barcode a NULL"
    assert dup is not None and dup.is_active is True
    assert dup.barcode == "7790033330003", "el dup recuperó su barcode"


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_revert_bloquea_colision_persistente_de_barcode(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Control del test anterior: si el barcode YA era del canónico, sigue bloqueando.

    Única diferencia con el caso transitorio: el canónico trae barcode propio, así que
    ``fields_completed`` no lo incluye y MERGE⁻¹ no lo va a liberar.
    """
    tid = sample_tenant.tenant_id
    canonical_id, dup_id = await _seed_transient_group(
        db_session, tid, canonical_barcode="7790033330003"
    )

    applied, _ = await _plan_apply(db_session, tid)
    assert applied.status == "APPLIED"
    await db_session.commit()

    merge_item = (
        await db_session.execute(
            select(DataRepairItem).where(
                DataRepairItem.run_id == applied.run_id,
                DataRepairItem.action == "MERGE_PRODUCT",
            )
        )
    ).scalars().one()
    assert "barcode" not in (merge_item.after_json or {})["fields_completed"]

    await _release_key(db_session, canonical_id, "sku")

    reverted = await svc.revert_dedup_run(db_session, tid, applied.run_id, lease_id=None)
    assert reverted.status == "PARTIALLY_REVERTED"
    assert reverted.group_results[0]["reason"] == svc._REVERT_IDENTITY_COLLISION
    db_session.expunge_all()
    dup = await db_session.get(Product, dup_id)
    assert dup is not None and dup.is_active is False


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_revert_permite_colision_transitoria_de_sku(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Variante espejo: la clave completada es el SKU y la compartida el barcode."""
    tid = sample_tenant.tenant_id
    canonical = await _add_product(
        db_session, tid, name="Yerba", barcode="7790033330003", stock_units=5
    )
    canonical.has_user_edits = True
    dup = await _add_product(
        db_session, tid, name="Yerba x", barcode="7790033330003", sku="YERBA-X", stock_units=8
    )
    await _add_movement(db_session, tid, canonical.id, 5, SOURCE_PURCHASE_IMPORT, "y1")
    await _add_movement(db_session, tid, dup.id, 8, SOURCE_RECEIPT, "y2")
    await _add_balance(db_session, tid, canonical.id, 5)
    await _add_balance(db_session, tid, dup.id, 8)
    await db_session.flush()
    canonical_id, dup_id = canonical.id, dup.id

    applied, canonicals = await _plan_apply(db_session, tid)
    assert applied.status == "APPLIED"
    assert canonicals == [canonical_id]
    await db_session.commit()

    merge_item = (
        await db_session.execute(
            select(DataRepairItem).where(
                DataRepairItem.run_id == applied.run_id,
                DataRepairItem.action == "MERGE_PRODUCT",
            )
        )
    ).scalars().one()
    assert (merge_item.after_json or {})["fields_completed"] == {"sku": "YERBA-X"}

    await _release_key(db_session, canonical_id, "barcode")

    reverted = await svc.revert_dedup_run(db_session, tid, applied.run_id, lease_id=None)
    assert reverted.status == "REVERTED", reverted.group_results
    db_session.expunge_all()
    canon = await db_session.get(Product, canonical_id)
    dup_row = await db_session.get(Product, dup_id)
    assert canon is not None and canon.sku is None
    assert dup_row is not None and dup_row.is_active is True and dup_row.sku == "YERBA-X"


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_revert_bloquea_colision_entre_dos_dups_del_mismo_grupo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Dos dups que recuperan el MISMO barcode colisionan entre sí, no con el canónico.

    Es el caso que la tolerancia transitoria destapa: liberada la clave del canónico,
    los dos dups pasan el chequeo contra la base (el barcode que ven es el que MERGE⁻¹
    va a nulear) y se reactivarían los dos con el mismo código → violación del índice
    DURANTE la mutación, que es justo lo que la fase de guards existe para evitar.
    """
    tid = sample_tenant.tenant_id
    canonical = await _add_product(db_session, tid, name="Yerba", sku="YERBA", stock_units=5)
    canonical.has_user_edits = True
    dup_a = await _add_product(
        db_session, tid, name="Yerba a", sku="YERBA", barcode="7790033330003", stock_units=8
    )
    dup_b = await _add_product(
        db_session, tid, name="Yerba b", sku="YERBA", barcode="7790033330003", stock_units=3
    )
    for pid, qty, src, h in (
        (canonical.id, 5, SOURCE_PURCHASE_IMPORT, "y1"),
        (dup_a.id, 8, SOURCE_RECEIPT, "y2"),
        (dup_b.id, 3, SOURCE_RECEIPT, "y3"),
    ):
        await _add_movement(db_session, tid, pid, qty, src, h)
        await _add_balance(db_session, tid, pid, qty)
    await db_session.flush()
    canonical_id, dup_a_id, dup_b_id = canonical.id, dup_a.id, dup_b.id

    applied, canonicals = await _plan_apply(db_session, tid)
    assert applied.status == "APPLIED"
    assert canonicals == [canonical_id]
    await db_session.commit()

    await _release_key(db_session, canonical_id, "sku")

    reverted = await svc.revert_dedup_run(db_session, tid, applied.run_id, lease_id=None)
    assert reverted.status == "PARTIALLY_REVERTED", reverted.group_results
    assert reverted.group_results[0]["reason"] == svc._REVERT_IDENTITY_COLLISION
    db_session.expunge_all()
    for pid in (dup_a_id, dup_b_id):
        row = await db_session.get(Product, pid)
        assert row is not None and row.is_active is False, "ningún dup se reactivó"
