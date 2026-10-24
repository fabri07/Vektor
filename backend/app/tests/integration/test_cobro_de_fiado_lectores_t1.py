"""T1 — una venta fiada y su cobro son UN ingreso, una entrada de caja y deuda 0.

Revisión profunda 2026-10-24: los cobros de fiado (`payment_method="inflow"`)
se sumaban como venta en el Resumen económico, el BSL, el chat y la proyección
de caja, así que una venta fiada de $1.000 cobrada después contaba $2.000 de
ingreso, y la deuda del chat no restaba lo cobrado.

La venta fiada entra por `POST /sales` (el camino de la app) y el cobro por
`cash_service.save_cash_inflow`, el writer que ejecuta el chat al aprobar
REGISTER_CASH_INFLOW. Cada lector se afirma por separado, con el cobro total y
con dos cobros parciales.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.shared.analytics import summarize_receivables
from app.application.services.cash_service import save_cash_inflow
from app.application.services.deterministic_finance import calcular_flujo_neto_30d
from app.application.services.forecast_service import ingresos_de_caja_por_dia
from app.domain.business_time import today_ar
from app.persistence.models.business import BusinessProfile
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.repositories.transaction_repository import SaleRepository
from app.state.business_state_service import _hash_key, compute_business_state
from app.tests._respaldo import producto_con_compra_http
from app.tests.integration.test_pago_mixto_lectores import _operacion

HOY = today_ar()
DIA_VENTA = HOY - timedelta(days=10)
DIA_PRIMER_COBRO = HOY - timedelta(days=5)


class _FakeRedis:
    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._store[key] = value


@pytest.fixture(autouse=True)
def _sin_celery(mock_score_trigger: Any) -> None:
    pass


async def _venta_fiada(client: AsyncClient, headers: dict[str, str]) -> uuid.UUID:
    """Venta fiada de $1.000 a un cliente real, por `POST /sales`."""
    cliente = await client.post(
        "/api/v1/customers",
        json={
            "name": "Rosa",
            "customer_type": "person",
            "last_name": "Fiada",
            "dni": "30111222",
            "phone": "+54 11 5555-1111",
        },
        headers=headers,
    )
    assert cliente.status_code == 201, cliente.text
    cid = cliente.json()["id"]
    venta = await client.post(
        "/api/v1/sales",
        json={
            "product_id": await producto_con_compra_http(client, headers),
            "amount": "1000.00",
            "quantity": 1,
            "transaction_date": DIA_VENTA.isoformat(),
            "payment_method": "account",
            "customer_id": cid,
        },
        headers=headers,
    )
    assert venta.status_code == 201, venta.text
    return uuid.UUID(cid)


async def _cobro(
    db: AsyncSession, tenant_id: uuid.UUID, cliente: uuid.UUID, importe: str, dia: date
) -> None:
    """El cobro tal como lo escribe el chat, fechado ``dia``."""
    entry = await save_cash_inflow(
        {"amount": importe, "customer_id": str(cliente)}, tenant_id, db
    )
    entry.transaction_date = datetime.combine(dia, time(11, 0))
    await db.commit()


async def _resumen(client: AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    resp = await client.get(
        "/api/v1/economic-summary",
        params={"from_date": DIA_VENTA.isoformat(), "to_date": HOY.isoformat()},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    data: dict[str, Any] = resp.json()
    return data


async def _ventas_del_bsl_esperadas(db: AsyncSession, tenant_id: uuid.UUID) -> Decimal:
    """`monthly_sales_est` con UNA venta de $1.000 en un solo día (< 10 ventas):
    70 % del estimado del onboarding + 30 % de la proyección (1.000 × 30 / 1)."""
    estimado = (
        await db.execute(
            select(BusinessProfile.monthly_sales_estimate_ars).where(
                BusinessProfile.tenant_id == tenant_id
            )
        )
    ).scalar_one() or Decimal("0")
    return estimado * Decimal("0.7") + Decimal("30000") * Decimal("0.3")


# ── Cobro total ──────────────────────────────────────────────────────────────


async def test_cobro_total_un_ingreso_una_entrada_de_caja_y_deuda_cero(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    cliente = await _venta_fiada(client, auth_headers)
    await _cobro(db_session, tid, cliente, "1000", HOY)
    repo = SaleRepository(db_session)

    # Resumen económico: $1.000 de ingreso (antes 2.000).
    resumen = await _resumen(client, auth_headers)
    assert resumen["total_income_ars"] == 1000

    # Caja: la venta fiada no entra; el cobro entra el día que se cobra.
    caja = await ingresos_de_caja_por_dia(tid, db_session, DIA_VENTA)
    assert caja == {HOY: 1000.0}

    # Deuda: el cliente ya no debe nada.
    assert await repo.get_receivables_by_customer(tid) == []
    assert summarize_receivables([])["total_owed"] == 0.0
    saldo = await repo.get_balance_by_customer(tid, cliente)
    assert saldo["balance"] == 0.0

    # Chat: ventas, facturación por cliente y serie diaria cuentan UNA venta.
    flujo = await calcular_flujo_neto_30d(tid, db_session)
    assert flujo["total_ventas"] == Decimal("1000")
    (por_cliente,) = await repo.get_sales_by_customer(tid)
    assert (por_cliente["total"], por_cliente["n_sales"]) == (1000.0, 1)
    assert sum(await repo.get_daily_revenue_series(tid, days=30)) == 1000.0

    # BSL: la venta cuenta una vez y el cobro es flujo líquido.
    state = await compute_business_state(tid, db_session, _FakeRedis())  # type: ignore[arg-type]
    assert state.liquid_inflow_est == Decimal("1000")
    assert state.monthly_sales_est == await _ventas_del_bsl_esperadas(db_session, tid)


# ── Cobros parciales ─────────────────────────────────────────────────────────


async def test_cobros_parciales_bajan_la_deuda_y_entran_a_caja_el_dia_de_cada_uno(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    cliente = await _venta_fiada(client, auth_headers)
    repo = SaleRepository(db_session)

    await _cobro(db_session, tid, cliente, "400", DIA_PRIMER_COBRO)
    (deudor,) = await repo.get_receivables_by_customer(tid)
    assert (deudor["customer_id"], deudor["total_owed"]) == (str(cliente), 600.0)
    # n_sales cuenta los movimientos de deuda, no los cobros.
    assert deudor["n_sales"] == 1
    assert (await _resumen(client, auth_headers))["total_income_ars"] == 1000

    await _cobro(db_session, tid, cliente, "600", HOY)
    assert await repo.get_receivables_by_customer(tid) == []
    assert (await _resumen(client, auth_headers))["total_income_ars"] == 1000
    caja = await ingresos_de_caja_por_dia(tid, db_session, DIA_VENTA)
    assert caja == {DIA_PRIMER_COBRO: 400.0, HOY: 600.0}

    flujo = await calcular_flujo_neto_30d(tid, db_session)
    assert flujo["total_ventas"] == Decimal("1000")
    state = await compute_business_state(tid, db_session, _FakeRedis())  # type: ignore[arg-type]
    assert state.liquid_inflow_est == Decimal("1000")
    assert state.monthly_sales_est == await _ventas_del_bsl_esperadas(db_session, tid)


async def test_un_cobro_parcial_deja_deuda_y_no_suma_ingreso(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    cliente = await _venta_fiada(client, auth_headers)
    await _cobro(db_session, tid, cliente, "250", HOY)
    repo = SaleRepository(db_session)

    assert (await _resumen(client, auth_headers))["total_income_ars"] == 1000
    (deudor,) = await repo.get_receivables_by_customer(tid)
    assert deudor["total_owed"] == 750.0
    assert summarize_receivables([deudor])["total_owed"] == 750.0
    assert await ingresos_de_caja_por_dia(tid, db_session, DIA_VENTA) == {HOY: 250.0}


# ── Lo que sigue siendo caja y lo que no ─────────────────────────────────────


async def test_la_caja_proyectada_saca_el_fiado_y_cuenta_el_resto(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Toda venta menos el fiado, como la resta de FactsService. La tarjeta de
    crédito se sigue contando el día de la venta (correrla es otra decisión) y
    `other` (un medio que el import no reconoce) también: antes de T1 contaba."""
    tid = sample_tenant.tenant_id
    mediodia = datetime.combine(HOY, time(12, 0))
    for metodo, importe in (
        ("cash", "100"),
        ("transfer", "200"),
        ("qr", "300"),
        ("debit_card", "400"),
        ("credit_card", "5000"),
        ("other", "50"),
        ("account", "7000"),
    ):
        db_session.add(
            SaleEntry(
                tenant_id=tid,
                amount=Decimal(importe),
                quantity=1,
                transaction_date=mediodia,
                payment_method=metodo,
                provenance="REAL",
            )
        )
    # Pago mixto de caja (B9): entra lo no fiado (150 + 50), nunca la línea entera.
    await _operacion(
        db_session,
        tid,
        lineas=["300"],
        tenders=[("cash", "150"), ("qr", "50"), ("account", "100")],
    )
    await db_session.commit()
    assert await ingresos_de_caja_por_dia(tid, db_session, DIA_VENTA) == {HOY: 6250.0}


async def test_un_cobro_de_otro_negocio_no_baja_la_deuda(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    from app.persistence.models.tenant import Tenant as TenantModel  # noqa: PLC0415

    tid = sample_tenant.tenant_id
    cliente = await _venta_fiada(client, auth_headers)
    otro = TenantModel(tenant_id=uuid.uuid4(), legal_name="Otro SRL", display_name="Otro")
    db_session.add(otro)
    await db_session.flush()
    # El mismo customer_id con un cobro escrito bajo OTRO tenant no cuenta.
    await _cobro(db_session, otro.tenant_id, cliente, "1000", HOY)

    (deudor,) = await SaleRepository(db_session).get_receivables_by_customer(tid)
    assert deudor["total_owed"] == 1000.0


async def test_el_cobro_invalida_el_estado_cacheado_del_bsl(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """Un cobro no cambia las ventas pero sí la caja: el caché tiene que caer.
    Antes de T1 lo invalidaba de rebote el conteo de ventas, que contaba el cobro."""
    tid = sample_tenant.tenant_id
    redis = _FakeRedis()
    cliente = await _venta_fiada(client, auth_headers)
    # Dos lecturas: la primera puede ver la venta sin costear todavía (el costeo
    # corre al commit); la segunda es el estado estable que queda cacheado.
    await compute_business_state(tid, db_session, redis)  # type: ignore[arg-type]
    await db_session.commit()
    antes = await compute_business_state(tid, db_session, redis)  # type: ignore[arg-type]
    assert antes.liquid_inflow_est == Decimal("0")
    huella = redis._store[_hash_key(tid)]
    assert (await compute_business_state(tid, db_session, redis)) is not None  # type: ignore[arg-type]
    assert redis._store[_hash_key(tid)] == huella  # sin cambios, sigue cacheado

    await _cobro(db_session, tid, cliente, "1000", HOY)
    despues = await compute_business_state(tid, db_session, redis)  # type: ignore[arg-type]
    assert despues.liquid_inflow_est == Decimal("1000")
    assert despues.monthly_sales_est == antes.monthly_sales_est


async def test_resumen_de_ventas_cuenta_la_venta_y_no_el_cobro(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    cliente = await _venta_fiada(client, auth_headers)
    await _cobro(db_session, sample_tenant.tenant_id, cliente, "1000", HOY)
    resp = await client.get("/api/v1/sales/summary", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert (Decimal(str(data["total_ars"])), data["entry_count"]) == (Decimal("1000"), 1)


async def test_un_cobro_mayor_que_la_deuda_no_aparece_como_deudor(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    tid = sample_tenant.tenant_id
    cliente = await _venta_fiada(client, auth_headers)
    await _cobro(db_session, tid, cliente, "1500", HOY)
    repo = SaleRepository(db_session)
    assert await repo.get_receivables_by_customer(tid) == []
    # El saldo a favor sigue visible donde se muestra el saldo.
    assert (await repo.get_balance_by_customer(tid, cliente))["balance"] == -500.0
    assert (await _resumen(client, auth_headers))["total_income_ars"] == 1000


async def test_el_sobrante_del_arqueo_es_caja_y_no_ingreso(
    client: AsyncClient,
    auth_headers: dict[str, str],
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """El sobrante «como ingreso» del arqueo se escribe como `inflow` sin cliente
    (`cash_close_service`): entra a caja, no es venta ni baja ninguna deuda."""
    tid = sample_tenant.tenant_id
    cliente = await _venta_fiada(client, auth_headers)
    sobrante = await save_cash_inflow({"amount": "300"}, tid, db_session)
    sobrante.transaction_date = datetime.combine(HOY, time(20, 0))
    await db_session.commit()

    assert (await _resumen(client, auth_headers))["total_income_ars"] == 1000
    assert await ingresos_de_caja_por_dia(tid, db_session, DIA_VENTA) == {HOY: 300.0}
    (deudor,) = await SaleRepository(db_session).get_receivables_by_customer(tid)
    assert (deudor["customer_id"], deudor["total_owed"]) == (str(cliente), 1000.0)
    state = await compute_business_state(tid, db_session, _FakeRedis())  # type: ignore[arg-type]
    assert state.liquid_inflow_est == Decimal("300")
