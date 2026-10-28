"""Fecha de negocio AR en resúmenes, listados, series y chat (E0-A-6..15, B-8, D-7).

El server corre en UTC: de 21 a 24 ART, `date.today()` ya es mañana. Cada
ventana «últimos N días» se corría un día: el día más viejo salía del total, el
«hasta» informaba mañana y las series diarias terminaban en un día futuro en 0.

Todos los casos corren a las 22:30 ART de un día DISTINTO del real (hace tres
días, como en T7): cualquier `date.today()` que quede da otro día a cualquier
hora del server, y el test lo caza. Cada caso siembra un dato en el borde viejo
de la ventana (hoy − N), que entra con el día AR y sale con el UTC.
"""

from __future__ import annotations

import importlib
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import deterministic_finance
from app.application.services.marketing_service import MarketingService
from app.application.services.validation_gate import ValidationGate
from app.domain.business_time import AR_TZ, today_ar
from app.persistence.models.customer import Customer
from app.persistence.models.social_metric import SocialMetric
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import ExpenseEntry, SaleEntry
from app.persistence.repositories.customer_repository import CustomerRepository
from app.persistence.repositories.transaction_repository import (
    ExpenseRepository,
    SaleRepository,
    get_daily_net_series,
)
from app.tests._reloj_ar import reloj_utc

_DIA = today_ar() - timedelta(days=3)
_AR_2230 = datetime(_DIA.year, _DIA.month, _DIA.day, 22, 30, tzinfo=AR_TZ)

#: Módulos cuyo `date.today()` tiene que dar el día UTC (si todavía lo usan).
_MODULOS = (
    "app.persistence.repositories.transaction_repository",
    "app.persistence.repositories.customer_repository",
    "app.api.v1.sales",
    "app.api.v1.expenses",
    "app.application.services.deterministic_finance",
    "app.application.services.validation_gate",
    "app.application.services.marketing_service",
)


def _a_las(dia: date, hora: int) -> datetime:
    return datetime.combine(dia, time(hora, 0))


def _venta(tenant: Tenant, dia: date, monto: str, **extra: Any) -> SaleEntry:
    return SaleEntry(
        tenant_id=tenant.tenant_id, amount=Decimal(monto), quantity=1,
        transaction_date=_a_las(dia, 10), payment_method="cash", **extra,
    )


def _gasto(tenant: Tenant, dia: date, monto: str) -> ExpenseEntry:
    return ExpenseEntry(
        tenant_id=tenant.tenant_id, amount=Decimal(monto), category="RENT",
        description="Alquiler", transaction_date=_a_las(dia, 10),
    )


def test_el_reloj_reproduce_el_server_en_utc() -> None:
    """Sanidad del andamio: a las 22:30 ART el día UTC ya es mañana."""
    ventas = importlib.import_module("app.api.v1.sales")
    with reloj_utc(_AR_2230, _MODULOS):
        assert today_ar() == _DIA
        assert ventas.date.today() == _DIA + timedelta(days=1)


# ── E0-A-6 / B-8: series diarias, velocidad y estadística de gastos ──────────


async def test_las_series_diarias_terminan_hoy_ar_y_no_en_un_dia_futuro(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    db_session.add_all([
        _venta(sample_tenant, _DIA, "1000"),
        _venta(sample_tenant, _DIA - timedelta(days=59), "300"),
        _gasto(sample_tenant, _DIA, "400"),
    ])
    await db_session.commit()
    tid = sample_tenant.tenant_id

    with reloj_utc(_AR_2230, _MODULOS):
        ingresos = await SaleRepository(db_session).get_daily_revenue_series(tid, days=60)
        gastos = await ExpenseRepository(db_session).get_daily_expense_series(tid, days=60)
        neto = await get_daily_net_series(db_session, tid, days=60)

    assert len(ingresos) == len(gastos) == len(neto) == 60
    # El último punto es HOY con su venta, no un mañana en 0.0.
    assert ingresos[-1] == 1000.0
    assert gastos[-1] == 400.0
    assert neto[-1] == 600.0
    # El día más viejo de la ventana sigue adentro.
    assert ingresos[0] == 300.0


async def test_velocidad_y_estadistica_de_gastos_incluyen_el_borde_ar(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    from app.tests._respaldo import producto_con_compra  # noqa: PLC0415

    tid = sample_tenant.tenant_id
    producto = await producto_con_compra(db_session, tid, nombre="Borde AR", stock=10)
    db_session.add_all([
        _venta(sample_tenant, _DIA - timedelta(days=30), "500", product_id=producto.id),
        _gasto(sample_tenant, _DIA - timedelta(days=90), "700"),
    ])
    await db_session.commit()

    with reloj_utc(_AR_2230, _MODULOS):
        velocidad = await SaleRepository(db_session).get_daily_velocity(tid, days=30)
        por_categoria = await ExpenseRepository(db_session).get_expense_stats_by_category(
            tid, days=90
        )

    assert velocidad == {str(producto.id): round(1 / 30, 4)}
    assert 700.0 in por_categoria["RENT"]["amounts"]


# ── E0-A-7: /sales/summary y el listado por defecto ─────────────────────────


async def test_resumen_y_listado_de_ventas_a_las_2230_usan_el_dia_ar(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    borde = _DIA - timedelta(days=30)
    db_session.add(_venta(sample_tenant, borde, "2500"))
    await db_session.commit()

    with reloj_utc(_AR_2230, _MODULOS):
        resumen = await client.get("/api/v1/sales/summary", headers=auth_headers)
        listado = await client.get("/api/v1/sales", headers=auth_headers)

    assert resumen.status_code == 200, resumen.text
    cuerpo = resumen.json()
    assert cuerpo["period_covered"] == f"{borde} al {_DIA}"
    assert cuerpo["entry_count"] == 1
    assert float(cuerpo["total_ars"]) == 2500.0
    assert listado.status_code == 200, listado.text
    assert [v["transaction_date"][:10] for v in listado.json()] == [borde.isoformat()]


# ── E0-A-8: /expenses/summary, el listado y el conteo por defecto ───────────


async def test_resumen_listado_y_conteo_de_gastos_a_las_2230_usan_el_dia_ar(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    borde = _DIA - timedelta(days=30)
    db_session.add(_gasto(sample_tenant, borde, "1800"))
    await db_session.commit()

    with reloj_utc(_AR_2230, _MODULOS):
        resumen = await client.get("/api/v1/expenses/summary", headers=auth_headers)
        listado = await client.get("/api/v1/expenses", headers=auth_headers)
        conteo = await client.get("/api/v1/expenses/count", headers=auth_headers)

    assert resumen.status_code == 200, resumen.text
    cuerpo = resumen.json()
    assert cuerpo["period_covered"] == f"{borde} al {_DIA}"
    assert cuerpo["entry_count"] == 1
    assert float(cuerpo["total_ars"]) == 1800.0
    assert listado.status_code == 200, listado.text
    assert [g["transaction_date"][:10] for g in listado.json()] == [borde.isoformat()]
    assert conteo.status_code == 200, conteo.text
    assert conteo.json()["total"] == 1


# ── E0-A-9 / D-7: la ventana de 30 días del chat (DeterministicFinance) ─────


async def test_deterministic_finance_a_las_2230_cierra_hoy_ar(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    borde = _DIA - timedelta(days=30)
    db_session.add_all([_venta(sample_tenant, borde, "900"), _gasto(sample_tenant, borde, "200")])
    await db_session.commit()
    tid = sample_tenant.tenant_id

    with reloj_utc(_AR_2230, _MODULOS):
        flujo = await deterministic_finance.calcular_flujo_neto_30d(tid, db_session)
        ticket = await deterministic_finance.calcular_ticket_promedio(tid, db_session)

    assert (flujo["desde"], flujo["hasta"]) == (borde.isoformat(), _DIA.isoformat())
    assert flujo["total_ventas"] == Decimal("900")
    assert flujo["total_gastos"] == Decimal("200")
    assert ticket["n_transacciones"] == 1


# ── E0-A-11: aviso de fecha futura del ValidationGate ───────────────────────


def test_validation_gate_avisa_pasado_manana_ar_a_las_2230() -> None:
    pasado_manana = _DIA + timedelta(days=2)
    resumen = {
        "file_type": "spreadsheet", "confidence": "HIGH", "inferred_type": "ventas",
        "has_venta": True, "has_fecha": True, "has_gasto": False, "has_producto": False,
        "ventas_detectadas": [{"fecha": pasado_manana.strftime("%d/%m/%Y"), "monto": "50000"}],
        "gastos_detectados": [], "stock_detectado": [], "warnings": [],
    }
    with reloj_utc(_AR_2230, _MODULOS):
        resultado = ValidationGate().validate(resumen)
    assert resultado.passed
    assert resultado.corrected_summary is not None
    assert f"future_date_warning:{pasado_manana.isoformat()}" in (
        resultado.corrected_summary["warnings"]
    )


# ── E0-A-12: clientes inactivos ─────────────────────────────────────────────


async def test_un_cliente_que_compro_hace_60_dias_ar_no_es_inactivo_todavia(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    cliente = Customer(tenant_id=sample_tenant.tenant_id, name="Compró al borde")
    db_session.add(cliente)
    await db_session.flush()
    db_session.add(
        _venta(sample_tenant, _DIA - timedelta(days=60), "1000", customer_id=cliente.id)
    )
    await db_session.commit()

    with reloj_utc(_AR_2230, _MODULOS):
        inactivos = await CustomerRepository(db_session).get_inactive_customers(
            sample_tenant.tenant_id, days=60
        )
    assert str(cliente.id) not in {c["customer_id"] for c in inactivos}


# ── E0-A-14: ventana del dashboard de marketing ─────────────────────────────


async def test_el_dashboard_de_marketing_incluye_la_metrica_del_borde_ar(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    db_session.add(SocialMetric(
        tenant_id=sample_tenant.tenant_id, platform="instagram",
        metric_date=_DIA - timedelta(days=30), followers=10, reach=300, engagement=30,
        ads_spend_ars=Decimal("0"),
    ))
    await db_session.commit()

    with reloj_utc(_AR_2230, _MODULOS):
        tablero = await MarketingService(db_session).get_dashboard(
            sample_tenant.tenant_id, days=30
        )
    assert tablero.has_data is True
    (instagram,) = tablero.platforms
    assert instagram.reach == 300
