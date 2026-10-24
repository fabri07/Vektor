"""B9 — los lectores del pago mixto, contra PostgreSQL real.

`test_pago_mixto_lectores.py` prueba los importes sobre SQLite. Este repite los
casos cuya FORMA de SQL puede diferir entre motores: el `HAVING count(DISTINCT)`
que define qué operación es mixta, la subconsulta de procedencia de una fila por
operación, `func.date` sobre `operation_date` y la comparación
timestamp ≥ date de la ventana del health score. Ver
``[[feedback_sqlite_masks_postgres]]``.

Cada test abre y cierra su propia sesión: una transacción que sobrevive al test
cuelga los `CREATE INDEX CONCURRENTLY` de otros tests de la suite `postgres`.

Gating: se skippea sin ``TEST_PG_DSN``.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.application.services.facts_provider import load_facts_frames
from app.domain.verticals import Vertical
from app.persistence.models.business import BusinessProfile
from app.persistence.models.customer import Customer
from app.persistence.models.pos_operation import PosOperation
from app.persistence.models.tenant import Tenant
from app.persistence.models.transaction import SaleEntry
from app.persistence.repositories._pos_tender_scope import tenders_mixtos
from app.persistence.repositories.transaction_repository import SaleRepository
from app.state.business_state_service import compute_business_state
from app.tests.integration.test_pago_mixto_lectores import (
    HOY,
    MEDIODIA,
    _FakeRedis,
    _operacion,
    _por_metodo,
)

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]


_Negocio = tuple[async_sessionmaker[AsyncSession], uuid.UUID, uuid.UUID]


@pytest_asyncio.fixture
async def negocio() -> AsyncGenerator[_Negocio, None]:
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    async with sm() as s:
        s.add(Tenant(tenant_id=tenant_id, legal_name="Mixto", display_name="Mixto"))
        await s.flush()
        s.add(
            BusinessProfile(
                profile_id=uuid.uuid4(),
                tenant_id=tenant_id,
                vertical_code=Vertical.KIOSCO_ALMACEN.value,
                data_mode="M0",
                data_confidence="LOW",
                onboarding_completed=False,
            )
        )
        cliente = Customer(tenant_id=tenant_id, name="Rosa Fiada")
        s.add(cliente)
        await s.commit()
        cliente_id = cliente.id
    try:
        yield sm, tenant_id, cliente_id
    finally:
        async with sm() as s:
            await s.execute(delete(PosOperation).where(PosOperation.tenant_id == tenant_id))
            await s.execute(delete(SaleEntry).where(SaleEntry.tenant_id == tenant_id))
            await s.execute(delete(Customer).where(Customer.tenant_id == tenant_id))
            await s.execute(delete(BusinessProfile).where(BusinessProfile.tenant_id == tenant_id))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
            await s.commit()
        await engine.dispose()


async def test_los_lectores_reparten_el_mixto_en_postgres(negocio: _Negocio) -> None:
    sm, tid, cliente_id = negocio
    async with sm() as s:
        # Tres líneas y dos pagos: unir con las líneas daría seis filas.
        await _operacion(
            s, tid, lineas=["500", "300", "200"],
            tenders=[("cash", "600"), ("account", "400")], customer_id=cliente_id,
        )
        # Dos pagos del mismo método: NO es mixto.
        await _operacion(s, tid, lineas=["1000"], tenders=[("cash", "300"), ("cash", "700")])
        # Anulada: no suma en ningún lector.
        await _operacion(
            s, tid, lineas=["1000"], tenders=[("cash", "1"), ("account", "999")],
            customer_id=cliente_id, status="VOIDED",
        )
        await _operacion(
            s, tid, lineas=["1000"], tenders=[("debit_card", "300"), ("credit_card", "700")]
        )
        s.add(
            SaleEntry(
                tenant_id=tid, amount=Decimal("400"), quantity=1, transaction_date=MEDIODIA,
                payment_method="inflow", customer_id=cliente_id,
            )
        )
        await s.commit()

    async with sm() as s:
        b = tenders_mixtos(tid, HOY, HOY)
        filas = (await s.execute(select(b.c.payment_method, b.c.amount_ars))).all()
        assert sorted((m, float(a)) for m, a in filas) == [
            ("account", 400.0), ("cash", 600.0), ("credit_card", 700.0), ("debit_card", 300.0),
        ]

        repo = SaleRepository(s)
        desglose = _por_metodo(await repo.cash_breakdown_by_method(tid, HOY, HOY))
        assert desglose == {
            "cash": 1600.0, "account": 400.0, "debit_card": 300.0,
            "credit_card": 700.0, "inflow": 400.0,
        }

        # La parte fiada del mixto (400) ya se cobró (inflow 400): no debe nada (T1).
        assert await repo.get_receivables_by_customer(tid) == []
        saldo = await repo.get_balance_by_customer(tid, cliente_id)
        assert saldo == {"total_account": 400.0, "total_paid": 400.0, "balance": 0.0}
        (fila,) = await repo.get_balances_by_customer(tid)
        assert (fila["total_account"], fila["total_paid"], fila["balance"]) == (400.0, 400.0, 0.0)

        frames = await load_facts_frames(s, tid, start=HOY, end=HOY)
        assert sorted(
            zip(frames.mixed_tenders["payment_method"], frames.mixed_tenders["amount_ars"],
                strict=True)
        ) == [("account", 400.0), ("cash", 600.0), ("credit_card", 700.0), ("debit_card", 300.0)]

        state = await compute_business_state(tid, s, _FakeRedis())  # type: ignore[arg-type]
        # Líquido: 1000 del mixto-no-mixto en efectivo + 600 + 300 de débito + el
        # cobro de fiado (400, T1: es plata que entra). Ni el fiado (400) ni el
        # crédito (700) entran.
        assert state.liquid_inflow_est == Decimal("2300")
