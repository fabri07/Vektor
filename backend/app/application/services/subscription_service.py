"""Control de acceso y cupos por suscripción — el bloque B3 de la política.

Tres capas evaluadas en orden, en cada operación protegida:

    identidad y empresa → permiso del usuario → estado efectivo de la
    suscripción → cupo disponible → ejecución

Este módulo resuelve las dos últimas. Las dos primeras ya existen
(`get_current_user`/`get_current_tenant`, roles, PIN step-up).

Patrón de cupos: reservar (`reserve`) ANTES de ejecutar, confirmar
(`commit`) al terminar bien, liberar (`release`) si termina mal — nunca al
revés. Cada reserva queda identificada por `operation_id` (elegido por quien
llama: el `request_id` del chat, el `attempt_id` del import, un id propio de
la extracción), así que reintentar la MISMA operación nunca reserva una
unidad de más — es la corrección directa del rate-limit de chat actual
(`agent.py`), que hace el chequeo y el incremento en pasos separados, con
toda la llamada al LLM en el medio.

Transacciones cortas a propósito: `reserve()` y `commit()`/`release()` cada
uno hace SU PROPIO commit — nunca se mantiene un lock de fila mientras corre
una llamada a IA o un import largo.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.subscription import (
    LEGACY_FREE_LIMITS,
    LEGACY_FREE_PLAN_CODE,
    EffectiveAccess,
    PlanQuota,
    QuotaExceeded,
    QuotaResource,
    SeatLimitExceeded,
    SubscriptionAccessDenied,
    effective_access,
)
from app.persistence.models.tenant import (
    Subscription,
    SubscriptionQuotaReservation,
    SubscriptionQuotaUsage,
)


def _granted_quota(subscription: Subscription) -> PlanQuota:
    """Cupos otorgados de la propia fila — nunca resueltos contra el catálogo.

    `granted_*` en NULL tiene DOS causas distintas, que no se resuelven igual:

    * **`plan_code == "FREE"` (legado)**: cuentas acuñadas antes de que este
      módulo existiera — nunca van a tener `granted_*` poblado porque esas
      columnas no existían. Cae a `LEGACY_FREE_LIMITS` (código, no catálogo)
      para preservar el comportamiento previo — sin esto, cualquier cuenta
      `FREE` ya en producción quedaría con cupo CERO el día que esto se
      despliega, y el chat se les corta a todas de golpe.
    * **Cualquier otro plan sin `granted_*`**: una `Subscription` recién
      creada que todavía no pasó por `activate`. Cae a cupo cero — no es un
      bug, toda `Subscription` nueva nace en `TRIAL` (que usa `TRIAL_LIMITS`,
      ni mira esto); solo importa si alguien fuerza `status=ACTIVE` a mano
      sin pasar por `activate` primero.
    """
    if subscription.granted_ia_queries_per_month is None:
        if subscription.plan_code == LEGACY_FREE_PLAN_CODE:
            return LEGACY_FREE_LIMITS
        return PlanQuota(
            seats_included=subscription.seats_included,
            ia_queries_per_month=0,
            imports_per_month=0,
            photo_pdf_reads_per_month=0,
        )
    return PlanQuota(
        seats_included=subscription.seats_included,
        ia_queries_per_month=subscription.granted_ia_queries_per_month,
        imports_per_month=subscription.granted_imports_per_month or 0,
        photo_pdf_reads_per_month=subscription.granted_photo_pdf_reads_per_month or 0,
    )


def resolve_access(subscription: Subscription, *, now: datetime | None = None) -> EffectiveAccess:
    """`effective_access()` aplicado a una `Subscription` real — ver el dominio."""
    momento = now or datetime.now(UTC)
    return effective_access(
        status=subscription.status,
        granted_quota=_granted_quota(subscription),
        created_at=subscription.created_at,
        trial_ends_at=subscription.trial_ends_at,
        grace_ends_at=subscription.grace_ends_at,
        current_period_start=subscription.current_period_start,
        current_period_end=subscription.current_period_end,
        now=momento,
    )


def enforce_active_subscription(subscription: Subscription, *, now: datetime | None = None) -> None:
    """Capa transversal: ¿esta suscripción habilita escribir/consultar IA?

    No mira ningún cupo — solo el estado. Úsese en cualquier operación que
    deba bloquearse en `READ_ONLY`/`CANCELLED` vencida/prueba vencida, tenga
    o no un recurso de cupo asociado (ventas, gastos, movimientos de stock:
    ninguno consume IA/import/lectura, pero todos son escritura comercial).
    """
    acceso = resolve_access(subscription, now=now)
    if acceso.blocked_reason is not None:
        raise SubscriptionAccessDenied(subscription.tenant_id, acceso.blocked_reason)


async def reserve(
    session: AsyncSession,
    *,
    subscription: Subscription,
    resource: QuotaResource,
    operation_id: str,
    units: int = 1,
    now: datetime | None = None,
) -> None:
    """Reserva `units` de `resource` para `operation_id`, o tira si no hay cupo.

    Idempotente: si `operation_id` ya tiene una reserva (`RESERVED` o
    `COMMITTED`), esta llamada es un no-op silencioso — un reintento de la
    MISMA operación nunca reserva una unidad de más. Si ya estaba
    `RELEASED`, también es un no-op: esa operación ya se dio por perdida: no
    se le "revive" la reserva, quien reintenta genuino usa un `operation_id`
    nuevo.

    Levanta `SubscriptionAccessDenied` si el estado no habilita el recurso, o
    `QuotaExceeded` si no queda cupo en el período vigente. Ninguna de las
    dos dos dejan una fila a medio escribir: todo lo que este método toca
    queda commiteado o revertido antes de devolver el control.
    """
    momento = now or datetime.now(UTC)
    acceso = resolve_access(subscription, now=momento)
    if acceso.blocked_reason is not None:
        raise SubscriptionAccessDenied(subscription.tenant_id, acceso.blocked_reason)

    limite = acceso.quota.limit_for(resource)
    if limite < units:
        raise QuotaExceeded(subscription.tenant_id, resource, limite, acceso.period_end)

    bind = session.bind
    dialecto = bind.dialect.name if bind is not None else ""

    reservation_id = uuid.uuid4()
    if dialecto == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as _pg_insert  # noqa: PLC0415

        insertar = (
            _pg_insert(SubscriptionQuotaReservation)
            .values(
                id=reservation_id,
                tenant_id=subscription.tenant_id,
                resource=resource.value,
                operation_id=operation_id,
                period_start=acceso.period_start,
                units=units,
                state="RESERVED",
            )
            .on_conflict_do_nothing(
                index_elements=["tenant_id", "resource", "operation_id"]
            )
            .returning(SubscriptionQuotaReservation.id)
        )
    elif dialecto == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as _sqlite_insert  # noqa: PLC0415

        insertar = (
            _sqlite_insert(SubscriptionQuotaReservation)
            .values(
                id=reservation_id,
                tenant_id=subscription.tenant_id,
                resource=resource.value,
                operation_id=operation_id,
                period_start=acceso.period_start,
                units=units,
                state="RESERVED",
            )
            .on_conflict_do_nothing(
                index_elements=["tenant_id", "resource", "operation_id"]
            )
            .returning(SubscriptionQuotaReservation.id)
        )
    else:  # pragma: no cover — sin ON CONFLICT no hay idempotencia real
        raise RuntimeError(f"reserve() no soporta el dialecto {dialecto!r}")

    ganó = (await session.execute(insertar)).scalar_one_or_none() is not None
    if not ganó:
        # Ya existía una reserva para este operation_id: no se toca el cupo
        # de nuevo, sea cual sea su estado (RESERVED/COMMITTED/RELEASED).
        await session.commit()
        return

    # Core insert tipado por dialecto (no SQL crudo con `str(uuid)`): un UUID
    # pasado como texto literal en `text()` no pasa por el bind processor de la
    # columna, y en SQLite eso lo guarda con un formato distinto (hex sin
    # guiones) al que usa el ORM al comparar — el `SELECT` de después nunca
    # encontraba la fila que sí se había insertado.
    valores_usage = {
        "id": uuid.uuid4(),
        "tenant_id": subscription.tenant_id,
        "subscription_id": subscription.subscription_id,
        "resource": resource.value,
        "period_start": acceso.period_start,
        "period_end": acceso.period_end,
        "used": 0,
        "reserved": units,
        "limit_snapshot": limite,
    }
    incremento = {
        "reserved": SubscriptionQuotaUsage.reserved + units,
        "updated_at": momento,
    }
    cabe = (
        SubscriptionQuotaUsage.used + SubscriptionQuotaUsage.reserved + units
        <= SubscriptionQuotaUsage.limit_snapshot
    )
    if dialecto == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as _pg_insert  # noqa: PLC0415

        upsert = (
            _pg_insert(SubscriptionQuotaUsage)
            .values(**valores_usage)
            .on_conflict_do_update(
                index_elements=["tenant_id", "resource", "period_start"],
                set_=incremento,
                where=cabe,
            )
            .returning(SubscriptionQuotaUsage.reserved)
        )
    else:
        from sqlalchemy.dialects.sqlite import insert as _sqlite_insert  # noqa: PLC0415

        upsert = (
            _sqlite_insert(SubscriptionQuotaUsage)
            .values(**valores_usage)
            .on_conflict_do_update(
                index_elements=["tenant_id", "resource", "period_start"],
                set_=incremento,
                where=cabe,
            )
            .returning(SubscriptionQuotaUsage.reserved)
        )
    resultado = await session.execute(upsert)
    hubo_lugar = resultado.first() is not None
    if not hubo_lugar:
        # Sin lugar: deshacer la reserva que se acaba de insertar — nunca
        # existió capacidad real, no corresponde ni RELEASED (esa fila jamás
        # contó contra el cupo).
        await session.execute(
            sa.delete(SubscriptionQuotaReservation).where(
                SubscriptionQuotaReservation.id == reservation_id
            )
        )
        await session.commit()
        raise QuotaExceeded(subscription.tenant_id, resource, limite, acceso.period_end)

    await session.commit()


async def commit(
    session: AsyncSession, *, tenant_id: uuid.UUID, resource: QuotaResource, operation_id: str
) -> None:
    """Confirma una reserva: `used += units`, `reserved -= units`.

    No-op idempotente si la reserva ya no está en `RESERVED` (ya confirmada,
    liberada, o nunca existió) — el `WHERE state = 'RESERVED'` es el CAS.
    """
    resultado = await session.execute(
        sa.update(SubscriptionQuotaReservation)
        .where(
            SubscriptionQuotaReservation.tenant_id == tenant_id,
            SubscriptionQuotaReservation.resource == resource.value,
            SubscriptionQuotaReservation.operation_id == operation_id,
            SubscriptionQuotaReservation.state == "RESERVED",
        )
        .values(state="COMMITTED")
        .returning(
            SubscriptionQuotaReservation.period_start, SubscriptionQuotaReservation.units
        )
    )
    fila = resultado.first()
    if fila is None:
        await session.commit()
        return
    period_start, units = fila
    await session.execute(
        sa.update(SubscriptionQuotaUsage)
        .where(
            SubscriptionQuotaUsage.tenant_id == tenant_id,
            SubscriptionQuotaUsage.resource == resource.value,
            SubscriptionQuotaUsage.period_start == period_start,
        )
        .values(
            used=SubscriptionQuotaUsage.used + units,
            reserved=SubscriptionQuotaUsage.reserved - units,
        )
    )
    await session.commit()


async def release(
    session: AsyncSession, *, tenant_id: uuid.UUID, resource: QuotaResource, operation_id: str
) -> None:
    """Libera una reserva sin confirmarla: `reserved -= units`, `used` intacto.

    Un error técnico o una ejecución fallida no consume unidad comercial —
    política §4. No-op idempotente igual que `commit()`.
    """
    resultado = await session.execute(
        sa.update(SubscriptionQuotaReservation)
        .where(
            SubscriptionQuotaReservation.tenant_id == tenant_id,
            SubscriptionQuotaReservation.resource == resource.value,
            SubscriptionQuotaReservation.operation_id == operation_id,
            SubscriptionQuotaReservation.state == "RESERVED",
        )
        .values(state="RELEASED")
        .returning(
            SubscriptionQuotaReservation.period_start, SubscriptionQuotaReservation.units
        )
    )
    fila = resultado.first()
    if fila is None:
        await session.commit()
        return
    period_start, units = fila
    await session.execute(
        sa.update(SubscriptionQuotaUsage)
        .where(
            SubscriptionQuotaUsage.tenant_id == tenant_id,
            SubscriptionQuotaUsage.resource == resource.value,
            SubscriptionQuotaUsage.period_start == period_start,
        )
        .values(reserved=SubscriptionQuotaUsage.reserved - units)
    )
    await session.commit()


async def enforce_seat_limit(
    session: AsyncSession, *, subscription: Subscription, active_users_excluding_new: int
) -> None:
    """¿Un usuario activo más entra en `seats_included`?

    Toma un `SELECT ... FOR UPDATE` sobre la fila de la suscripción antes de
    contar: dos altas simultáneas no pueden pasar la cuenta a la vez y
    terminar las dos por encima del cupo — la segunda espera el lock de la
    primera y cuenta con el número ya actualizado.

    `active_users_excluding_new` lo cuenta el llamador (dentro de la MISMA
    transacción, después de este lock) para no acoplar este servicio al
    repositorio de usuarios.
    """
    await session.execute(
        sa.select(Subscription.subscription_id)
        .where(Subscription.subscription_id == subscription.subscription_id)
        .with_for_update()
    )
    if active_users_excluding_new + 1 > subscription.seats_included:
        raise SeatLimitExceeded(
            subscription.tenant_id, subscription.seats_included, active_users_excluding_new
        )
