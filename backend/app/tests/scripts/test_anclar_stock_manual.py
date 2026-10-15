"""``scripts/anclar_stock_manual.py`` (M·P2c): el stock cargado a mano antes de
M·P2c queda explicado por el ledger, con el costo conocido A ESA FECHA."""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from anclar_stock_manual import anclar_tenant  # noqa: E402

from app.application.services import stock_service  # noqa: E402
from app.domain.costo_vigente import MANUAL_FIELD  # noqa: E402
from app.persistence.models.inventory import InventoryMovement  # noqa: E402
from app.persistence.models.product import Product  # noqa: E402
from app.persistence.models.tenant import Tenant  # noqa: E402
from app.persistence.repositories.costo_promedio_repository import (  # noqa: E402
    cargar_historias,
)

_ALTA = datetime(2026, 5, 10, 15, 0, tzinfo=UTC)


async def _producto(
    db: AsyncSession, tenant_id: uuid.UUID, stock: int, **extra: object
) -> Product:
    p = Product(
        tenant_id=tenant_id, name=f"Vela {uuid.uuid4().hex[:6]}",
        sale_price_ars=Decimal("300"), stock_units=stock, created_at=_ALTA, **extra,
    )
    db.add(p)
    await db.flush()
    return p


async def _movs(db: AsyncSession, pid: uuid.UUID) -> list[InventoryMovement]:
    return list(
        (await db.execute(select(InventoryMovement).where(InventoryMovement.product_id == pid)))
        .scalars()
        .all()
    )


async def test_dry_run_no_escribe(db_session: AsyncSession, sample_tenant: Tenant) -> None:
    p = await _producto(db_session, sample_tenant.tenant_id, 40)
    informe = await anclar_tenant(db_session, sample_tenant.tenant_id, apply=False)
    assert informe.anclados == 1 and informe.unidades == 40
    assert await _movs(db_session, p.id) == []


async def test_apply_ancla_sin_inventar_costo_y_es_idempotente(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    # Costo de HOY sin sello del alta: no se usa para stock de mayo.
    p = await _producto(db_session, tid, 40, unit_cost_ars=Decimal("999"))
    informe = await anclar_tenant(db_session, tid, apply=True)
    assert (informe.anclados, informe.sin_costo) == (1, 1)
    (mov,) = await _movs(db_session, p.id)
    assert (mov.movement_type, mov.source_type, mov.qty) == (
        "adjustment", "catalog_initial_stock", 40,
    )
    assert mov.unit_cost is None
    assert mov.source_event_id == f"manual:ancla:{p.id}"
    assert p.stock_units == 40  # no se toca

    historia = (await cargar_historias(db_session, tid, [p.id]))[p.id]
    assert not historia.stock_fuera_del_ledger

    otra = await anclar_tenant(db_session, tid, apply=True)
    assert otra.anclados == 0
    assert len(await _movs(db_session, p.id)) == 1


async def test_usa_el_costo_tipeado_el_dia_del_alta(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    p = await _producto(
        db_session, tid, 10, unit_cost_ars=Decimal("80"),
        custom_fields={MANUAL_FIELD: {"costo": "80", "fecha": "2026-05-10T12:00:00"}},
    )
    await anclar_tenant(db_session, tid, apply=True)
    (mov,) = await _movs(db_session, p.id)
    assert mov.unit_cost == Decimal("80")
    assert mov.unit_cost_final == Decimal("80")


async def test_diferencia_negativa_no_se_ancla(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    p = await _producto(db_session, tid, 0, unit_cost_ars=Decimal("80"))
    await stock_service.increment_stock(p.id, tid, 10, Decimal("80"), None, db_session)
    p.stock_units = 4  # un setattr viejo bajó el stock sin movimiento
    await db_session.flush()
    informe = await anclar_tenant(db_session, tid, apply=True)
    assert (informe.anclados, informe.negativos) == (0, 1)
    assert len(await _movs(db_session, p.id)) == 1
