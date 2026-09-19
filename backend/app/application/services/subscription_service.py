"""Control de acceso y cupos por suscripción — el bloque B3 de la política.

Tres capas evaluadas en orden, en cada operación protegida:

    identidad y empresa → permiso del usuario → estado efectivo de la
    suscripción → cupo disponible → ejecución

Este módulo resuelve las dos últimas. Las dos primeras ya existen
(`get_current_user`/`get_current_tenant`, roles, PIN step-up).

Patrón de cupos: reservar (`reserve`) ANTES de ejecutar, confirmar
(`commit`) al terminar bien, liberar (`release`) si termina mal — nunca al
revés. Cada reserva queda identificada por `operation_id` (elegido por quien
llama: `chat:{trace_id}` en el chat, `import-attempt:{id}` en la importación
en segundo plano, `read:{uuid}` en una lectura — los prefijos viven en el
dominio porque la conciliación decide según ellos), así que reintentar la
MISMA operación nunca reserva una unidad de más — es la corrección directa
del rate-limit de chat actual
(`agent.py`), que hace el chequeo y el incremento en pasos separados, con
toda la llamada al LLM en el medio.

Transacciones cortas por default: `reserve()` y `commit()`/`release()` cada
uno hace SU PROPIO commit — nunca se mantiene un lock de fila mientras corre
una llamada a IA. Ese commit publica TODO lo pendiente en la sesión, así que
quien necesite componer el cupo con sus propias escrituras (que el consumo
entre o no entre junto con los datos) pasa `autocommit=False` y commitea él.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.subscription import (
    LEGACY_FREE_LIMITS,
    LEGACY_FREE_PLAN_CODE,
    READ_OPERATION_PREFIX,
    EffectiveAccess,
    PlanQuota,
    QuotaExceeded,
    QuotaResource,
    ReservationMismatch,
    ReservationNotUsable,
    ReserveOutcome,
    SeatLimitExceeded,
    SubscriptionAccessDenied,
    SubscriptionMissing,
    effective_access,
)
from app.observability.logger import get_logger
from app.persistence.models.tenant import (
    Subscription,
    SubscriptionQuotaReservation,
    SubscriptionQuotaUsage,
)
from app.persistence.repositories.tenant_repository import TenantRepository

logger = get_logger(__name__)


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


def _utc(valor: datetime | None) -> datetime | None:
    """SQLite devuelve naive lo que se guardó con zona (Postgres no). Todo lo
    que esta tabla persiste es UTC, así que se le repone — el dominio sigue
    rechazando comparar naive contra aware, que es lo que lo hace confiable."""
    if valor is not None and valor.tzinfo is None:
        return valor.replace(tzinfo=UTC)
    return valor


def _next_quota(subscription: Subscription) -> PlanQuota | None:
    """Condiciones del período SIGUIENTE ya pago, o `None` si no hay uno.

    Son una copia tomada al cobrar (igual que `granted_*`), nunca el catálogo
    en vivo: cambiar `plan_definitions` no altera un período que ya se pagó.
    """
    if subscription.next_period_end is None:
        return None
    if subscription.next_granted_ia_queries_per_month is None:
        return None  # pagó el mismo plan: rigen las condiciones vigentes
    return PlanQuota(
        seats_included=subscription.next_seats_included or subscription.seats_included,
        ia_queries_per_month=subscription.next_granted_ia_queries_per_month,
        imports_per_month=subscription.next_granted_imports_per_month or 0,
        photo_pdf_reads_per_month=subscription.next_granted_photo_pdf_reads_per_month or 0,
    )


def resolve_access(subscription: Subscription, *, now: datetime | None = None) -> EffectiveAccess:
    """`effective_access()` aplicado a una `Subscription` real — ver el dominio."""
    momento = now or datetime.now(UTC)
    return effective_access(
        status=subscription.status,
        granted_quota=_granted_quota(subscription),
        created_at=_utc(subscription.created_at) or momento,
        trial_ends_at=_utc(subscription.trial_ends_at),
        grace_ends_at=_utc(subscription.grace_ends_at),
        current_period_start=_utc(subscription.current_period_start),
        current_period_end=_utc(subscription.current_period_end),
        now=momento,
        is_legacy_free=subscription.plan_code == LEGACY_FREE_PLAN_CODE,
        cancel_at_period_end=bool(subscription.cancel_at_period_end),
        next_period_end=_utc(subscription.next_period_end),
        next_quota=_next_quota(subscription),
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


async def assert_tenant_can_write(
    session: AsyncSession, tenant_id: uuid.UUID, *, origen: str, now: datetime | None = None
) -> Subscription:
    """LA autorización de un alta de negocio — la misma por API, agente y worker.

    Vive acá y no en una dependency de FastAPI porque una dependency no corre
    cuando un worker o el agente llaman a una función Python: cada ejecutor
    interno que crea datos de negocio llama a esto, igual que el gate HTTP
    (`deps.require_active_subscription`, que es solo su traducción a 402/503).

    Levanta `SubscriptionMissing` o `SubscriptionAccessDenied`. Una vez por
    operación: los subpasos de algo ya autorizado (el movimiento de stock de
    una venta, el compensatorio de una corrección) NO vuelven a preguntar.
    """
    subscription = await TenantRepository(session).get_current_subscription(tenant_id)
    if subscription is None:
        logger.error("subscription_missing", tenant_id=str(tenant_id), origen=origen)
        raise SubscriptionMissing(tenant_id)
    enforce_active_subscription(subscription, now=now)
    return subscription


async def reserve(
    session: AsyncSession,
    *,
    subscription: Subscription,
    resource: QuotaResource,
    operation_id: str,
    units: int = 1,
    now: datetime | None = None,
    autocommit: bool = True,
) -> ReserveOutcome:
    """Reserva `units` de `resource` para `operation_id`, o tira si no hay cupo.

    Idempotente: si `operation_id` ya tiene una reserva, no se toca el cupo
    de nuevo — pero el resultado DICE en qué estado estaba (`ReserveOutcome`).
    Solo `NEW` autoriza a ejecutar: seguir adelante con una reserva
    `RELEASED` o `COMMITTED` sería correr la operación sin cupo que la
    respalde, y con una `RESERVED` ajena, correrla dos veces. Pedir otra
    cantidad de unidades con la misma clave tira `ReservationMismatch`.

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
        # de nuevo, pero se informa su estado — no son casos equivalentes.
        previa = (
            await session.execute(
                sa.select(
                    SubscriptionQuotaReservation.state, SubscriptionQuotaReservation.units
                ).where(
                    SubscriptionQuotaReservation.tenant_id == subscription.tenant_id,
                    SubscriptionQuotaReservation.resource == resource.value,
                    SubscriptionQuotaReservation.operation_id == operation_id,
                )
            )
        ).one()
        if autocommit:
            await session.commit()
        if previa.units != units:
            raise ReservationMismatch(operation_id, previa.units, units)
        return {
            "RESERVED": ReserveOutcome.IN_PROGRESS,
            "COMMITTED": ReserveOutcome.ALREADY_COMMITTED,
            "RELEASED": ReserveOutcome.ALREADY_RELEASED,
        }[previa.state]

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
        if autocommit:
            await session.commit()
        raise QuotaExceeded(subscription.tenant_id, resource, limite, acceso.period_end)

    if autocommit:
        await session.commit()
    return ReserveOutcome.NEW


async def _resolver(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    resource: QuotaResource,
    operation_id: str,
    confirmar: bool,
    autocommit: bool,
) -> bool:
    """Saca una reserva de `RESERVED` y ajusta el contador. El `WHERE state =
    'RESERVED'` es el CAS: solo un llamador gana, y `True` es "fui yo"."""
    resultado = await session.execute(
        sa.update(SubscriptionQuotaReservation)
        .where(
            SubscriptionQuotaReservation.tenant_id == tenant_id,
            SubscriptionQuotaReservation.resource == resource.value,
            SubscriptionQuotaReservation.operation_id == operation_id,
            SubscriptionQuotaReservation.state == "RESERVED",
        )
        .values(state="COMMITTED" if confirmar else "RELEASED")
        .returning(
            SubscriptionQuotaReservation.period_start, SubscriptionQuotaReservation.units
        )
    )
    fila = resultado.first()
    if fila is not None:
        period_start, units = fila
        valores = {"reserved": SubscriptionQuotaUsage.reserved - units}
        if confirmar:
            valores["used"] = SubscriptionQuotaUsage.used + units
        await session.execute(
            sa.update(SubscriptionQuotaUsage)
            .where(
                SubscriptionQuotaUsage.tenant_id == tenant_id,
                SubscriptionQuotaUsage.resource == resource.value,
                SubscriptionQuotaUsage.period_start == period_start,
            )
            .values(**valores)
        )
    if autocommit:
        await session.commit()
    return fila is not None


async def commit(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    resource: QuotaResource,
    operation_id: str,
    autocommit: bool = True,
) -> bool:
    """Confirma una reserva: `used += units`, `reserved -= units`.

    Devuelve si ESTA llamada la confirmó. `False` = ya no estaba en
    `RESERVED` (confirmada, liberada, o nunca existió): no-op idempotente.
    """
    return await _resolver(
        session, tenant_id=tenant_id, resource=resource, operation_id=operation_id,
        confirmar=True, autocommit=autocommit,
    )


async def release(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    resource: QuotaResource,
    operation_id: str,
    autocommit: bool = True,
) -> bool:
    """Libera una reserva sin confirmarla: `reserved -= units`, `used` intacto.

    Un error técnico o una ejecución fallida no consume unidad comercial —
    política §4. Mismo contrato de retorno e idempotencia que `commit()`.
    """
    return await _resolver(
        session, tenant_id=tenant_id, resource=resource, operation_id=operation_id,
        confirmar=False, autocommit=autocommit,
    )


async def assert_quota_available(
    session: AsyncSession,
    *,
    subscription: Subscription,
    resource: QuotaResource,
    units: int = 1,
    now: datetime | None = None,
) -> None:
    """Chequeo temprano y NO vinculante: ¿quedaría cupo para esta operación?

    Sirve para contestar 402/429 antes de hacer un trabajo largo. No reserva
    ni bloquea nada, así que no garantiza el cupo: quien decide es el consumo
    real (`consume_with_data`), que puede perder la última unidad contra otra
    operación y revertir. Se hace así a propósito — reservar al empezar
    retendría la fila del contador durante toda una importación.
    """
    acceso = resolve_access(subscription, now=now)
    if acceso.blocked_reason is not None:
        raise SubscriptionAccessDenied(subscription.tenant_id, acceso.blocked_reason)
    limite = acceso.quota.limit_for(resource)
    fila = (
        await session.execute(
            sa.select(SubscriptionQuotaUsage.used, SubscriptionQuotaUsage.reserved).where(
                SubscriptionQuotaUsage.tenant_id == subscription.tenant_id,
                SubscriptionQuotaUsage.resource == resource.value,
                SubscriptionQuotaUsage.period_start == acceso.period_start,
            )
        )
    ).first()
    ocupado = 0 if fila is None else fila.used + fila.reserved
    if ocupado + units > limite:
        raise QuotaExceeded(subscription.tenant_id, resource, limite, acceso.period_end)


async def consume_with_data(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    resource: QuotaResource,
    operation_id: str,
    now: datetime | None = None,
) -> None:
    """Consume UNA unidad en la transacción de quien llama — sin commit.

    Para operaciones cuyo resultado se persiste: el consumo entra o se
    revierte JUNTO con los datos, así que no puede quedar ni dato sin consumo
    ni consumo sin dato, y no deja reservas colgadas que conciliar.

    Si la operación ya traía una reserva (se autorizó al registrarse, p. ej.
    una importación en segundo plano), se confirma ESA y no se vuelve a mirar
    la suscripción: lo ya autorizado puede terminar aunque haya vencido
    después. Si no, se autoriza, reserva y confirma acá. Llamarlo dos veces
    con la misma clave consume una sola vez.
    """
    if await commit(
        session, tenant_id=tenant_id, resource=resource, operation_id=operation_id,
        autocommit=False,
    ):
        return
    subscription = await assert_tenant_can_write(
        session, tenant_id, origen=f"consume:{resource.value}", now=now
    )
    resultado = await reserve(
        session, subscription=subscription, resource=resource,
        operation_id=operation_id, now=now, autocommit=False,
    )
    if resultado is ReserveOutcome.NEW:
        await commit(
            session, tenant_id=tenant_id, resource=resource, operation_id=operation_id,
            autocommit=False,
        )
    elif resultado is not ReserveOutcome.ALREADY_COMMITTED:
        raise ReservationNotUsable(operation_id, resultado.value)


class LecturaConIA:
    """Lo que el llamador le cuenta a `photo_pdf_read` sobre cómo salió."""

    #: Sólo una lectura que devolvió datos consume. El llamador la marca.
    exitosa: bool = False


@asynccontextmanager
async def photo_pdf_read(
    session: AsyncSession, tenant_id: uuid.UUID, *, usa_ia: bool
) -> AsyncIterator[LecturaConIA]:
    """Cupo de UNA lectura de foto/PDF alrededor de la llamada al modelo.

    `usa_ia=False` (planilla: parseo determinístico, no cuesta) no toca nada.
    Si usa IA: autoriza y reserva ANTES de llamar al proveedor (transacción
    corta propia — no se retiene un lock mientras corre el LLM), y al salir
    confirma si `lectura.exitosa`, o libera: un error técnico o una lectura
    que no devolvió nada no consume. Los reintentos internos del servicio de
    extracción son la misma lectura, no unidades nuevas.
    """
    lectura = LecturaConIA()
    if not usa_ia:
        yield lectura
        return
    subscription = await assert_tenant_can_write(session, tenant_id, origen="photo_pdf_read")
    operation_id = f"{READ_OPERATION_PREFIX}{uuid.uuid4()}"
    await reserve(
        session,
        subscription=subscription,
        resource=QuotaResource.PHOTO_PDF_READ,
        operation_id=operation_id,
    )
    try:
        yield lectura
    except BaseException:
        lectura.exitosa = False
        raise
    finally:
        try:
            await (commit if lectura.exitosa else release)(
                session,
                tenant_id=tenant_id,
                resource=QuotaResource.PHOTO_PDF_READ,
                operation_id=operation_id,
            )
        except Exception:  # noqa: BLE001 — nunca tapar el resultado de la lectura
            # Queda RESERVED: la conciliación la resuelve (no se pierde ni se cobra).
            logger.warning("photo_pdf_read.resolve_failed", operation_id=operation_id)


async def enforce_seat_limit(
    session: AsyncSession,
    *,
    subscription: Subscription,
    count_active_users: Callable[[], Awaitable[int]],
    now: datetime | None = None,
) -> None:
    """¿Un usuario activo más entra en las plazas del acceso efectivo?

    El orden es el contrato: **bloquear → contar → (el llamador) crear**.
    Por eso recibe CÓMO contar y no un número: un entero ya calculado se
    calculó antes del lock, y dos altas simultáneas leerían el mismo conteo
    viejo y entrarían las dos. Acá la segunda espera el `FOR UPDATE` de la
    primera y cuenta con esa alta ya visible — siempre que el llamador
    inserte y commitee en ESTA misma transacción, que es la que sostiene el
    lock.

    Las plazas salen del acceso efectivo, no de la columna: durante la
    prueba es una sola, sea cual sea el plan asignado. Una suscripción que
    no habilita escribir tampoco habilita sumar usuarios.

    FREE legado no tiene tope: nunca lo tuvo (su `seats_included=1` es un
    default de columna, no una condición comercial), y el criterio para esas
    cuentas es preservar lo que ya podían hacer hasta que tengan su propio
    plan de migración.
    """
    if subscription.plan_code == LEGACY_FREE_PLAN_CODE:
        return
    await session.execute(
        sa.select(Subscription.subscription_id)
        .where(Subscription.subscription_id == subscription.subscription_id)
        .with_for_update()
    )
    acceso = resolve_access(subscription, now=now)
    if acceso.blocked_reason is not None:
        raise SubscriptionAccessDenied(subscription.tenant_id, acceso.blocked_reason)
    activos = await count_active_users()
    if activos + 1 > acceso.quota.seats_included:
        raise SeatLimitExceeded(subscription.tenant_id, acceso.quota.seats_included, activos)
