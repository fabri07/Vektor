"""B9 — los lectores de plata ven el pago mixto de caja con importes EXACTOS.

La escritura del mixto todavía está cerrada (422 `MIXED_TENDER_NOT_ENABLED`), así
que estas operaciones se siembran directo en la base con la forma que va a
escribir `POST /pos/operations`: líneas con `payment_method = "mixed"` y un
tender por medio. Los lectores tienen que estar listos ANTES de abrir la
escritura; si no, el primer mixto real sería plata que ningún arqueo imputa.

No alcanza con que un invariante cierre: si el mixto se leyera mal faltaría de
dos lados a la vez y la igualdad seguiría valiendo. Cada test afirma el importe
de cada medio por separado.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.cash_close_service import CashCloseService
from app.application.services.facts_provider import load_facts_frames
from app.domain.business_time import today_ar
from app.domain.transaction import MIXED_PAYMENT_METHOD
from app.persistence.models.business import BusinessProfile
from app.persistence.models.customer import Customer
from app.persistence.models.pos_operation import PosOperation, PosOperationLine, PosTender
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.repositories._pos_tender_scope import (
    operaciones_mixtas_con_procedencia_mezclada,
    tenders_mixtos,
)
from app.persistence.repositories.transaction_repository import SaleRepository
from app.state.business_state_service import compute_business_state

HOY = today_ar()
MEDIODIA = datetime.combine(HOY, time(12, 0))


async def _operacion(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    lineas: list[str],
    tenders: list[tuple[str, str]],
    customer_id: uuid.UUID | None = None,
    status: str = "COMPLETED",
    provenance: str | list[str] = "REAL",
    fecha: datetime = MEDIODIA,
) -> PosOperation:
    """Siembra una operación con la forma que escribe la caja.

    Las líneas llevan `mixed` sólo si hay más de un método DISTINTO: dos pagos
    en efectivo siguen siendo una venta de efectivo.
    """
    metodos = {m for m, _ in tenders}
    metodo_linea = next(iter(metodos)) if len(metodos) == 1 else MIXED_PAYMENT_METHOD
    total = sum(Decimal(x) for x in lineas)
    assert total == sum(Decimal(x) for _, x in tenders), "Σ tenders ≠ Σ líneas"
    procedencias = provenance if isinstance(provenance, list) else [provenance] * len(lineas)
    anulada = status == "VOIDED"

    op = PosOperation(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        client_operation_id=str(uuid.uuid4()),
        customer_id=customer_id,
        operation_date=fecha,
        subtotal_ars=total,
        total_ars=total,
        status=status,
    )
    session.add(op)
    for posicion, (importe, prov) in enumerate(zip(lineas, procedencias, strict=True)):
        venta = SaleEntry(
            tenant_id=tenant_id,
            amount=Decimal(importe),
            quantity=1,
            transaction_date=fecha,
            payment_method=metodo_linea,
            customer_id=customer_id,
            provenance=prov,
            voided_at=datetime.now(UTC) if anulada else None,
            void_reason="USER_CANCELLED" if anulada else None,
        )
        session.add(venta)
        await session.flush()
        session.add(
            PosOperationLine(
                tenant_id=tenant_id,
                operation_id=op.id,
                sale_entry_id=venta.id,
                position=posicion,
                quantity=1,
                unit_price_list=Decimal(importe),
                line_total_ars=Decimal(importe),
            )
        )
    for posicion, (metodo, importe) in enumerate(tenders):
        session.add(
            PosTender(
                tenant_id=tenant_id,
                operation_id=op.id,
                position=posicion,
                payment_method=metodo,
                amount_ars=Decimal(importe),
            )
        )
    await session.flush()
    return op


def _venta(tenant_id: uuid.UUID, importe: str, metodo: str, **extra: object) -> SaleEntry:
    return SaleEntry(
        tenant_id=tenant_id,
        amount=Decimal(importe),
        quantity=1,
        transaction_date=MEDIODIA,
        payment_method=metodo,
        provenance="REAL",
        **extra,
    )


@pytest_asyncio.fixture
async def cliente(db_session: AsyncSession, sample_tenant: Tenant) -> Customer:
    c = Customer(tenant_id=sample_tenant.tenant_id, name="Rosa Fiada")
    db_session.add(c)
    await db_session.flush()
    return c


def _por_metodo(filas: list[dict]) -> dict[str, float]:
    out: dict[str, float] = {}
    for f in filas:
        out[f["payment_method"]] = out.get(f["payment_method"], 0.0) + f["total"]
    return out


# ── El scope: una fila por tender, nunca por línea ───────────────────────────


async def test_tres_lineas_dos_pagos_son_dos_filas(
    db_session: AsyncSession, sample_tenant: Tenant, cliente: Customer
) -> None:
    await _operacion(
        db_session,
        sample_tenant.tenant_id,
        lineas=["500", "300", "200"],
        tenders=[("cash", "600"), ("account", "400")],
        customer_id=cliente.id,
    )
    b = tenders_mixtos(sample_tenant.tenant_id)
    filas = (await db_session.execute(select(b.c.payment_method, b.c.amount_ars))).all()
    # Unir tenders con líneas daría seis filas y el triple de plata.
    assert sorted((m, float(a)) for m, a in filas) == [("account", 400.0), ("cash", 600.0)]


async def test_dos_pagos_del_mismo_metodo_no_son_mixto(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    await _operacion(
        db_session,
        sample_tenant.tenant_id,
        lineas=["1000"],
        tenders=[("cash", "300"), ("cash", "700")],
    )
    b = tenders_mixtos(sample_tenant.tenant_id)
    assert (await db_session.execute(select(b))).all() == []
    filas = await SaleRepository(db_session).cash_breakdown_by_method(
        sample_tenant.tenant_id, HOY, HOY
    )
    # Un solo importe de $1.000 en efectivo: la línea, no los tenders.
    assert _por_metodo(filas) == {"cash": 1000.0}


# ── Desglose por método: arqueo del día e /insights/cash-breakdown ───────────


async def test_desglose_reparte_el_mixto_y_no_muestra_mixed(
    db_session: AsyncSession, sample_tenant: Tenant, cliente: Customer
) -> None:
    tid = sample_tenant.tenant_id
    db_session.add(_venta(tid, "100", "cash"))
    await _operacion(
        db_session,
        tid,
        lineas=["500", "300", "200"],
        tenders=[("cash", "600"), ("account", "400")],
        customer_id=cliente.id,
    )
    await _operacion(
        db_session, tid, lineas=["1000"], tenders=[("cash", "600"), ("debit_card", "400")]
    )
    await db_session.flush()

    filas = await SaleRepository(db_session).cash_breakdown_by_method(tid, HOY, HOY)
    assert _por_metodo(filas) == {"cash": 1300.0, "account": 400.0, "debit_card": 400.0}
    assert all(f["payment_method"] != MIXED_PAYMENT_METHOD for f in filas)


async def test_el_arqueo_del_dia_espera_solo_la_parte_en_efectivo(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    await _operacion(
        db_session, tid, lineas=["1000"], tenders=[("cash", "600"), ("debit_card", "400")]
    )
    total, por_metodo = await CashCloseService(db_session)._compute_expected(tid, HOY)
    assert por_metodo == {"cash": 600.0, "debit_card": 400.0}
    assert total == 1000.0


async def test_fuera_del_rango_no_suma(db_session: AsyncSession, sample_tenant: Tenant) -> None:
    tid = sample_tenant.tenant_id
    await _operacion(
        db_session,
        tid,
        lineas=["1000"],
        tenders=[("cash", "600"), ("debit_card", "400")],
        fecha=MEDIODIA - timedelta(days=2),
    )
    filas = await SaleRepository(db_session).cash_breakdown_by_method(tid, HOY, HOY)
    assert filas == []


# ── Deuda de clientes ────────────────────────────────────────────────────────


async def test_un_cliente_cuya_unica_deuda_es_mixta_aparece_en_los_tres(
    db_session: AsyncSession, sample_tenant: Tenant, cliente: Customer
) -> None:
    tid = sample_tenant.tenant_id
    await _operacion(
        db_session,
        tid,
        lineas=["500", "300", "200"],
        tenders=[("cash", "600"), ("account", "400")],
        customer_id=cliente.id,
    )
    repo = SaleRepository(db_session)

    deudores = await repo.get_receivables_by_customer(tid)
    assert deudores == [
        {
            "customer_id": str(cliente.id),
            "customer_name": "Rosa Fiada",
            "total_owed": 400.0,
            # Un movimiento: el pago fiado. No las tres líneas del ticket.
            "n_sales": 1,
        }
    ]
    saldo = await repo.get_balance_by_customer(tid, cliente.id)
    assert saldo == {"total_account": 400.0, "total_paid": 0.0, "balance": 400.0}
    saldos = await repo.get_balances_by_customer(tid)
    assert [(s["customer_id"], s["balance"]) for s in saldos] == [(str(cliente.id), 400.0)]


async def test_cobrar_la_parte_fiada_deja_el_saldo_en_cero(
    db_session: AsyncSession, sample_tenant: Tenant, cliente: Customer
) -> None:
    tid = sample_tenant.tenant_id
    await _operacion(
        db_session,
        tid,
        lineas=["1000"],
        tenders=[("cash", "600"), ("account", "400")],
        customer_id=cliente.id,
    )
    db_session.add(_venta(tid, "400", "inflow", customer_id=cliente.id))
    await db_session.flush()
    repo = SaleRepository(db_session)

    # Sin la población B esto daba −400.
    saldo = await repo.get_balance_by_customer(tid, cliente.id)
    assert saldo == {"total_account": 400.0, "total_paid": 400.0, "balance": 0.0}
    (fila,) = await repo.get_balances_by_customer(tid)
    assert (fila["total_account"], fila["total_paid"], fila["balance"]) == (400.0, 400.0, 0.0)


async def test_fiado_comun_y_mixto_del_mismo_cliente_se_suman(
    db_session: AsyncSession, sample_tenant: Tenant, cliente: Customer
) -> None:
    tid = sample_tenant.tenant_id
    db_session.add(_venta(tid, "250", "account", customer_id=cliente.id))
    await _operacion(
        db_session,
        tid,
        lineas=["1000"],
        tenders=[("cash", "600"), ("account", "400")],
        customer_id=cliente.id,
    )
    repo = SaleRepository(db_session)
    (deudor,) = await repo.get_receivables_by_customer(tid)
    assert (deudor["total_owed"], deudor["n_sales"]) == (650.0, 2)
    (fila,) = await repo.get_balances_by_customer(tid)
    assert (fila["total_account"], fila["n_sales"]) == (650.0, 2)


# ── Anulación y tenants ──────────────────────────────────────────────────────


async def test_un_mixto_anulado_no_suma_en_ningun_lector(
    db_session: AsyncSession, sample_tenant: Tenant, cliente: Customer
) -> None:
    tid = sample_tenant.tenant_id
    await _operacion(
        db_session,
        tid,
        lineas=["500", "500"],
        tenders=[("cash", "600"), ("account", "400")],
        customer_id=cliente.id,
        status="VOIDED",
    )
    repo = SaleRepository(db_session)
    assert await repo.cash_breakdown_by_method(tid, HOY, HOY) == []
    assert await repo.get_receivables_by_customer(tid) == []
    assert (await repo.get_balance_by_customer(tid, cliente.id))["balance"] == 0.0
    assert await repo.get_balances_by_customer(tid) == []


async def test_el_mixto_de_otro_negocio_no_se_filtra(
    db_session: AsyncSession, sample_tenant: Tenant, second_tenant: Tenant
) -> None:
    otro = Customer(tenant_id=second_tenant.tenant_id, name="Ajeno")
    db_session.add(otro)
    await db_session.flush()
    await _operacion(
        db_session,
        second_tenant.tenant_id,
        lineas=["1000"],
        tenders=[("cash", "600"), ("account", "400")],
        customer_id=otro.id,
    )
    repo = SaleRepository(db_session)
    tid = sample_tenant.tenant_id
    assert await repo.cash_breakdown_by_method(tid, HOY, HOY) == []
    assert await repo.get_receivables_by_customer(tid) == []
    assert await repo.get_balances_by_customer(tid) == []
    assert (await repo.get_balance_by_customer(tid, otro.id))["total_account"] == 0.0


# ── Procedencia ──────────────────────────────────────────────────────────────


async def test_procedencia_mezclada_no_se_inventa_y_se_detecta(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    tid = sample_tenant.tenant_id
    await _operacion(
        db_session,
        tid,
        lineas=["500", "500"],
        tenders=[("cash", "600"), ("debit_card", "400")],
        provenance=["REAL", "DEMO"],
    )
    b = tenders_mixtos(tid)
    assert (await db_session.execute(select(b))).all() == []
    mezcladas = (
        await db_session.execute(operaciones_mixtas_con_procedencia_mezclada(tid))
    ).scalar_one()
    assert mezcladas == 1


# ── FactsService: el provider real trae los tenders con su procedencia ───────


async def test_el_provider_de_facts_trae_los_tenders_mixtos_vivos(
    db_session: AsyncSession, sample_tenant: Tenant, cliente: Customer
) -> None:
    tid = sample_tenant.tenant_id
    await _operacion(
        db_session,
        tid,
        lineas=["500", "300", "200"],
        tenders=[("cash", "600"), ("account", "400")],
        customer_id=cliente.id,
    )
    await _operacion(
        db_session,
        tid,
        lineas=["1000"],
        tenders=[("cash", "500"), ("credit_card", "500")],
        provenance="DEMO",
    )
    await _operacion(
        db_session, tid, lineas=["1000"], tenders=[("cash", "1"), ("qr", "999")], status="VOIDED"
    )
    frames = await load_facts_frames(db_session, tid, start=HOY, end=HOY)
    m = frames.mixed_tenders
    assert sorted(zip(m["payment_method"], m["amount_ars"], m["provenance"], strict=True)) == [
        ("account", 400.0, "REAL"),
        ("cash", 500.0, "DEMO"),
        ("cash", 600.0, "REAL"),
        ("credit_card", 500.0, "DEMO"),
    ]
    # Las ventas de esas operaciones vienen enteras en `sales`, marcadas.
    ventas = frames.sales
    marcadas = ventas.loc[ventas["payment_method"] == MIXED_PAYMENT_METHOD, "amount_ars"]
    assert float(marcadas.sum()) == 2000.0


# ── Health score: flujo líquido ──────────────────────────────────────────────


class _FakeRedis:
    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self._store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._store[key] = value


async def test_el_flujo_liquido_suma_solo_la_parte_liquida_del_mixto(
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_business_profile: BusinessProfile,
    cliente: Customer,
) -> None:
    tid = sample_tenant.tenant_id
    await _operacion(
        db_session,
        tid,
        lineas=["600", "400"],
        tenders=[("cash", "500"), ("debit_card", "300"), ("account", "200")],
        customer_id=cliente.id,
    )
    await _operacion(
        db_session, tid, lineas=["1000"], tenders=[("qr", "400"), ("credit_card", "600")]
    )
    state = await compute_business_state(tid, db_session, _FakeRedis())  # type: ignore[arg-type]
    # 500 + 300 + 400: ni el fiado (200) ni el crédito (600) son plata de hoy.
    assert state.liquid_inflow_est == Decimal("1200")
