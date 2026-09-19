"""Dominio de suscripciones: planes, estados, cupos y reservas.

Cierra la advertencia que dejó `tenant_provisioning.py` ("no copiar
`requested_plan` a `plan_code` sin construir antes el circuito comercial
completo"): con este módulo, el circuito existe — un tenant aprobado arranca
en `TRIAL` con cupos propios y fijos (`TRIAL_LIMITS`), nunca los del plan
asignado, y recién si el dueño lo activa a mano
(`scripts/subscriptions.py activate`, mientras no exista Mercado Pago) pasa a
`ACTIVE` con los cupos y el precio QUE ESTABAN VIGENTES EN `plan_definitions`
al momento de activar — copiados a la propia `Subscription`
(`granted_ia_queries_per_month` y hermanos), nunca resueltos en vivo contra el
catálogo. Cambiar el catálogo más adelante no altera retroactivamente un
período ya otorgado.

Ver la política de planes y precios en ``docs/plans/`` para el porqué de cada
número.

Sin dependencias de infraestructura (ni SQLAlchemy, ni settings, ni repos) —
`effective_access()` recibe todo por parámetro, incluido `now`, para ser
testeable con fechas fijas y no depender de un reloj propio.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final


class SubscriptionStatus(StrEnum):
    """Estados posibles de una `Subscription`. Cerrado — ver el CHECK de la tabla.

    ``TRIAL``     → recién aprobada, cupos de prueba (`TRIAL_LIMITS`), nunca
                     los del plan asignado — evita regalar el plan pago antes
                     de cobrar.
    ``ACTIVE``    → plan pago vigente, cupos `granted_*` de la propia fila.
    ``GRACE``     → venció el período pagado, dentro de la ventana de 7 días:
                     mantiene SOLO el saldo que quedaba del período anterior
                     — nunca se le acredita un cupo mensual nuevo sin
                     confirmar la renovación (`renew`).
    ``READ_ONLY`` → gracia vencida, o prueba terminada sin contratar: bloquea
                     IA, importaciones, lecturas y escrituras de negocio —
                     nunca el acceso a los datos ya cargados ni la
                     exportación (eso no depende de este módulo).
    ``CANCELLED`` → cancelación solicitada. Mientras `current_period_end` no
                     pasó, se comporta como `ACTIVE` (ya está pago); pasado
                     ese día, como `READ_ONLY`. Ver `effective_access()`.
    """

    TRIAL = "TRIAL"
    ACTIVE = "ACTIVE"
    GRACE = "GRACE"
    READ_ONLY = "READ_ONLY"
    CANCELLED = "CANCELLED"


#: Estados que NO son "una suscripción viva": los que el índice único parcial
#: `uq_subscriptions_one_live_per_tenant` deja convivir con una viva, y los
#: que `get_current_subscription` sólo devuelve si no hay ninguna viva.
TERMINAL_STATUSES: Final[frozenset[str]] = frozenset({SubscriptionStatus.CANCELLED.value})


class AssignablePlan(StrEnum):
    """Planes que un dueño puede ASIGNAR al aprobar una solicitud.

    Espeja el subconjunto pago de `RequestedPlan`
    (`app.domain.access_request`) — mismo criterio que `Vertical` vs
    `RequestedVertical`: lo que el solicitante puede pedir (`free`, `premium`
    histórico) es más amplio que lo que el dueño puede asignar. Ninguna
    solicitud histórica (`free`/`premium`) mapea sola a un plan: el dueño
    elige explícitamente, siempre — nunca se inventa una equivalencia.
    """

    ESENCIAL = "esencial"
    CONTROL = "control"
    DIRECCION = "direccion"


class QuotaResource(StrEnum):
    """Recursos con cupo mensual. Cerrado — ver el CHECK de las tablas de cupo."""

    IA_QUERY = "ia_query"
    IMPORT = "import"
    PHOTO_PDF_READ = "photo_pdf_read"


class ReservationState(StrEnum):
    """Ciclo de vida de UNA reserva de cupo, identificada por `operation_id`.

    ``RESERVED``  → tomó cupo (`reserved += units`), la operación está en curso.
    ``COMMITTED`` → terminó bien: `used += units`, `reserved -= units`.
    ``RELEASED``  → terminó mal (o nunca corrió): `reserved -= units`, `used`
                    no se toca — un error técnico no consume unidad comercial.

    Las transiciones desde `COMMITTED`/`RELEASED` a cualquier otro estado no
    existen: son terminales. Confirmar o liberar una reserva ya resuelta es
    un no-op (ver `subscription_service`), no un error — así un reintento con
    el mismo `operation_id` nunca mueve el contador dos veces.
    """

    RESERVED = "RESERVED"
    COMMITTED = "COMMITTED"
    RELEASED = "RELEASED"


class ReserveOutcome(StrEnum):
    """Qué encontró `reserve()` — quien llama decide con esto si ejecuta.

    Solo ``NEW`` autoriza a ejecutar la operación. Las otras tres significan
    que ese `operation_id` ya se había presentado, y NO son equivalentes:

    ``IN_PROGRESS``       → otro ejecutor la tiene reservada: ejecutar de
                             nuevo serían dos ejecuciones con un solo cupo.
    ``ALREADY_COMMITTED`` → ya terminó bien y ya consumió: corresponde
                             devolver el resultado guardado, no recalcular.
    ``ALREADY_RELEASED``  → ya se dio por perdida: ejecutar acá sería IA sin
                             cupo. Un reintento genuino usa un id nuevo.
    """

    NEW = "NEW"
    IN_PROGRESS = "IN_PROGRESS"
    ALREADY_COMMITTED = "ALREADY_COMMITTED"
    ALREADY_RELEASED = "ALREADY_RELEASED"


#: Prefijo de la clave de reserva de una importación en segundo plano. La
#: clave es por INTENTO, no por archivo: un reintento técnico del mismo
#: intento reusa su reserva (no cobra de nuevo), y un intento que fracasa
#: libera la suya sin dejarle al archivo una clave quemada.
IMPORT_ATTEMPT_OPERATION_PREFIX: Final[str] = "import-attempt:"


#: Prefijo de la clave de un turno de chat: `chat:{trace_id}`. El `trace_id`
#: es el que `decision_audit_log` guarda solo en cada fila, así que un turno
#: que llegó a auditarse (con sus tokens) deja evidencia durable de que corrió
#: — es lo que usa la conciliación para decidir confirmar en vez de liberar.
CHAT_OPERATION_PREFIX: Final[str] = "chat:"

#: Prefijo de la clave de una lectura de foto/PDF con IA.
READ_OPERATION_PREFIX: Final[str] = "read:"


def import_attempt_operation_id(attempt_id: object) -> str:
    return f"{IMPORT_ATTEMPT_OPERATION_PREFIX}{attempt_id}"


#: Días de prueba desde la aprobación (política A6). Arranca en el momento en
#: que se aprueba la solicitud — no en el primer ingreso — y no se reinicia
#: si la solicitud se re-aprueba o se reenvía la invitación: es un recorte
#: deliberado para esta entrega (ver docstring de `tenant_provisioning`).
TRIAL_DAYS: Final[int] = 14

#: Ventana de gracia tras vencer un período pagado (política B3).
GRACE_DAYS: Final[int] = 7


@dataclass(frozen=True)
class PlanQuota:
    """Cupos mensuales resueltos para una suscripción — de `TRIAL_LIMITS` o de
    los `granted_*` de la propia `Subscription`, nunca del catálogo en vivo."""

    seats_included: int
    ia_queries_per_month: int
    imports_per_month: int
    photo_pdf_reads_per_month: int

    def limit_for(self, resource: QuotaResource) -> int:
        return {
            QuotaResource.IA_QUERY: self.ia_queries_per_month,
            QuotaResource.IMPORT: self.imports_per_month,
            QuotaResource.PHOTO_PDF_READ: self.photo_pdf_reads_per_month,
        }[resource]


#: Cupos de la prueba (política A6): 1 usuario, 10 consultas de IA, 2
#: importaciones, 1 lectura de foto/PDF. Fijos para TODA prueba,
#: independientemente de qué plan pago apunte a convertirse — el trial no es
#: "el plan gratis", es una ventana acotada e igual para todos. No sale del
#: catálogo: es un valor de código, a propósito (nunca cambia por tenant).
TRIAL_LIMITS: Final[PlanQuota] = PlanQuota(
    seats_included=1,
    ia_queries_per_month=10,
    imports_per_month=2,
    photo_pdf_reads_per_month=1,
)

#: `plan_code` de las suscripciones acuñadas antes de que existiera este
#: módulo (y de los caminos que no pasan por el circuito comercial, ej.
#: `AuthService.register` / demo). No es un plan pago: es el `FREE` histórico.
LEGACY_FREE_PLAN_CODE: Final[str] = "FREE"

#: Cupos de una suscripción `FREE` legado — "sin límite mensual configurado"
#: tal cual estaba ANTES de este módulo, a propósito: el único control que
#: ya tenían estas cuentas es el rate-limit diario global de `agent.py`, que
#: sigue vigente sin cambios. Es un valor de código (como `TRIAL_LIMITS`), no
#: del catálogo — `provision_tenant` lo copia a `granted_*` al acuñar, para
#: no depender de que `plan_definitions` esté sembrada (los tests en SQLite
#: crean el esquema con `create_all`, no corren el seed de la migración).
LEGACY_FREE_LIMITS: Final[PlanQuota] = PlanQuota(
    seats_included=1,
    ia_queries_per_month=999_999,
    imports_per_month=999_999,
    photo_pdf_reads_per_month=999_999,
)


class SubscriptionAccessDenied(Exception):  # noqa: N818
    """La suscripción no habilita el recurso pedido — no es un tema de cupo.

    Cubre `READ_ONLY`, `CANCELLED` con período vencido, y `TRIAL`/`GRACE`
    vencidas (evaluado por fecha, no por el string de `status` — ver
    `effective_access`). El llamador HTTP lo traduce a 402 con el código
    `SUBSCRIPTION_READ_ONLY`, nunca a un 429 de cupo.
    """

    #: Lo que ve el usuario cuando el rechazo sale por el chat (el agente
    #: muestra `user_message` tal cual; ver `_execute_local_action`).
    user_message = (
        "Tu suscripción no está activa, así que no puedo registrar datos nuevos. "
        "Podés seguir consultando, corrigiendo y exportando lo que ya cargaste."
    )

    def __init__(self, tenant_id: object, reason: str) -> None:
        self.tenant_id = tenant_id
        self.reason = reason
        super().__init__(f"tenant {tenant_id}: acceso denegado ({reason})")


class SubscriptionMissing(Exception):  # noqa: N818
    """El tenant no tiene NINGUNA `Subscription` — dato roto, no plan gratis.

    Todo tenant se acuña con una. El llamador HTTP lo traduce a 503
    (`SUBSCRIPTION_UNAVAILABLE`): el usuario no debe nada, el problema es
    nuestro y hay que verlo — nunca se resuelve dejando pasar.
    """

    user_message = (
        "No pudimos verificar tu suscripción en este momento. Ya quedó avisado; "
        "probá de nuevo en unos minutos."
    )

    def __init__(self, tenant_id: object) -> None:
        self.tenant_id = tenant_id
        super().__init__(f"tenant {tenant_id}: sin ninguna suscripción")


class QuotaExceeded(Exception):  # noqa: N818
    """Se agotó el cupo del período vigente para este recurso.

    Distinto de `SubscriptionAccessDenied`: acá la suscripción SÍ habilita el
    recurso, simplemente no queda cupo (`used + reserved` ya llegó al
    `limit_snapshot`) hasta la renovación. El llamador HTTP lo traduce a 429
    con el código `QUOTA_EXCEEDED`, incluyendo el recurso, el límite y
    `renews_at` para que el mensaje sea accionable.
    """

    def __init__(
        self, tenant_id: object, resource: QuotaResource, limit: int, renews_at: datetime
    ) -> None:
        self.tenant_id = tenant_id
        self.resource = resource
        self.limit = limit
        self.renews_at = renews_at
        super().__init__(
            f"tenant {tenant_id}: cupo de {resource.value} agotado ({limit}/período)"
        )


class ReservationMismatch(Exception):  # noqa: N818
    """El mismo `operation_id` se presentó pidiendo otra cantidad de unidades.

    No es un reintento: es otra operación usando una clave ajena. Se rechaza
    en vez de elegir en silencio cuál de las dos cantidades vale.
    """

    def __init__(self, operation_id: str, units_reserved: int, units_requested: int) -> None:
        self.operation_id = operation_id
        self.units_reserved = units_reserved
        self.units_requested = units_requested
        super().__init__(
            f"operación {operation_id}: reservó {units_reserved} y ahora pide {units_requested}"
        )


class ReservationNotUsable(Exception):  # noqa: N818
    """La operación trae una reserva que ya se liberó: no respalda ejecutar.

    Pasa si algo dio por perdida la operación (cierre con error, conciliación)
    y después alguien la ejecuta igual. Consumir de nuevo por las dudas sería
    cobrar dos veces; seguir sin consumir, regalarla. Se corta.
    """

    def __init__(self, operation_id: str, outcome: str) -> None:
        self.operation_id = operation_id
        self.outcome = outcome
        super().__init__(f"operación {operation_id}: reserva no utilizable ({outcome})")


class SeatLimitExceeded(Exception):  # noqa: N818
    """El plan no incluye más usuarios activos de los que ya tiene el tenant."""

    def __init__(self, tenant_id: object, seats_included: int, active_users: int) -> None:
        self.tenant_id = tenant_id
        self.seats_included = seats_included
        self.active_users = active_users
        super().__init__(
            f"tenant {tenant_id}: {active_users} usuarios activos, plan incluye {seats_included}"
        )


#: Todos los rechazos de suscripción/cupo. Vive en el dominio porque lo usan
#: la capa HTTP (para traducirlos) y los workers (para clasificarlos).
SUBSCRIPTION_ERRORS: Final = (
    SubscriptionMissing,
    SubscriptionAccessDenied,
    QuotaExceeded,
    SeatLimitExceeded,
    ReservationNotUsable,
    ReservationMismatch,
)


@dataclass(frozen=True)
class EffectiveAccess:
    """Resultado de evaluar una `Subscription` en un momento dado.

    `blocked_reason` es `None` cuando la suscripción habilita el recurso (con
    los cupos de `quota`, dentro de `[period_start, period_end)`); si no es
    `None`, ningún cupo importa — el llamador rechaza antes de tocar
    `subscription_quota_usage`.
    """

    quota: PlanQuota
    period_start: datetime
    period_end: datetime
    blocked_reason: str | None


def effective_access(
    *,
    status: str,
    granted_quota: PlanQuota,
    created_at: datetime,
    trial_ends_at: datetime | None,
    grace_ends_at: datetime | None,
    current_period_start: datetime | None,
    current_period_end: datetime | None,
    now: datetime,
    is_legacy_free: bool = False,
) -> EffectiveAccess:
    """Resuelve estado + cupos + período de UNA suscripción, en un solo lugar.

    La prueba y la gracia vencidas se detectan por FECHA, no por el string de
    `status`: no hay ningún job que las pase a `READ_ONLY` en el momento
    exacto (`scripts/subscriptions.py expire-due` las persiste cuando corre,
    pero el enforcement de acá NUNCA depende de que haya corrido — evaluarlo
    en caliente es lo que hace que el bloqueo sea correcto igual).

    Lo mismo vale para `ACTIVE`: un período pago vencido pasa SOLO a gracia y
    después a restringido, aunque el `status` persistido siga diciendo
    `ACTIVE`. Única excepción, `is_legacy_free`: el `FREE` histórico no tiene
    ciclo comercial y sus fechas de período (el seed de demo las pone a 30
    días) nunca significaron un vencimiento.

    Los intervalos son `[inicio, fin)`: en el instante exacto del fin ya no
    hay acceso.

    Todas las fechas deben venir con timezone (UTC) — comparar naive contra
    aware tira `TypeError`, a propósito: es mejor romper temprano que comparar
    mal un vencimiento.
    """
    if status == SubscriptionStatus.TRIAL.value:
        fin = trial_ends_at or created_at
        return EffectiveAccess(
            quota=TRIAL_LIMITS,
            period_start=created_at,
            period_end=fin,
            blocked_reason="prueba_vencida" if now >= fin else None,
        )

    if status == SubscriptionStatus.GRACE.value:
        # Gracia: se conserva el MISMO período que ya estaba corriendo — nunca
        # se acredita un cupo nuevo sin confirmar la renovación. Si por algún
        # motivo no hay período seteado, no hay nada que conservar: bloquea.
        if current_period_start is None or current_period_end is None:
            return EffectiveAccess(
                quota=granted_quota,
                period_start=now,
                period_end=now,
                blocked_reason="gracia_sin_periodo",
            )
        limite_gracia = grace_ends_at or (current_period_end + timedelta(days=GRACE_DAYS))
        return EffectiveAccess(
            quota=granted_quota,
            period_start=current_period_start,
            period_end=current_period_end,
            blocked_reason="gracia_vencida" if now >= limite_gracia else None,
        )

    if status == SubscriptionStatus.READ_ONLY.value:
        periodo = _periodo_vigente(current_period_start, current_period_end, now)
        return EffectiveAccess(
            quota=granted_quota, period_start=periodo[0], period_end=periodo[1],
            blocked_reason="solo_lectura",
        )

    if status == SubscriptionStatus.CANCELLED.value:
        periodo = _periodo_vigente(current_period_start, current_period_end, now)
        vencida = current_period_end is None or now >= current_period_end
        return EffectiveAccess(
            quota=granted_quota,
            period_start=periodo[0],
            period_end=periodo[1],
            blocked_reason="cancelada_periodo_vencido" if vencida else None,
        )

    # ACTIVE: único estado no cubierto arriba (el enum es cerrado).
    periodo = _periodo_vigente(current_period_start, current_period_end, now)
    if is_legacy_free:
        return EffectiveAccess(
            quota=granted_quota, period_start=periodo[0], period_end=periodo[1],
            blocked_reason=None,
        )
    if current_period_end is None:
        # Un plan pago `ACTIVE` sin período no pasó por `activate`: no hay
        # nada pago que respaldar. Faltar el dato nunca reabre el acceso.
        return EffectiveAccess(
            quota=granted_quota, period_start=periodo[0], period_end=periodo[1],
            blocked_reason="activa_sin_periodo",
        )
    # Vencido el período, corre la gracia con el MISMO período (y su saldo);
    # pasada la gracia, bloquea. Ninguno de los dos pasos espera a `expire-due`
    # — y por eso el motivo es el MISMO que el de un `GRACE` vencido: para el
    # cliente es una sola situación, y que el nombre dependiera de si corrió
    # un cron es justo lo que evaluar por fecha vino a eliminar.
    limite_gracia = current_period_end + timedelta(days=GRACE_DAYS)
    return EffectiveAccess(
        quota=granted_quota,
        period_start=periodo[0],
        period_end=periodo[1],
        blocked_reason="gracia_vencida" if now >= limite_gracia else None,
    )


def _periodo_vigente(
    start: datetime | None, end: datetime | None, now: datetime
) -> tuple[datetime, datetime]:
    """Período de facturación vigente, o el mes calendario de `now` (UTC) si la
    suscripción todavía no tiene uno seteado (activación manual pendiente)."""
    if start is not None and end is not None:
        return start, end
    primer_dia = now.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0, tzinfo=UTC
    )
    siguiente_mes = (primer_dia.replace(day=28) + timedelta(days=4)).replace(day=1)
    return primer_dia, siguiente_mes
