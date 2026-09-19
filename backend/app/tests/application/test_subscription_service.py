"""`subscription_service` — reserve/commit/release, sin concurrencia real.

La concurrencia real (dos operaciones DISTINTAS peleando la última unidad)
está en `integration/test_subscription_quota_pg.py` — necesita Postgres de
verdad, no la base en memoria de esta suite.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import subscription_service as svc
from app.domain.subscription import (
    QuotaExceeded,
    QuotaResource,
    ReservationMismatch,
    ReserveOutcome,
    SeatLimitExceeded,
    SubscriptionAccessDenied,
)
from app.persistence.models.tenant import Subscription, SubscriptionQuotaUsage, Tenant

pytestmark = pytest.mark.asyncio


async def _suscripcion_activa(
    db_session: AsyncSession,
    tenant: Tenant,
    *,
    ia_queries: int = 5,
    imports: int = 2,
    reads: int = 1,
    seats: int = 3,
) -> Subscription:
    ahora = datetime.now(UTC)
    sub = Subscription(
        subscription_id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        plan_code="control",
        status="ACTIVE",
        seats_included=seats,
        granted_ia_queries_per_month=ia_queries,
        granted_imports_per_month=imports,
        granted_photo_pdf_reads_per_month=reads,
        current_period_start=ahora - timedelta(days=1),
        current_period_end=ahora + timedelta(days=29),
    )
    db_session.add(sub)
    await db_session.commit()
    return sub


async def test_reserve_commit_mueve_reserved_a_used(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    sub = await _suscripcion_activa(db_session, sample_tenant, ia_queries=5)
    await svc.reserve(
        db_session, subscription=sub, resource=QuotaResource.IA_QUERY, operation_id="op-1"
    )

    fila = (
        await db_session.execute(
            select(SubscriptionQuotaUsage).where(
                SubscriptionQuotaUsage.tenant_id == sub.tenant_id
            )
        )
    ).scalar_one()
    assert (fila.used, fila.reserved, fila.limit_snapshot) == (0, 1, 5)

    await svc.commit(
        db_session, tenant_id=sub.tenant_id, resource=QuotaResource.IA_QUERY, operation_id="op-1"
    )
    await db_session.refresh(fila)
    assert (fila.used, fila.reserved) == (1, 0)


async def test_release_devuelve_reserved_sin_tocar_used(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    sub = await _suscripcion_activa(db_session, sample_tenant)
    await svc.reserve(
        db_session, subscription=sub, resource=QuotaResource.IMPORT, operation_id="op-err"
    )
    await svc.release(
        db_session, tenant_id=sub.tenant_id, resource=QuotaResource.IMPORT, operation_id="op-err"
    )
    fila = (
        await db_session.execute(
            select(SubscriptionQuotaUsage).where(
                SubscriptionQuotaUsage.tenant_id == sub.tenant_id,
                SubscriptionQuotaUsage.resource == "import",
            )
        )
    ).scalar_one()
    assert (fila.used, fila.reserved) == (0, 0)


async def test_reintentar_el_mismo_operation_id_no_reserva_dos_veces(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    sub = await _suscripcion_activa(db_session, sample_tenant, ia_queries=5)
    for _ in range(3):  # simula reintentos del mismo request_id
        await svc.reserve(
            db_session, subscription=sub, resource=QuotaResource.IA_QUERY, operation_id="mismo-id"
        )
    fila = (
        await db_session.execute(
            select(SubscriptionQuotaUsage).where(
                SubscriptionQuotaUsage.tenant_id == sub.tenant_id
            )
        )
    ).scalar_one()
    assert fila.reserved == 1  # nunca 3


async def test_confirmar_o_liberar_una_reserva_ya_resuelta_es_no_op(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    sub = await _suscripcion_activa(db_session, sample_tenant)
    await svc.reserve(
        db_session, subscription=sub, resource=QuotaResource.IA_QUERY, operation_id="op-x"
    )
    await svc.commit(
        db_session, tenant_id=sub.tenant_id, resource=QuotaResource.IA_QUERY, operation_id="op-x"
    )
    # Confirmar de nuevo (reintento tardío) no debe sumar `used` otra vez.
    await svc.commit(
        db_session, tenant_id=sub.tenant_id, resource=QuotaResource.IA_QUERY, operation_id="op-x"
    )
    # Ni liberar algo ya confirmado debe restarle a `reserved` (que ya está en 0).
    await svc.release(
        db_session, tenant_id=sub.tenant_id, resource=QuotaResource.IA_QUERY, operation_id="op-x"
    )
    fila = (
        await db_session.execute(
            select(SubscriptionQuotaUsage).where(
                SubscriptionQuotaUsage.tenant_id == sub.tenant_id
            )
        )
    ).scalar_one()
    assert (fila.used, fila.reserved) == (1, 0)


async def test_reserve_agota_cupo_tira_quota_exceeded(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    sub = await _suscripcion_activa(db_session, sample_tenant, reads=1)
    await svc.reserve(
        db_session, subscription=sub, resource=QuotaResource.PHOTO_PDF_READ, operation_id="r-1"
    )
    with pytest.raises(QuotaExceeded):
        await svc.reserve(
            db_session,
            subscription=sub,
            resource=QuotaResource.PHOTO_PDF_READ,
            operation_id="r-2",
        )
    # La reserva fallida no debe haber dejado rastro en el contador.
    fila = (
        await db_session.execute(
            select(SubscriptionQuotaUsage).where(
                SubscriptionQuotaUsage.tenant_id == sub.tenant_id,
                SubscriptionQuotaUsage.resource == "photo_pdf_read",
            )
        )
    ).scalar_one()
    assert fila.reserved == 1  # solo la primera


async def test_reserve_en_read_only_tira_access_denied(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    sub = await _suscripcion_activa(db_session, sample_tenant)
    sub.status = "READ_ONLY"
    await db_session.commit()
    with pytest.raises(SubscriptionAccessDenied):
        await svc.reserve(
            db_session, subscription=sub, resource=QuotaResource.IA_QUERY, operation_id="op-ro"
        )


async def test_enforce_active_subscription_bloquea_read_only(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    sub = await _suscripcion_activa(db_session, sample_tenant)
    sub.status = "READ_ONLY"
    with pytest.raises(SubscriptionAccessDenied):
        svc.enforce_active_subscription(sub)


def _contar(n: int, orden: list[str] | None = None):
    async def _count() -> int:
        if orden is not None:
            orden.append("count")
        return n

    return _count


async def test_seat_limit_exceeded_cuando_ya_esta_en_el_limite(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    sub = await _suscripcion_activa(db_session, sample_tenant, seats=2)
    with pytest.raises(SeatLimitExceeded):
        await svc.enforce_seat_limit(db_session, subscription=sub, count_active_users=_contar(2))


async def test_seat_limit_permite_si_hay_lugar(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    sub = await _suscripcion_activa(db_session, sample_tenant, seats=3)
    await svc.enforce_seat_limit(db_session, subscription=sub, count_active_users=_contar(2))


async def test_seat_limit_cuenta_despues_de_tomar_el_lock(
    db_session: AsyncSession, sample_tenant: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """El contrato es el ORDEN: un conteo anterior al lock es un conteo viejo."""
    sub = await _suscripcion_activa(db_session, sample_tenant, seats=3)
    orden: list[str] = []
    original = db_session.execute

    async def _execute(stmt, *a, **kw):
        if "FOR UPDATE" in str(stmt.compile(dialect=postgresql.dialect())):
            orden.append("lock")
        return await original(stmt, *a, **kw)

    monkeypatch.setattr(db_session, "execute", _execute)
    await svc.enforce_seat_limit(
        db_session, subscription=sub, count_active_users=_contar(1, orden)
    )
    assert orden == ["lock", "count"]


async def test_seat_limit_en_prueba_es_una_sola_plaza_sea_cual_sea_el_plan(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    sub = await _suscripcion_activa(db_session, sample_tenant, seats=3)
    sub.status = "TRIAL"
    sub.trial_ends_at = datetime.now(UTC) + timedelta(days=5)
    await db_session.commit()
    with pytest.raises(SeatLimitExceeded) as exc:
        await svc.enforce_seat_limit(db_session, subscription=sub, count_active_users=_contar(1))
    assert exc.value.seats_included == 1


async def test_seat_limit_con_suscripcion_vencida_no_suma_usuarios(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    sub = await _suscripcion_activa(db_session, sample_tenant, seats=3)
    sub.status = "READ_ONLY"
    await db_session.commit()
    with pytest.raises(SubscriptionAccessDenied):
        await svc.enforce_seat_limit(db_session, subscription=sub, count_active_users=_contar(0))


# ── reserve(): el resultado dice en qué estado estaba la operación ────────────


async def _reservar(db_session: AsyncSession, sub: Subscription, op: str, units: int = 1):
    return await svc.reserve(
        db_session, subscription=sub, resource=QuotaResource.IA_QUERY,
        operation_id=op, units=units,
    )


async def test_reserve_nueva_devuelve_new_y_repetida_en_curso(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    sub = await _suscripcion_activa(db_session, sample_tenant)
    assert await _reservar(db_session, sub, "op-1") is ReserveOutcome.NEW
    assert await _reservar(db_session, sub, "op-1") is ReserveOutcome.IN_PROGRESS


async def test_reserve_repetida_ya_confirmada(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    sub = await _suscripcion_activa(db_session, sample_tenant)
    await _reservar(db_session, sub, "op-1")
    assert await svc.commit(
        db_session, tenant_id=sub.tenant_id, resource=QuotaResource.IA_QUERY, operation_id="op-1"
    )
    assert await _reservar(db_session, sub, "op-1") is ReserveOutcome.ALREADY_COMMITTED


async def test_reserve_repetida_ya_liberada_no_revive_la_reserva(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    sub = await _suscripcion_activa(db_session, sample_tenant)
    await _reservar(db_session, sub, "op-1")
    assert await svc.release(
        db_session, tenant_id=sub.tenant_id, resource=QuotaResource.IA_QUERY, operation_id="op-1"
    )
    assert await _reservar(db_session, sub, "op-1") is ReserveOutcome.ALREADY_RELEASED
    # Y resolver de nuevo no mueve nada: `False` = no fui yo.
    assert not await svc.commit(
        db_session, tenant_id=sub.tenant_id, resource=QuotaResource.IA_QUERY, operation_id="op-1"
    )


async def test_reserve_misma_clave_con_otras_unidades_es_conflicto(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    sub = await _suscripcion_activa(db_session, sample_tenant)
    await _reservar(db_session, sub, "op-1", units=1)
    with pytest.raises(ReservationMismatch):
        await _reservar(db_session, sub, "op-1", units=2)


async def test_reserve_sin_autocommit_no_publica_lo_pendiente_del_llamador(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Componer el cupo con escrituras propias: si el llamador revierte, se
    van juntas la reserva y sus datos — `reserve()` no commiteó por él."""
    sub = await _suscripcion_activa(db_session, sample_tenant)
    resultado = await svc.reserve(
        db_session, subscription=sub, resource=QuotaResource.IA_QUERY,
        operation_id="op-1", autocommit=False,
    )
    assert resultado is ReserveOutcome.NEW
    await db_session.rollback()
    await db_session.refresh(sub)  # el rollback expira los atributos cargados
    assert await _reservar(db_session, sub, "op-1") is ReserveOutcome.NEW


async def test_subscription_free_legado_sin_granted_no_queda_en_cupo_cero(
    db_session: AsyncSession, sample_tenant: Tenant
) -> None:
    """Una fila `FREE` creada ANTES de este módulo (`granted_*` en NULL, tal

    como quedan las cuentas ya en producción tras la migración) tiene que
    seguir pudiendo usar el chat — nunca bloquearse por cupo cero de golpe."""
    sub = Subscription(
        subscription_id=uuid.uuid4(),
        tenant_id=sample_tenant.tenant_id,
        plan_code="FREE",
        status="ACTIVE",
        seats_included=1,
        # granted_* deliberadamente NULL — así quedan las filas históricas.
    )
    db_session.add(sub)
    await db_session.commit()

    await svc.reserve(
        db_session, subscription=sub, resource=QuotaResource.IA_QUERY, operation_id="op-legado"
    )
    fila = (
        await db_session.execute(
            select(SubscriptionQuotaUsage).where(
                SubscriptionQuotaUsage.tenant_id == sub.tenant_id
            )
        )
    ).scalar_one()
    assert fila.limit_snapshot >= 999_999
