"""E-5 y E-6 — la fusión de duplicados no pierde operaciones ni deja nada en el
duplicado inactivo.

- **E-5:** con aperturas de catálogo repetidas (MOST_RECENT), el canónico tomaba la
  cantidad del ancla más reciente como stock absoluto y borraba las ventas y
  compras que había tenido después. Ahora el ancla se lleva hacia adelante: si es
  del canónico no se toca nada; si es de un duplicado, se conservan los
  movimientos posteriores del canónico. La reversa vuelve al estado exacto.
- **E-6:** las salidas pendientes del POS (``pos_stock_discrepancies``) y los
  vínculos con proveedores (``product_supplier_links``) del duplicado pasan al
  canónico, y la reversa los devuelve.

Mismo patrón de siembra que ``test_product_dedup_alias_b8a.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.application.services import pos_discrepancy_service
from app.application.services import product_dedup_service as svc
from app.application.services.inventory_movement_origin import SOURCE_CATALOG_INITIAL_STOCK
from app.application.services.product_dedup_service import (
    STOCK_MOST_RECENT,
    STOCK_REVIEW,
    MovementRow,
    ProductRecord,
)
from app.persistence.models.inventory import InventoryBalance, InventoryMovement
from app.persistence.models.pos_stock_discrepancy import PosStockDiscrepancy
from app.persistence.models.product import Product
from app.persistence.models.product_supplier_link import ProductSupplierLink
from app.persistence.models.supplier import Supplier
from app.persistence.models.tenant import Tenant

_BASE = datetime(2026, 1, 1, tzinfo=UTC)


@pytest_asyncio.fixture
async def db_session(isolated_db_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSession(isolated_db_engine, expire_on_commit=False) as session:
        yield session


# ── Helpers ──────────────────────────────────────────────────────────────────────


def _mov(
    qty: int, source_type: str | None, dia: int, tipo: str = "purchase", hora: int = 0
) -> MovementRow:
    return MovementRow(
        qty=qty,
        movement_type=tipo,
        source_type=source_type,
        source_row_hash=None,
        occurred_at=_BASE + timedelta(days=dia, hours=hora),
        created_at=_BASE,
    )


def _record(n: int, stock: int, *movs: MovementRow) -> ProductRecord:
    return ProductRecord(
        id=uuid.UUID(int=n),
        has_user_edits=False,
        barcode_normalized=None,
        sku_normalized=None,
        created_at=_BASE,
        unit_cost_ars=None,
        stock_units=stock,
        fk_count=0,
        current_qty=None,
        reserved_qty=None,
        movements=movs,
    )


def _apertura(qty: int, dia: int) -> MovementRow:
    return _mov(qty, SOURCE_CATALOG_INITIAL_STOCK, dia)


def _venta_pos(qty: int, dia: int, hora: int = 0) -> MovementRow:
    # La venta en vivo no tiene source_type.
    return _mov(-qty, None, dia, tipo="sale", hora=hora)


async def _producto(session: AsyncSession, tid: uuid.UUID, **campos: object) -> Product:
    p = Product(
        id=uuid.uuid4(),
        tenant_id=tid,
        sale_price_ars=Decimal("100.00"),
        custom_fields={},
        is_active=True,
        **campos,
    )
    session.add(p)
    await session.flush()
    return p


async def _movimiento(
    session: AsyncSession,
    tid: uuid.UUID,
    pid: uuid.UUID,
    qty: int,
    source_type: str | None,
    dia: int,
    tipo: str = "purchase",
) -> None:
    session.add(
        InventoryMovement(
            id=uuid.uuid4(),
            tenant_id=tid,
            product_id=pid,
            movement_type=tipo,
            qty=qty,
            source_type=source_type,
            occurred_at=_BASE + timedelta(days=dia),
        )
    )
    await session.flush()


async def _balance(session: AsyncSession, tid: uuid.UUID, pid: uuid.UUID, qty: int) -> None:
    session.add(
        InventoryBalance(
            id=uuid.uuid4(), tenant_id=tid, product_id=pid, current_qty=qty, reserved_qty=0
        )
    )
    await session.flush()


async def _aplicar(session: AsyncSession, tid: uuid.UUID) -> svc.ApplyResult:
    plan = await svc.plan_dedup(session, tid)
    source_run_id = await svc.persist_dedup_plan(session, tid, plan)
    return await svc.apply_dedup_plan(session, tid, source_run_id, lease_id=None)


async def _liberar_claves_y_revertir(
    session: AsyncSession, tid: uuid.UUID, canonico_id: uuid.UUID, run_id: uuid.UUID
) -> svc.RevertResult:
    # La reversa automática rebota por la clave que formó el grupo: se libera a mano.
    c = await session.get(Product, canonico_id)
    assert c is not None
    c.sku = None
    c.barcode = None
    await session.commit()
    return await svc.revert_dedup_run(session, tid, run_id, lease_id=None)


async def _stock(session: AsyncSession, pid: uuid.UUID) -> tuple[int, int | None]:
    session.expunge_all()
    p = await session.get(Product, pid)
    assert p is not None
    bal = (
        await session.execute(
            select(InventoryBalance.current_qty).where(InventoryBalance.product_id == pid)
        )
    ).scalar_one_or_none()
    return p.stock_units, bal


# ── E-5: decisión pura ───────────────────────────────────────────────────────────


def test_e5_el_ancla_del_canonico_es_la_mas_reciente_y_sus_ventas_no_se_borran() -> None:
    # Apertura de 10 el día 10 y 3 vendidas por POS → stock 7. El duplicado trae la
    # apertura vieja del día 1. Antes: delta = 10 − 7 = +3 (las ventas desaparecían).
    canonico = _record(1, 7, _apertura(10, 10), _venta_pos(3, 11))
    dup = _record(2, 10, _apertura(10, 1))
    dec = svc.classify_group_stock_decision(canonico, [dup])
    assert dec.kind == STOCK_MOST_RECENT
    assert dec.delta == 0


def test_e5_el_ancla_del_canonico_es_la_mas_reciente_y_su_compra_no_se_borra() -> None:
    # Apertura de 10 el día 10 y una compra posterior de 5 → stock 15. Antes: −5.
    canonico = _record(1, 15, _apertura(10, 10), _mov(5, "receipt", 12))
    dup = _record(2, 10, _apertura(10, 1))
    dec = svc.classify_group_stock_decision(canonico, [dup])
    assert dec.kind == STOCK_MOST_RECENT
    assert dec.delta == 0


def test_e5_ancla_del_duplicado_se_lleva_hacia_adelante_con_lo_posterior_del_canonico() -> None:
    # Canónico: apertura 10 (día 1), vende 3 (día 5) y 2 (día 12) → 5. El duplicado
    # contó 12 el día 10: esa lectura ya refleja la venta del día 5, pero no la del
    # 12. Stock correcto = 12 − 2 = 10 → delta +5. Antes: 12 − 5 = +7.
    canonico = _record(1, 5, _apertura(10, 1), _venta_pos(3, 5), _venta_pos(2, 12))
    dup = _record(2, 12, _apertura(12, 10))
    dec = svc.classify_group_stock_decision(canonico, [dup])
    assert dec.kind == STOCK_MOST_RECENT
    assert dec.delta == 5


def test_e5_ancla_del_duplicado_y_canonico_con_residuo_va_a_revision() -> None:
    # stock 9 con un ledger que suma 7: no se sabe qué tenía el día de la lectura.
    canonico = _record(1, 9, _apertura(10, 1), _venta_pos(3, 5))
    dup = _record(2, 12, _apertura(12, 10))
    dec = svc.classify_group_stock_decision(canonico, [dup])
    assert dec.kind == STOCK_REVIEW
    assert dec.reason == svc.STOCK_REASON_RESIDUO


def test_e5_movimiento_del_canonico_en_la_fecha_del_ancla_va_a_revision() -> None:
    # Venta el MISMO día que la lectura del duplicado: ¿antes o después? No se adivina.
    canonico = _record(1, 7, _apertura(10, 1), _venta_pos(3, 10))
    dup = _record(2, 12, _apertura(12, 10))
    dec = svc.classify_group_stock_decision(canonico, [dup])
    assert dec.kind == STOCK_REVIEW


def test_e5_venta_del_mismo_dia_del_ancla_con_hora_va_a_revision() -> None:
    # El ancla cae a las 00:00 y la venta del POS del mismo día es a las 15:00: no se
    # sabe si la lectura ya la contaba. Comparar el instante la tomaba como posterior.
    canonico = _record(1, 7, _apertura(10, 1), _venta_pos(3, 10, hora=15))
    dup = _record(2, 12, _apertura(12, 10))
    dec = svc.classify_group_stock_decision(canonico, [dup])
    assert dec.kind == STOCK_REVIEW
    assert dec.reason == svc.STOCK_REASON_MIXED


def test_e5_ancla_del_duplicado_que_daria_stock_negativo_va_a_revision() -> None:
    # Apertura 10 (día 1), vende 8 (día 3) → 2. El duplicado contó 5 el día 2:
    # 5 − 8 = −3. Lectura y ventas se contradicen: no se adivina.
    canonico = _record(1, 2, _apertura(10, 1), _venta_pos(8, 3))
    dup = _record(2, 5, _apertura(5, 2))
    dec = svc.classify_group_stock_decision(canonico, [dup])
    assert dec.kind == STOCK_REVIEW
    assert dec.reason == svc.STOCK_REASON_NEGATIVE_TARGET
    assert dec.delta is None


# ── E-5: apply + revert contra la base ───────────────────────────────────────────


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_e5_ancla_que_daria_stock_negativo_no_escribe_nada(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    canonico = await _producto(
        db_session, tid, name="Harina", sku="HAR1", barcode="7790011110001", stock_units=2
    )
    dup = await _producto(db_session, tid, name="Harina x", sku="HAR1", stock_units=5)
    await _movimiento(db_session, tid, canonico.id, 10, SOURCE_CATALOG_INITIAL_STOCK, 1)
    await _movimiento(db_session, tid, canonico.id, -8, None, 3, tipo="sale")
    await _movimiento(db_session, tid, dup.id, 5, SOURCE_CATALOG_INITIAL_STOCK, 2)
    await _balance(db_session, tid, canonico.id, 2)
    await _balance(db_session, tid, dup.id, 5)
    await db_session.commit()
    canonico_id, dup_id = canonico.id, dup.id

    plan = await svc.plan_dedup(db_session, tid)
    (grupo,) = plan.merge_groups
    assert grupo.requires_review
    assert f"stock:{svc.STOCK_REASON_NEGATIVE_TARGET}" in grupo.review_reasons
    source_run_id = await svc.persist_dedup_plan(db_session, tid, plan)
    await db_session.commit()
    applied = await svc.apply_dedup_plan(db_session, tid, source_run_id, lease_id=None)
    assert applied.groups_applied == 0

    # Antes del arreglo: applied, con stock_units = current_qty = −3.
    assert await _stock(db_session, canonico_id) == (2, 2)
    assert await _stock(db_session, dup_id) == (5, 5)
    d = await db_session.get(Product, dup_id)
    assert d is not None and d.is_active is True


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_e5_la_fusion_conserva_las_ventas_del_pos_del_canonico(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    canonico = await _producto(
        db_session, tid, name="Galletitas", sku="GAL1", barcode="7790077770007", stock_units=7
    )
    dup = await _producto(db_session, tid, name="Galletitas x", sku="GAL1", stock_units=10)
    await _movimiento(db_session, tid, canonico.id, 10, SOURCE_CATALOG_INITIAL_STOCK, 10)
    await _movimiento(db_session, tid, canonico.id, -3, None, 11, tipo="sale")
    await _movimiento(db_session, tid, dup.id, 10, SOURCE_CATALOG_INITIAL_STOCK, 1)
    await _balance(db_session, tid, canonico.id, 7)
    await _balance(db_session, tid, dup.id, 10)
    await db_session.commit()

    applied = await _aplicar(db_session, tid)
    assert applied.status == "APPLIED", applied.group_results

    # Antes del arreglo quedaba en 10: las 3 vendidas desaparecían.
    assert await _stock(db_session, canonico.id) == (7, 7)


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_e5_ancla_del_duplicado_aplica_y_la_reversa_vuelve_al_estado_exacto(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    canonico = await _producto(
        db_session, tid, name="Arroz", sku="ARR1", barcode="7790066660006", stock_units=5
    )
    dup = await _producto(db_session, tid, name="Arroz x", sku="ARR1", stock_units=12)
    await _movimiento(db_session, tid, canonico.id, 10, SOURCE_CATALOG_INITIAL_STOCK, 1)
    await _movimiento(db_session, tid, canonico.id, -3, None, 5, tipo="sale")
    await _movimiento(db_session, tid, canonico.id, -2, None, 12, tipo="sale")
    await _movimiento(db_session, tid, dup.id, 12, SOURCE_CATALOG_INITIAL_STOCK, 10)
    await _balance(db_session, tid, canonico.id, 5)
    await _balance(db_session, tid, dup.id, 12)
    await db_session.commit()
    canonico_id, dup_id = canonico.id, dup.id

    applied = await _aplicar(db_session, tid)
    assert applied.status == "APPLIED", applied.group_results
    # 12 contados el día 10, menos los 2 vendidos el día 12.
    assert await _stock(db_session, canonico_id) == (10, 10)

    reverted = await _liberar_claves_y_revertir(db_session, tid, canonico_id, applied.run_id)
    assert reverted.status == "REVERTED", reverted.group_results
    assert await _stock(db_session, canonico_id) == (5, 5)
    assert await _stock(db_session, dup_id) == (12, 12)
    d = await db_session.get(Product, dup_id)
    assert d is not None and d.is_active is True


# ── E-6: salidas pendientes del POS ──────────────────────────────────────────────


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_e6_la_salida_pendiente_pasa_al_canonico_y_se_resuelve_sobre_el(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    canonico = await _producto(
        db_session, tid, name="Fideos", sku="FID1", barcode="7790055550005", stock_units=2
    )
    dup = await _producto(db_session, tid, name="Fideos x", sku="FID1", stock_units=0)
    disc = PosStockDiscrepancy(
        tenant_id=tid,
        operation_id=uuid.uuid4(),
        line_id=uuid.uuid4(),
        product_id=dup.id,
        pending_units=5,
        stock_at_capture=0,
    )
    db_session.add(disc)
    await db_session.commit()
    canonico_id, dup_id, disc_id = canonico.id, dup.id, disc.id

    applied = await _aplicar(db_session, tid)
    assert applied.status == "APPLIED", applied.group_results

    db_session.expunge_all()
    movida = await db_session.get(PosStockDiscrepancy, disc_id)
    assert movida is not None and movida.product_id == canonico_id

    # Resolverla: el faltante sale del stock del canónico (5 − 2 = 3) y entra ahí,
    # no al duplicado inactivo.
    faltante = await pos_discrepancy_service.aplicar_ajuste_de_entrada(
        db_session, movida, occurred_at=None
    )
    await db_session.commit()
    assert faltante == 3
    assert (await _stock(db_session, canonico_id))[0] == 5
    assert (await _stock(db_session, dup_id))[0] == 0


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_e6_la_reversa_devuelve_la_salida_pendiente_al_duplicado(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    canonico = await _producto(
        db_session, tid, name="Polenta", sku="POL1", barcode="7790044440004", stock_units=2
    )
    dup = await _producto(db_session, tid, name="Polenta x", sku="POL1", stock_units=0)
    disc = PosStockDiscrepancy(
        tenant_id=tid,
        operation_id=uuid.uuid4(),
        line_id=uuid.uuid4(),
        product_id=dup.id,
        pending_units=5,
        stock_at_capture=0,
    )
    db_session.add(disc)
    await db_session.commit()
    canonico_id, dup_id, disc_id = canonico.id, dup.id, disc.id

    applied = await _aplicar(db_session, tid)
    assert applied.status == "APPLIED", applied.group_results
    reverted = await _liberar_claves_y_revertir(db_session, tid, canonico_id, applied.run_id)
    assert reverted.status == "REVERTED", reverted.group_results

    db_session.expunge_all()
    vuelta = await db_session.get(PosStockDiscrepancy, disc_id)
    assert vuelta is not None and vuelta.product_id == dup_id


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_e6_no_revierte_si_la_salida_pendiente_se_resolvio_despues(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    canonico = await _producto(
        db_session, tid, name="Lentejas", sku="LEN1", barcode="7790033330003", stock_units=2
    )
    dup = await _producto(db_session, tid, name="Lentejas x", sku="LEN1", stock_units=0)
    disc = PosStockDiscrepancy(
        tenant_id=tid,
        operation_id=uuid.uuid4(),
        line_id=uuid.uuid4(),
        product_id=dup.id,
        pending_units=1,
        stock_at_capture=0,
    )
    db_session.add(disc)
    await db_session.commit()
    canonico_id, disc_id = canonico.id, disc.id

    applied = await _aplicar(db_session, tid)
    assert applied.status == "APPLIED", applied.group_results
    # Sólo cambia el estado (sin tocar stock ni ledger): la única guarda que lo ve
    # es la de E-6.
    movida = await db_session.get(PosStockDiscrepancy, disc_id)
    assert movida is not None
    movida.status = "RESOLVED"
    await db_session.commit()

    reverted = await _liberar_claves_y_revertir(db_session, tid, canonico_id, applied.run_id)
    assert reverted.groups_reverted == 0
    assert reverted.group_results[0]["reason"] == "state_diverged"
    db_session.expunge_all()
    sigue = await db_session.get(PosStockDiscrepancy, disc_id)
    assert sigue is not None and sigue.product_id == canonico_id


# ── E-6: vínculos con proveedores ────────────────────────────────────────────────


async def _proveedor(session: AsyncSession, tid: uuid.UUID, nombre: str) -> uuid.UUID:
    s = Supplier(tenant_id=tid, name=nombre)
    session.add(s)
    await session.flush()
    return s.id


async def _vinculo(
    session: AsyncSession,
    tid: uuid.UUID,
    pid: uuid.UUID,
    sid: uuid.UUID,
    source: str,
    *,
    anulado: bool = False,
) -> uuid.UUID:
    lk = ProductSupplierLink(
        tenant_id=tid,
        product_id=pid,
        supplier_id=sid,
        source=source,
        voided_at=_BASE if anulado else None,
    )
    session.add(lk)
    await session.flush()
    return lk.id


async def _vinculo_estado(
    session: AsyncSession, link_id: uuid.UUID
) -> tuple[uuid.UUID, str, bool]:
    session.expunge_all()
    lk = await session.get(ProductSupplierLink, link_id)
    assert lk is not None
    return lk.product_id, lk.source, lk.voided_at is not None


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_e6_los_vinculos_con_proveedores_pasan_al_canonico_y_vuelven(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    canonico = await _producto(
        db_session, tid, name="Aceite", sku="ACE1", barcode="7790022220002", stock_units=0
    )
    dup = await _producto(db_session, tid, name="Aceite x", sku="ACE1", stock_units=0)
    norte = await _proveedor(db_session, tid, "Norte")
    sur = await _proveedor(db_session, tid, "Sur")
    oeste = await _proveedor(db_session, tid, "Oeste")
    # Norte: sólo el duplicado lo tiene → se mueve.
    solo_dup = await _vinculo(db_session, tid, dup.id, norte, "purchase_evidence")
    # Sur: el canónico lo tiene anulado y por catálogo; el del duplicado está vivo con
    # compra → el del canónico revive con la procedencia fuerte (único por par).
    sur_canon = await _vinculo(
        db_session, tid, canonico.id, sur, "catalog_declared", anulado=True
    )
    sur_dup = await _vinculo(db_session, tid, dup.id, sur, "purchase_evidence")
    # Oeste: los dos lo tienen vivo e igual → nada que hacer, el del dup queda.
    oeste_canon = await _vinculo(db_session, tid, canonico.id, oeste, "purchase_evidence")
    oeste_dup = await _vinculo(db_session, tid, dup.id, oeste, "catalog_declared")
    await db_session.commit()
    canonico_id, dup_id = canonico.id, dup.id

    applied = await _aplicar(db_session, tid)
    assert applied.status == "APPLIED", applied.group_results

    assert await _vinculo_estado(db_session, solo_dup) == (
        canonico_id, "purchase_evidence", False
    )
    assert await _vinculo_estado(db_session, sur_canon) == (
        canonico_id, "purchase_evidence", False
    )
    assert await _vinculo_estado(db_session, sur_dup) == (dup_id, "purchase_evidence", False)
    assert await _vinculo_estado(db_session, oeste_canon) == (
        canonico_id, "purchase_evidence", False
    )
    assert await _vinculo_estado(db_session, oeste_dup) == (dup_id, "catalog_declared", False)

    reverted = await _liberar_claves_y_revertir(db_session, tid, canonico_id, applied.run_id)
    assert reverted.status == "REVERTED", reverted.group_results

    assert await _vinculo_estado(db_session, solo_dup) == (dup_id, "purchase_evidence", False)
    assert await _vinculo_estado(db_session, sur_canon) == (
        canonico_id, "catalog_declared", True
    )
    session_link = await db_session.get(ProductSupplierLink, sur_canon)
    assert session_link is not None and session_link.voided_at is not None
    assert session_link.voided_at.replace(tzinfo=UTC) == _BASE
    assert await _vinculo_estado(db_session, sur_dup) == (dup_id, "purchase_evidence", False)
    assert await _vinculo_estado(db_session, oeste_canon) == (
        canonico_id, "purchase_evidence", False
    )


@pytest.mark.usefixtures("legacy_pre_f5_schema")
async def test_e6_no_revierte_si_un_vinculo_movido_se_edito_despues(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    canonico = await _producto(
        db_session, tid, name="Vinagre", sku="VIN1", barcode="7790011110002", stock_units=0
    )
    dup = await _producto(db_session, tid, name="Vinagre x", sku="VIN1", stock_units=0)
    norte = await _proveedor(db_session, tid, "Norte")
    movido = await _vinculo(db_session, tid, dup.id, norte, "catalog_declared")
    await db_session.commit()
    canonico_id = canonico.id

    applied = await _aplicar(db_session, tid)
    assert applied.status == "APPLIED", applied.group_results
    # Alguien lo anula después (sigue en el canónico): devolverlo pisaría esa edición.
    lk = await db_session.get(ProductSupplierLink, movido)
    assert lk is not None and lk.product_id == canonico_id
    lk.voided_at = _BASE
    await db_session.commit()

    reverted = await _liberar_claves_y_revertir(db_session, tid, canonico_id, applied.run_id)
    assert reverted.groups_reverted == 0
    assert reverted.group_results[0]["reason"] == "state_diverged"
    assert await _vinculo_estado(db_session, movido) == (canonico_id, "catalog_declared", True)
