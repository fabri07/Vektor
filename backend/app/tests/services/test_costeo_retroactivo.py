"""M·2c — costeo retroactivo de las ventas sin snapshot, siempre como estimación."""

from __future__ import annotations

import unittest.mock
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.costeo_retroactivo_service import (
    MOTIVO_RETROACTIVO,
    REPAIR_TYPE,
    costear_historico,
    revertir,
)
from app.application.services.stock_service import increment_stock
from app.domain.business_time import today_ar
from app.domain.contrato_de_costo import ContratoDeCosto
from app.persistence.models.business import BusinessProfile
from app.persistence.models.product import Product
from app.persistence.models.repair import DataRepairItem, DataRepairRun
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry

HOY = today_ar()


def _dia(dias_atras: int, hora: int = 10) -> datetime:
    return datetime.combine(HOY - timedelta(days=dias_atras), datetime.min.time()).replace(
        hour=hora
    )


async def _producto(db: AsyncSession, tenant: Tenant, factor: int = 1) -> Product:
    p = Product(
        tenant_id=tenant.tenant_id,
        name=f"P {uuid.uuid4().hex[:6]}",
        sale_price_ars=Decimal("200"),
        stock_units=0,
        sale_unit="unit" if factor == 1 else "gram",
        base_units_per_sale_unit=factor,
    )
    db.add(p)
    await db.flush()
    return p


async def _compra_confirmada(db: AsyncSession, tenant: Tenant, p: Product, costo: str) -> None:
    await db.execute(
        update(BusinessProfile)
        .where(BusinessProfile.tenant_id == tenant.tenant_id)
        .values(fiscal_condition="monotributo")
    )
    with unittest.mock.patch("app.application.services.stock_service.EventBus.emit"):
        await increment_stock(
            p.id, tenant.tenant_id, 5000, Decimal(costo), None, db,
            occurred_at=_dia(30),
            contrato=ContratoDeCosto(
                unit_cost_final=Decimal(costo),
                shipping_treatment="incluido",
                tax_treatment="no_aplica",
            ),
        )


async def _venta_vieja(
    db: AsyncSession, tenant: Tenant, p: Product, qty: int = 2, dias_atras: int = 10
) -> SaleEntry:
    """Una venta como las anteriores a M·2a: sin snapshot."""
    v = SaleEntry(
        tenant_id=tenant.tenant_id, product_id=p.id, amount=Decimal("400"), quantity=qty,
        transaction_date=_dia(dias_atras), payment_method="cash",
    )
    db.add(v)
    await db.commit()  # el listener la costea: se borra para simular lo viejo
    await db.execute(
        update(SaleEntry)
        .where(SaleEntry.id == v.id)
        .values(
            cost_unit_ars=None, cost_ars=None, cost_confirmed=None, cost_origin=None,
            cost_reason=None, cost_snapshot_version=None,
        )
    )
    await db.commit()
    await db.refresh(v)
    assert v.cost_snapshot_version is None
    return v


async def test_aplica_como_estimacion_aunque_en_vivo_hubiera_sido_confirmada(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    p = await _producto(db_session, sample_tenant)
    await _compra_confirmada(db_session, sample_tenant, p, "100")
    v = await _venta_vieja(db_session, sample_tenant, p)

    r = await costear_historico(db_session, sample_tenant.tenant_id, aplicar=True)
    await db_session.commit()

    assert r.ventas == 1
    assert r.habrian_sido_confirmadas == 1
    assert r.por_origen == {"compra": 1}
    assert r.costo_total == Decimal("200.00")
    assert r.cobertura_pct == Decimal("100.0")
    await db_session.refresh(v)
    assert v.cost_ars == Decimal("200.00")
    assert v.cost_confirmed is False
    assert v.cost_reason is not None and v.cost_reason.startswith(MOTIVO_RETROACTIVO)
    run = await db_session.get(DataRepairRun, r.run_id)
    assert run is not None and run.repair_type == REPAIR_TYPE and run.status == "APPLIED"
    items = (
        await db_session.execute(select(DataRepairItem).where(DataRepairItem.run_id == run.id))
    ).scalars().all()
    assert [i.sale_entry_id for i in items] == [v.id]
    assert items[0].action == "UPDATE_SALE"


async def test_reejecutar_no_toca_lo_ya_costeado(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    p = await _producto(db_session, sample_tenant)
    await _venta_vieja(db_session, sample_tenant, p)
    await costear_historico(db_session, sample_tenant.tenant_id, aplicar=True)
    await db_session.commit()
    segunda = await costear_historico(db_session, sample_tenant.tenant_id, aplicar=True)
    assert segunda.ventas == 0
    assert segunda.run_id is None


async def test_dry_run_no_deja_corrida(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    p = await _producto(db_session, sample_tenant)
    await _venta_vieja(db_session, sample_tenant, p)
    r = await costear_historico(db_session, sample_tenant.tenant_id, aplicar=False)
    assert r.ventas == 1
    assert r.run_id is None
    corridas = (await db_session.execute(select(DataRepairRun))).scalars().all()
    assert corridas == []


async def test_fraccionado_sin_pos_ni_archivo_queda_sin_costo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    queso = await _producto(db_session, sample_tenant, factor=1000)
    await _compra_confirmada(db_session, sample_tenant, queso, "800")
    v = await _venta_vieja(db_session, sample_tenant, queso, qty=2)
    r = await costear_historico(db_session, sample_tenant.tenant_id, aplicar=True)
    await db_session.commit()
    await db_session.refresh(v)
    assert v.cost_origin == "sin_costo"
    assert v.cost_ars is None
    assert r.por_origen == {"sin_costo": 1}
    assert "unidades base" in (v.cost_reason or "")


async def test_revertir_vuelve_a_null_y_saltea_lo_que_cambio(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    p = await _producto(db_session, sample_tenant)
    await _compra_confirmada(db_session, sample_tenant, p, "100")
    v1 = await _venta_vieja(db_session, sample_tenant, p, dias_atras=10)
    v2 = await _venta_vieja(db_session, sample_tenant, p, dias_atras=9)
    r = await costear_historico(db_session, sample_tenant.tenant_id, aplicar=True)
    await db_session.commit()
    assert r.run_id is not None
    # Alguien cambió la segunda después de la corrida (su costo de línea pasa a
    # 500): no se revierte.
    v2.quantity = 5
    await db_session.commit()

    revertidas, salteadas = await revertir(db_session, r.run_id)
    await db_session.commit()
    assert (revertidas, salteadas) == (1, 1)
    await db_session.refresh(v1)
    await db_session.refresh(v2)
    assert v1.cost_snapshot_version is None
    assert v2.cost_snapshot_version == 1
    run = await db_session.get(DataRepairRun, r.run_id)
    assert run is not None and run.status == "REVERTED"


async def test_otro_tenant_no_se_toca(
    db_session: AsyncSession, sample_tenant: Tenant, second_tenant: Tenant
) -> None:
    ajeno = await _producto(db_session, second_tenant)
    v = await _venta_vieja(db_session, second_tenant, ajeno)
    r = await costear_historico(db_session, sample_tenant.tenant_id, aplicar=True)
    await db_session.commit()
    assert r.ventas == 0
    await db_session.refresh(v)
    assert v.cost_snapshot_version is None
