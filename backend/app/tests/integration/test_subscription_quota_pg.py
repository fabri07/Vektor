"""Cupos por suscripción, contra Postgres real: la reserva bajo concurrencia.

Por qué acá y no en la suite de SQLite
--------------------------------------
El contrato es que, con una sola unidad de cupo disponible, dos operaciones
DISTINTAS que reservan a la vez nunca ganan las dos — lo garantiza el
`INSERT ... ON CONFLICT ... WHERE ... RETURNING` sobre
`subscription_quota_usage`, no una comprobación en memoria. Probarlo exige
dos transacciones REALES corriendo al mismo tiempo contra la misma base; en
SQLite en memoria cada test tiene su propia base y no hay concurrencia que
medir (mismo criterio que `test_operation_identity_pg.py`).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.application.services import subscription_billing_service as billing
from app.application.services import subscription_service as svc
from app.domain.subscription import QuotaExceeded, QuotaResource, SeatLimitExceeded
from app.persistence.models.tenant import (
    PlanDefinition,
    Subscription,
    SubscriptionPayment,
    SubscriptionQuotaReservation,
    SubscriptionQuotaUsage,
    Tenant,
)
from app.persistence.models.user import User

TEST_PG_DSN = os.environ.get("TEST_PG_DSN")

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not TEST_PG_DSN, reason="requiere PostgreSQL real (TEST_PG_DSN)"),
]


@pytest_asyncio.fixture(scope="module")
async def pg_engine() -> AsyncGenerator[AsyncEngine, None]:
    assert TEST_PG_DSN is not None
    engine = create_async_engine(TEST_PG_DSN, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def suscripcion(pg_engine: AsyncEngine) -> AsyncGenerator[Subscription, None]:
    """Un tenant + una suscripción ACTIVE con 1 solo cupo de IA — el caso límite."""
    factory = async_sessionmaker(pg_engine, expire_on_commit=False)
    tenant_id = uuid.uuid4()
    ahora = datetime.now(UTC)
    async with factory() as s:
        s.add(Tenant(tenant_id=tenant_id, legal_name="T", display_name="T"))
        await s.flush()
        sub = Subscription(
            subscription_id=uuid.uuid4(),
            tenant_id=tenant_id,
            plan_code="esencial",
            status="ACTIVE",
            seats_included=1,
            granted_ia_queries_per_month=1,
            granted_imports_per_month=1,
            granted_photo_pdf_reads_per_month=1,
            current_period_start=ahora - timedelta(days=1),
            current_period_end=ahora + timedelta(days=29),
        )
        s.add(sub)
        await s.commit()
        await s.refresh(sub)
    try:
        yield sub
    finally:
        async with factory() as s:
            await s.execute(
                delete(SubscriptionQuotaReservation).where(
                    SubscriptionQuotaReservation.tenant_id == tenant_id
                )
            )
            await s.execute(
                delete(SubscriptionQuotaUsage).where(
                    SubscriptionQuotaUsage.tenant_id == tenant_id
                )
            )
            await s.execute(
                delete(SubscriptionPayment).where(SubscriptionPayment.tenant_id == tenant_id)
            )
            await s.execute(delete(User).where(User.tenant_id == tenant_id))
            await s.execute(delete(Subscription).where(Subscription.tenant_id == tenant_id))
            await s.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
            await s.commit()


async def _reservar(
    factory: async_sessionmaker[AsyncSession],
    subscription_id: uuid.UUID,
    tenant_id: uuid.UUID,
    *,
    operation_id: str,
    espera: float,
) -> str:
    """Recarga la `Subscription` en SU PROPIA sesión — hace falta una instancia
    propia por tarea, no la misma fila de ORM compartida entre corrutinas."""
    async with factory() as session:
        sub = (
            await session.execute(
                select(Subscription).where(Subscription.subscription_id == subscription_id)
            )
        ).scalar_one()
        await asyncio.sleep(espera)
        await svc.reserve(
            session,
            subscription=sub,
            resource=QuotaResource.IA_QUERY,
            operation_id=operation_id,
        )
        return "reservó"


async def test_dos_operaciones_distintas_pelean_la_ultima_unidad(
    pg_engine: AsyncEngine, suscripcion: Subscription
) -> None:
    """Con 1 solo cupo, dos `operation_id` DISTINTOS reservando a la vez: una
    gana, la otra ve `QuotaExceeded` — nunca las dos, nunca ninguna que reviente
    con otra cosa."""
    factory = async_sessionmaker(pg_engine, expire_on_commit=False)

    resultados = await asyncio.gather(
        _reservar(
            factory, suscripcion.subscription_id, suscripcion.tenant_id,
            operation_id="op-a", espera=0.15,
        ),
        _reservar(
            factory, suscripcion.subscription_id, suscripcion.tenant_id,
            operation_id="op-b", espera=0.15,
        ),
        return_exceptions=True,
    )
    ganadoras = [r for r in resultados if r == "reservó"]
    agotadas = [r for r in resultados if isinstance(r, QuotaExceeded)]
    otros_fallos = [
        r for r in resultados if isinstance(r, BaseException) and not isinstance(r, QuotaExceeded)
    ]
    assert not otros_fallos, f"ninguna de las dos puede reventar con otra cosa: {otros_fallos}"
    assert len(ganadoras) == 1, f"tiene que ganar exactamente una: {resultados}"
    assert len(agotadas) == 1, f"la otra tiene que ver QuotaExceeded: {resultados}"

    factory2 = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with factory2() as s:
        fila = (
            await s.execute(
                select(SubscriptionQuotaUsage).where(
                    SubscriptionQuotaUsage.tenant_id == suscripcion.tenant_id
                )
            )
        ).scalar_one()
        assert (fila.used, fila.reserved) == (0, 1), (
            "la reserva perdedora no puede haber dejado rastro en el contador"
        )
        reservas = (
            await s.execute(
                select(SubscriptionQuotaReservation).where(
                    SubscriptionQuotaReservation.tenant_id == suscripcion.tenant_id
                )
            )
        ).scalars().all()
        assert len(reservas) == 1, (
            "la reserva que perdió el cupo se borra — no queda como RELEASED "
            "(nunca tuvo capacidad real)"
        )
        assert reservas[0].state == "RESERVED"


async def test_reintentar_la_misma_operacion_durante_la_concurrencia_no_duplica(
    pg_engine: AsyncEngine, suscripcion: Subscription
) -> None:
    """Dos llamadas con el MISMO `operation_id` a la vez (reintento genuino de
    red, no un segundo cliente): las dos tienen que terminar bien, y el cupo
    se gasta una sola vez."""
    factory = async_sessionmaker(pg_engine, expire_on_commit=False)

    resultados = await asyncio.gather(
        _reservar(
            factory, suscripcion.subscription_id, suscripcion.tenant_id,
            operation_id="op-repetido", espera=0.1,
        ),
        _reservar(
            factory, suscripcion.subscription_id, suscripcion.tenant_id,
            operation_id="op-repetido", espera=0.1,
        ),
        return_exceptions=True,
    )
    fallos = [r for r in resultados if isinstance(r, BaseException)]
    assert not fallos, f"un reintento genuino nunca debería fallar: {fallos}"

    async with factory() as s:
        fila = (
            await s.execute(
                select(SubscriptionQuotaUsage).where(
                    SubscriptionQuotaUsage.tenant_id == suscripcion.tenant_id
                )
            )
        ).scalar_one()
        assert fila.reserved == 1, "el mismo operation_id nunca reserva dos unidades"


# ── Plazas: bloquear → contar → crear ─────────────────────────────────────────


async def _alta_de_usuario(
    factory: async_sessionmaker[AsyncSession], subscription_id: uuid.UUID, tenant_id: uuid.UUID
) -> str:
    """Un alta completa en SU transacción: límite, insert y commit juntos —
    el lock de `enforce_seat_limit` vive hasta ese commit."""
    async with factory() as s:
        sub = await s.get(Subscription, subscription_id)
        assert sub is not None

        async def _activos() -> int:
            return (
                await s.execute(
                    select(func.count())
                    .select_from(User)
                    .where(User.tenant_id == tenant_id, User.is_active.is_(True))
                )
            ).scalar_one()

        try:
            await svc.enforce_seat_limit(s, subscription=sub, count_active_users=_activos)
        except SeatLimitExceeded:
            await s.rollback()
            return "sin_plaza"
        # Ventana para que la otra alta llegue al lock mientras esta lo tiene.
        await asyncio.sleep(0.2)
        s.add(
            User(
                user_id=uuid.uuid4(),
                tenant_id=tenant_id,
                email=f"u+{uuid.uuid4().hex[:10]}@test.com",
                full_name="Alta concurrente",
                password_hash="x",
                role_code="VIEWER",
                is_active=True,
            )
        )
        await s.commit()
        return "alta"


async def test_dos_altas_simultaneas_por_la_ultima_plaza_entra_una_sola(
    pg_engine: AsyncEngine, suscripcion: Subscription
) -> None:
    """`seats_included=1`, cero usuarios: con el conteo hecho ANTES del lock
    las dos leían 0 y entraban las dos."""
    factory = async_sessionmaker(pg_engine, expire_on_commit=False)
    resultados = await asyncio.gather(
        _alta_de_usuario(factory, suscripcion.subscription_id, suscripcion.tenant_id),
        _alta_de_usuario(factory, suscripcion.subscription_id, suscripcion.tenant_id),
    )
    assert sorted(resultados) == ["alta", "sin_plaza"]


# ── Pagos: la misma referencia, dos veces a la vez ────────────────────────────


async def _renovar(
    factory: async_sessionmaker[AsyncSession], subscription_id: uuid.UUID, referencia: str
) -> tuple[bool, datetime]:
    async with factory() as s:
        r = await billing.apply_payment(
            s,
            subscription_id=subscription_id,
            expected="renew",
            reference=referencia,
            amount_usd=Decimal("12"),
            amount_ars=Decimal("18500"),
            operator="test-concurrente",
        )
        await asyncio.sleep(0.2)  # que la otra llegue mientras esta tiene el lock
        await s.commit()
        return r.ya_aplicado, r.period_end


async def test_dos_pagos_simultaneos_con_la_misma_referencia_otorgan_un_solo_periodo(
    pg_engine: AsyncEngine, suscripcion: Subscription
) -> None:
    """Buscar la referencia antes de escribir (lo que hacía `activate`) deja
    pasar a las dos. El candado es el UNIQUE + el lock de la suscripción."""
    factory = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with factory() as s:
        assert await s.get(PlanDefinition, "esencial") is not None  # seed de la migración
    referencia = f"T-{uuid.uuid4().hex[:12]}"

    resultados = await asyncio.gather(
        _renovar(factory, suscripcion.subscription_id, referencia),
        _renovar(factory, suscripcion.subscription_id, referencia),
    )

    assert sorted(ya for ya, _ in resultados) == [False, True]
    assert resultados[0][1] == resultados[1][1]  # las dos informan EL MISMO período
    async with factory() as s:
        pagos = (
            await s.execute(
                select(func.count())
                .select_from(SubscriptionPayment)
                .where(SubscriptionPayment.reference == referencia)
            )
        ).scalar_one()
        sub = await s.get(Subscription, suscripcion.subscription_id)
    assert pagos == 1
    assert sub is not None and sub.next_period_end is not None  # un período, no dos
