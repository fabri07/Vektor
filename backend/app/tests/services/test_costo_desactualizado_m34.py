"""M·3-4 — la antigüedad del costo de cada venta («costo desactualizado» > 45 días).

Decisión del dueño: no se ajusta ningún costo por inflación; se dice qué parte de
lo vendido usó un costo de hace más de 45 días. La fecha de la fuente se congela
en la venta (``cost_source_date``); las ventas viejas la reciben del backfill
``--solo-fecha`` sólo si la fuente de hoy da el mismo costo (nunca revalúa).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pandas as pd
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.costeo_retroactivo_service import fechar_costos, revertir
from app.application.services.facts_service import FactsService, Period
from app.domain.business_time import today_ar
from app.domain.costo_de_referencia import CostoDeReferencia
from app.domain.costo_de_venta import costo_de_venta
from app.domain.costo_vigente import FECHA_FIELD, ORIGEN_FIELD, CostoFechado
from app.domain.margen import es_costo_desactualizado, texto_antiguedad_del_costo
from app.persistence.models.tenant import Tenant
from app.schemas.product import ProductResponse
from app.tests.application.test_facts_reconciliation import (
    _EXPENSES_COLS,
    _PRODUCTS_COLS,
    KIOSCO_THRESHOLDS,
    FakeProvider,
)
from app.tests.services.test_costeo_de_ventas import (
    _a_las,
    _comprar,
    _producto,
    _regimen,
    _vender,
)

# ── Dominio ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("dias", "viejo"), [(44, False), (45, False), (46, True), (None, None)])
def test_mas_de_45_dias_es_desactualizado(dias: int | None, viejo: bool | None) -> None:
    assert es_costo_desactualizado(dias) is viejo


def test_texto_de_la_antiguedad() -> None:
    assert texto_antiguedad_del_costo("compra", 52) == "última compra hace 52 días"
    assert texto_antiguedad_del_costo("stock_inicial", 1) == "última compra hace 1 día"
    assert texto_antiguedad_del_costo("manual", 0) == "costo cargado hoy"
    assert texto_antiguedad_del_costo("compra", None) is None


def _venta(referencia: CostoDeReferencia, manual: CostoFechado | None = None) -> Any:
    return costo_de_venta(
        referencia, manual, None, dia_de_venta=date(2026, 10, 10), unidades_base=1,
        base_units_per_sale_unit=1, fiscal_condition_crudo="monotributo",
    )


def test_la_fuente_lleva_su_fecha() -> None:
    compra = CostoDeReferencia(
        origen="compra", costo_unitario=Decimal("100"), fecha=datetime(2026, 8, 20, 12),
        envio="incluido", impuesto="no_aplica", contrato_confirmado=True,
    )
    assert _venta(compra).fecha_fuente == date(2026, 8, 20)
    # Una edición manual posterior a la compra gana y trae SU fecha.
    edicion = CostoFechado(Decimal("120"), datetime(2026, 9, 1, 9))
    snap = _venta(compra, edicion)
    assert (snap.origen, snap.fecha_fuente) == ("manual", date(2026, 9, 1))
    # Costo cargado sin fecha: antigüedad desconocida.
    cargado = CostoDeReferencia(origen="costo_cargado", costo_unitario=Decimal("90"))
    assert _venta(cargado).fecha_fuente is None


# ── FactsService ─────────────────────────────────────────────────────────────

_OCT = Period(start=date(2026, 10, 1), end=date(2026, 10, 31), label="mes_2026_10")


def _bruto(filas: list[dict[str, Any]]) -> Any:
    sales = pd.DataFrame(
        [{"sale_date": date(2026, 10, 1), "payment_method": "cash", "provenance": "REAL",
          "cost_confirmed": True, **f} for f in filas],
        columns=["sale_date", "amount_ars", "cost_ars", "cost_confirmed", "payment_method",
                 "provenance", "cost_source_date"],
    )
    svc = FactsService(FakeProvider(sales, pd.DataFrame(columns=_EXPENSES_COLS),
                                    pd.DataFrame(columns=_PRODUCTS_COLS)), KIOSCO_THRESHOLDS)
    return svc.margen_bruto("t", _OCT)


def test_el_bruto_dice_cuanto_se_vendio_con_costo_viejo() -> None:
    f = _bruto([
        # 1/10 − 16/8 = 46 días: desactualizado.
        {"amount_ars": 300, "cost_ars": 100, "cost_source_date": date(2026, 8, 16)},
        # 45 días justos: al día.
        {"amount_ars": 500, "cost_ars": 200, "cost_source_date": date(2026, 8, 17)},
        # Sin fecha: desconocida, nunca «al día».
        {"amount_ars": 200, "cost_ars": 50, "cost_source_date": None},
    ])
    assert f.detail["costo_desactualizado_ars"] == 300.0
    assert f.detail["costo_desactualizado_pct"] == 30.0
    assert f.detail["costo_antiguedad_desconocida_ars"] == 200.0
    assert f.detail["costo_antiguedad_desconocida_pct"] == 20.0
    assert f.detail["costo_desactualizado_umbral_dias"] == 45


def test_una_venta_sin_costo_no_cuenta_como_desactualizada() -> None:
    f = _bruto([
        {"amount_ars": 300, "cost_ars": float("nan"), "cost_source_date": date(2026, 1, 1)},
        {"amount_ars": 700, "cost_ars": 300, "cost_source_date": date(2026, 9, 30)},
    ])
    assert f.detail["costo_desactualizado_ars"] == 0.0


# ── Productos ────────────────────────────────────────────────────────────────


def _producto_resp(cf: dict[str, Any], costo: str | None = "100") -> ProductResponse:
    import uuid  # noqa: PLC0415

    return ProductResponse.model_validate({
        "id": uuid.uuid4(), "tenant_id": uuid.uuid4(), "name": "Vela", "sku": None,
        "description": None, "category": None,
        "sale_price_ars": Decimal("200"),
        "unit_cost_ars": None if costo is None else Decimal(costo),
        "stock_units": 0, "low_stock_threshold_units": 0, "is_active": True,
        "custom_fields": cf, "created_at": datetime(2026, 1, 1),
    })


def test_productos_dice_la_antiguedad_del_costo_vigente() -> None:
    hace_46 = (today_ar() - timedelta(days=46)).isoformat() + "T00:00:00"
    p = _producto_resp({ORIGEN_FIELD: "compra", FECHA_FIELD: hace_46})
    assert p.costo_antiguedad_dias == 46
    assert p.costo_desactualizado is True
    assert p.costo_antiguedad_texto == "última compra hace 46 días"
    hace_45 = (today_ar() - timedelta(days=45)).isoformat() + "T00:00:00"
    al_dia = _producto_resp({ORIGEN_FIELD: "compra", FECHA_FIELD: hace_45})
    assert al_dia.costo_desactualizado is False
    # Sin sello o sin costo: desconocida.
    assert _producto_resp({}).costo_desactualizado is None
    assert _producto_resp({FECHA_FIELD: hace_46}, costo=None).costo_antiguedad_dias is None


# ── Costeo y backfill contra la base ─────────────────────────────────────────

HOY = today_ar()


async def test_el_costeo_congela_la_fecha_de_la_compra(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    await _regimen(db_session, sample_tenant, "monotributo")
    p = await _producto(db_session, sample_tenant)
    await _comprar(db_session, sample_tenant, p, "100", _a_las(HOY - timedelta(days=50)))
    venta = await _vender(db_session, sample_tenant, p, _a_las(HOY))
    assert venta.cost_origin == "compra"
    assert venta.cost_source_date == HOY - timedelta(days=50)


async def test_backfill_fecha_solo_si_coincide_y_se_revierte(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    await _regimen(db_session, sample_tenant, "monotributo")
    p = await _producto(db_session, sample_tenant)
    await _comprar(db_session, sample_tenant, p, "100", _a_las(HOY - timedelta(days=60)))
    coincide = await _vender(db_session, sample_tenant, p, _a_las(HOY - timedelta(days=5)))
    otra = await _vender(db_session, sample_tenant, p, _a_las(HOY - timedelta(days=4)))
    # Como una venta anterior a M·3-4: sin fecha; a la segunda se le congeló otro
    # costo (p.ej. se corrigió la compra después): no coincide.
    coincide.cost_source_date = None
    otra.cost_source_date = None
    otra.cost_unit_ars = Decimal("90.00")
    await db_session.commit()

    ensayo = await fechar_costos(db_session, tid, aplicar=False)
    assert (ensayo.ventas, ensayo.fechadas, ensayo.no_coinciden) == (2, 1, 1)
    assert ensayo.desactualizadas == 1  # 55 días a la fecha de la venta
    await db_session.rollback()

    r = await fechar_costos(db_session, tid, aplicar=True)
    await db_session.commit()
    await db_session.refresh(coincide)
    await db_session.refresh(otra)
    assert coincide.cost_source_date == HOY - timedelta(days=60)
    assert otra.cost_source_date is None
    assert otra.cost_unit_ars == Decimal("90.00")  # nunca revalúa

    assert r.run_id is not None
    assert await revertir(db_session, r.run_id) == (1, 0)
    await db_session.commit()
    await db_session.refresh(coincide)
    assert coincide.cost_source_date is None


async def test_backfill_sin_nada_que_fechar_no_deja_corrida(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    from sqlalchemy import func, select  # noqa: PLC0415

    from app.persistence.models.repair import DataRepairRun  # noqa: PLC0415

    tid = sample_tenant.tenant_id
    await _regimen(db_session, sample_tenant, "monotributo")
    p = await _producto(db_session, sample_tenant)
    await _comprar(db_session, sample_tenant, p, "100", _a_las(HOY - timedelta(days=10)))
    venta = await _vender(db_session, sample_tenant, p, _a_las(HOY))
    venta.cost_source_date = None
    venta.cost_unit_ars = Decimal("90.00")  # no coincide: nunca se fecha
    await db_session.commit()
    r = await fechar_costos(db_session, tid, aplicar=True)
    await db_session.commit()
    assert (r.ventas, r.fechadas, r.run_id) == (1, 0, None)
    corridas = await db_session.scalar(
        select(func.count()).select_from(DataRepairRun).where(DataRepairRun.tenant_id == tid)
    )
    assert corridas == 0
