"""Servicio comercial de suscripciones: activar, renovar, cambiar de plan, cancelar.

Es la ÚNICA puerta por la que una suscripción recibe un período pago. Hoy la usa
`scripts/subscriptions.py` (cobro por transferencia); Mercado Pago entrará por
acá mismo, para que un pago manual y uno automático otorguen exactamente los
mismos derechos.

Reglas (decididas con el dueño, no se re-litigan acá):

* **Activar ≠ renovar.** `activate` es para quien NO tiene un período pago
  vigente ni en gracia (prueba, solo lectura, gracia vencida, cancelada
  vencida, FREE legado): el ciclo arranca HOY y fija el día ancla. `renew` es
  para quien sí lo tiene, y nunca pisa lo que está corriendo.
* **Pago anticipado** (el caso normal con transferencia: se paga unos días
  antes): otorga el período SIGUIENTE (`next_*`) sin tocar el actual ni su
  saldo de cupo. Un solo período por adelantado.
* **Pago durante la gracia**: el período nuevo corre desde el vencimiento
  ANTERIOR. Pagar tarde no regala días ni corre la fecha de cobro.
* **Meses calendario con día ancla** (`add_months_anchored`), un mes por pago.
* **Cambio de plan**: rige desde la próxima renovación; nunca resetea el cupo
  del período en curso ni hay prorrateo. Un downgrade que deje más usuarios
  activos que plazas se rechaza diciendo cuántos sobran — no se borra a nadie.
* **Cancelar**: marca `cancel_at_period_end`. Conserva todo lo pago y ahí
  corta, sin gracia. Renovar antes de ese día deshace la cancelación.

Idempotencia: el UNIQUE `(source, reference)` de `subscription_payments`, con
`INSERT ... ON CONFLICT DO NOTHING RETURNING`, bajo `FOR UPDATE` de la
suscripción. Buscar la referencia antes de escribir no resiste dos ejecuciones
simultáneas; esto sí. La misma referencia con OTROS datos es un error, no un
"ya aplicada": callarlo escondería un pago mal cargado.

Nada de este módulo commitea: el llamador decide (dry-run = rollback).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.subscription import (
    GRACE_DAYS,
    LEGACY_FREE_PLAN_CODE,
    SubscriptionStatus,
    add_months_anchored,
)
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.tenant import PlanDefinition, Subscription, SubscriptionPayment
from app.persistence.models.user import User

SOURCE_MANUAL_TRANSFER: Final = "manual_transfer"

KIND_ACTIVATION: Final = "activation"
KIND_RENEWAL_PREPAID: Final = "renewal_prepaid"
KIND_RENEWAL_IN_GRACE: Final = "renewal_in_grace"

DECISION_PAYMENT: Final = "SUBSCRIPTION_PAYMENT_APPLIED"
DECISION_PLAN_CHANGE: Final = "SUBSCRIPTION_PLAN_CHANGE_SCHEDULED"
DECISION_CANCEL: Final = "SUBSCRIPTION_CANCEL_SCHEDULED"
DECISION_ROLL: Final = "SUBSCRIPTION_PERIOD_ROLLED"


class BillingError(Exception):
    """Operación comercial rechazada. `mensaje` es para el operador."""

    def __init__(self, codigo: str, mensaje: str) -> None:
        self.codigo = codigo
        self.mensaje = mensaje
        super().__init__(f"{codigo}: {mensaje}")


@dataclass(frozen=True)
class ResultadoDePago:
    kind: str
    plan_code: str
    period_start: datetime
    period_end: datetime
    ya_aplicado: bool
    """`True` = esta referencia ya se había acreditado: no se tocó nada."""


def _utc(valor: datetime | None) -> datetime | None:
    if valor is not None and valor.tzinfo is None:
        return valor.replace(tzinfo=UTC)
    return valor


async def _bloquear(session: AsyncSession, subscription_id: uuid.UUID) -> Subscription:
    """La suscripción, con `FOR UPDATE`: serializa las operaciones comerciales
    de un mismo tenant (dos pagos, o un pago y un cambio de plan)."""
    return (
        await session.execute(
            sa.select(Subscription)
            .where(Subscription.subscription_id == subscription_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


def _auditar(
    session: AsyncSession, sub: Subscription, tipo: str, operador: str, datos: dict[str, Any]
) -> None:
    session.add(
        DecisionAuditLog(
            id=uuid.uuid4(),
            tenant_id=sub.tenant_id,
            decision_type=tipo,
            decision_data=datos,
            triggered_by=operador,
            created_at=datetime.now(UTC),
        )
    )


def _aplicar_plan_vigente(sub: Subscription, plan: PlanDefinition) -> None:
    sub.plan_code = plan.plan_code
    sub.seats_included = plan.seats_included
    sub.granted_ia_queries_per_month = plan.ia_queries_per_month
    sub.granted_imports_per_month = plan.imports_per_month
    sub.granted_photo_pdf_reads_per_month = plan.photo_pdf_reads_per_month


def _limpiar_siguiente(sub: Subscription) -> None:
    sub.next_period_end = None
    sub.next_plan_code = None
    sub.next_seats_included = None
    sub.next_granted_ia_queries_per_month = None
    sub.next_granted_imports_per_month = None
    sub.next_granted_photo_pdf_reads_per_month = None


def roll_forward(sub: Subscription, *, now: datetime) -> bool:
    """Si el período actual terminó y hay uno siguiente pago, lo hace vigente.

    Es prolijidad, no requisito: `effective_access` ya lo resuelve por fecha.
    Se llama antes de cada operación comercial (para razonar sobre UN período)
    y desde `expire-due`. `True` = hubo pase.
    """
    fin = _utc(sub.current_period_end)
    if sub.next_period_end is None or fin is None or now < fin:
        return False
    sub.current_period_start = fin
    sub.current_period_end = sub.next_period_end
    if sub.next_plan_code is not None:
        sub.plan_code = sub.next_plan_code
    if sub.next_granted_ia_queries_per_month is not None:
        sub.seats_included = sub.next_seats_included or sub.seats_included
        sub.granted_ia_queries_per_month = sub.next_granted_ia_queries_per_month
        sub.granted_imports_per_month = sub.next_granted_imports_per_month
        sub.granted_photo_pdf_reads_per_month = sub.next_granted_photo_pdf_reads_per_month
    _limpiar_siguiente(sub)
    return True


def _tiene_periodo_pago_en_curso_o_en_gracia(sub: Subscription, now: datetime) -> bool:
    if sub.plan_code == LEGACY_FREE_PLAN_CODE:
        return False
    if sub.status not in (SubscriptionStatus.ACTIVE.value, SubscriptionStatus.GRACE.value):
        return False
    fin = _utc(sub.current_period_end)
    return fin is not None and now < fin + timedelta(days=GRACE_DAYS)


async def _usuarios_activos(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    return (
        await session.execute(
            sa.select(sa.func.count())
            .select_from(User)
            .where(User.tenant_id == tenant_id, User.is_active.is_(True))
        )
    ).scalar_one()


async def _exigir_plazas(session: AsyncSession, sub: Subscription, plan: PlanDefinition) -> None:
    activos = await _usuarios_activos(session, sub.tenant_id)
    if activos > plan.seats_included:
        raise BillingError(
            "SEATS_EXCEEDED",
            f"El plan {plan.plan_code} incluye {plan.seats_included} usuario(s) y la cuenta "
            f"tiene {activos} activos: hay que desactivar {activos - plan.seats_included} "
            "antes. No se borra ni se desactiva a nadie automáticamente.",
        )


async def _plan(session: AsyncSession, plan_code: str) -> PlanDefinition:
    plan = await session.get(PlanDefinition, plan_code)
    if plan is None or plan.plan_code == LEGACY_FREE_PLAN_CODE:
        raise BillingError("PLAN_UNKNOWN", f"No existe el plan pago {plan_code!r}.")
    return plan


async def _registrar_pago(
    session: AsyncSession,
    sub: Subscription,
    *,
    source: str,
    reference: str,
    kind: str,
    plan: PlanDefinition,
    amount_usd: Decimal,
    amount_ars: Decimal,
    operator: str,
    period_start: datetime,
    period_end: datetime,
    notes: str | None,
) -> bool:
    """Reclama `(source, reference)`. `False` = ya existía (no se insertó)."""
    dialecto = session.bind.dialect.name if session.bind is not None else ""
    _insert: Any
    if dialecto == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as _pg_insert  # noqa: PLC0415

        _insert = _pg_insert
    else:
        from sqlalchemy.dialects.sqlite import insert as _sqlite_insert  # noqa: PLC0415

        _insert = _sqlite_insert
    fila = (
        await session.execute(
            _insert(SubscriptionPayment)
            .values(
                id=uuid.uuid4(),
                tenant_id=sub.tenant_id,
                subscription_id=sub.subscription_id,
                source=source,
                reference=reference,
                kind=kind,
                plan_code=plan.plan_code,
                amount_usd=amount_usd,
                amount_ars=amount_ars,
                operator=operator,
                period_start=period_start,
                period_end=period_end,
                granted_json={
                    "seats_included": plan.seats_included,
                    "ia_queries_per_month": plan.ia_queries_per_month,
                    "imports_per_month": plan.imports_per_month,
                    "photo_pdf_reads_per_month": plan.photo_pdf_reads_per_month,
                },
                notes=notes,
            )
            .on_conflict_do_nothing(index_elements=["source", "reference"])
            .returning(SubscriptionPayment.id)
        )
    ).first()
    return fila is not None


async def _pago_existente(
    session: AsyncSession, source: str, reference: str
) -> SubscriptionPayment | None:
    return (
        await session.execute(
            sa.select(SubscriptionPayment).where(
                SubscriptionPayment.source == source, SubscriptionPayment.reference == reference
            )
        )
    ).scalar_one_or_none()


def _ya_aplicado(
    previo: SubscriptionPayment,
    sub: Subscription,
    plan_code: str,
    amount_usd: Decimal,
    amount_ars: Decimal,
) -> ResultadoDePago:
    if (
        previo.tenant_id != sub.tenant_id
        or previo.plan_code != plan_code
        or Decimal(previo.amount_usd) != amount_usd
        or Decimal(previo.amount_ars) != amount_ars
    ):
        raise BillingError(
            "REFERENCE_MISMATCH",
            f"La referencia {previo.reference!r} ya se acreditó con OTROS datos "
            f"(tenant {previo.tenant_id}, plan {previo.plan_code}, USD {previo.amount_usd} / "
            f"ARS {previo.amount_ars}). Revisá la referencia: no se aplicó nada.",
        )
    inicio, fin = _utc(previo.period_start), _utc(previo.period_end)
    assert inicio is not None and fin is not None  # noqa: S101 — columnas NOT NULL
    return ResultadoDePago(
        kind=previo.kind, plan_code=previo.plan_code, period_start=inicio, period_end=fin,
        ya_aplicado=True,
    )


async def apply_payment(
    session: AsyncSession,
    *,
    subscription_id: uuid.UUID,
    expected: str,
    reference: str,
    amount_usd: Decimal,
    amount_ars: Decimal,
    operator: str,
    plan_code: str | None = None,
    source: str = SOURCE_MANUAL_TRANSFER,
    notes: str | None = None,
    now: datetime | None = None,
) -> ResultadoDePago:
    """Acredita un pago. `expected` es ``"activate"`` o ``"renew"``.

    Quien llama dice qué CREE estar haciendo y el servicio lo verifica contra
    el estado real: activar a quien tiene un período corriendo se lo pisaría, y
    "renovar" a quien no lo tiene le inventaría un ancla vieja. El error dice
    qué comando corresponde. `plan_code` es obligatorio al activar; al renovar
    es opcional (default: el cambio programado, o el plan vigente).
    """
    ahora = now or datetime.now(UTC)
    sub = await _bloquear(session, subscription_id)

    previo = await _pago_existente(session, source, reference)
    if previo is not None:
        return _ya_aplicado(previo, sub, plan_code or previo.plan_code, amount_usd, amount_ars)

    if roll_forward(sub, now=ahora):
        _auditar(session, sub, DECISION_ROLL, operator, {"motivo": "previo_a_pago"})

    renovable = _tiene_periodo_pago_en_curso_o_en_gracia(sub, ahora)
    if expected == "activate" and renovable:
        raise BillingError(
            "USE_RENEW",
            "La cuenta ya tiene un período pago vigente o en gracia: activar lo pisaría "
            "(y con él su saldo de cupo). Usá `renew`.",
        )
    if expected == "renew" and not renovable:
        raise BillingError(
            "USE_ACTIVATE",
            "La cuenta no tiene un período pago vigente ni en gracia (prueba, solo lectura, "
            "gracia vencida o FREE): no hay nada que renovar. Usá `activate`.",
        )

    if renovable:
        plan = await _plan(session, plan_code or sub.next_plan_code or sub.plan_code)
        if plan.plan_code != sub.plan_code:
            await _exigir_plazas(session, sub, plan)
        fin_actual = _utc(sub.current_period_end)
        assert fin_actual is not None  # noqa: S101 — lo garantiza `renovable`
        ancla = sub.billing_anchor_day or fin_actual.day
        nuevo_fin = add_months_anchored(fin_actual, anchor_day=ancla)
        if ahora < fin_actual:
            if sub.next_period_end is not None:
                raise BillingError(
                    "NEXT_PERIOD_ALREADY_PAID",
                    f"Ya hay un período siguiente pago (hasta {sub.next_period_end:%Y-%m-%d}). "
                    "Se acepta un solo período por adelantado.",
                )
            kind = KIND_RENEWAL_PREPAID
        else:
            kind = KIND_RENEWAL_IN_GRACE
        inicio = fin_actual
    else:
        if plan_code is None:
            raise BillingError("PLAN_REQUIRED", "Activar exige indicar el plan.")
        plan = await _plan(session, plan_code)
        await _exigir_plazas(session, sub, plan)
        kind, inicio, ancla = KIND_ACTIVATION, ahora, ahora.day
        nuevo_fin = add_months_anchored(ahora, anchor_day=ancla)

    if not await _registrar_pago(
        session, sub, source=source, reference=reference, kind=kind, plan=plan,
        amount_usd=amount_usd, amount_ars=amount_ars, operator=operator,
        period_start=inicio, period_end=nuevo_fin, notes=notes,
    ):
        # Otra ejecución reclamó la referencia entre nuestro SELECT y el INSERT
        # (sólo posible desde OTRA suscripción: la nuestra está bloqueada).
        otro = await _pago_existente(session, source, reference)
        assert otro is not None  # noqa: S101 — el conflicto prueba que existe
        return _ya_aplicado(otro, sub, plan.plan_code, amount_usd, amount_ars)

    antes = {"plan_code": sub.plan_code, "status": sub.status}
    sub.billing_anchor_day = ancla
    sub.plan_price_usd_reference = amount_usd
    sub.plan_price_ars = amount_ars
    sub.cancel_at_period_end = False  # pagar deshace una cancelación programada
    sub.grace_ends_at = None
    if kind == KIND_RENEWAL_PREPAID:
        # El período en curso NO se toca: ni fechas, ni condiciones, ni saldo.
        sub.next_period_end = nuevo_fin
        sub.next_plan_code = plan.plan_code
        sub.next_seats_included = plan.seats_included
        sub.next_granted_ia_queries_per_month = plan.ia_queries_per_month
        sub.next_granted_imports_per_month = plan.imports_per_month
        sub.next_granted_photo_pdf_reads_per_month = plan.photo_pdf_reads_per_month
    else:
        _aplicar_plan_vigente(sub, plan)
        sub.status = SubscriptionStatus.ACTIVE.value
        sub.current_period_start = inicio
        sub.current_period_end = nuevo_fin
        _limpiar_siguiente(sub)

    _auditar(
        session, sub, DECISION_PAYMENT, operator,
        {
            "source": source, "reference": reference, "kind": kind, "before": antes,
            "plan_code": plan.plan_code, "amount_usd": str(amount_usd),
            "amount_ars": str(amount_ars), "period_start": inicio.isoformat(),
            "period_end": nuevo_fin.isoformat(), "anchor_day": ancla, "notes": notes,
        },
    )
    return ResultadoDePago(
        kind=kind, plan_code=plan.plan_code, period_start=inicio, period_end=nuevo_fin,
        ya_aplicado=False,
    )


async def schedule_plan_change(
    session: AsyncSession,
    *,
    subscription_id: uuid.UUID,
    plan_code: str,
    operator: str,
    now: datetime | None = None,
) -> str:
    """Programa el plan de la PRÓXIMA renovación. No cobra ni toca el período
    en curso. Devuelve el plan programado."""
    ahora = now or datetime.now(UTC)
    sub = await _bloquear(session, subscription_id)
    roll_forward(sub, now=ahora)
    if not _tiene_periodo_pago_en_curso_o_en_gracia(sub, ahora):
        raise BillingError(
            "NO_PAID_PERIOD",
            "Sin período pago vigente no hay próxima renovación que programar: el plan se "
            "elige al activar.",
        )
    if sub.next_period_end is not None:
        raise BillingError(
            "NEXT_PERIOD_ALREADY_PAID",
            "El período siguiente ya está pago con sus condiciones: el cambio se programa "
            "después de que empiece.",
        )
    plan = await _plan(session, plan_code)
    await _exigir_plazas(session, sub, plan)
    sub.next_plan_code = None if plan.plan_code == sub.plan_code else plan.plan_code
    _auditar(
        session, sub, DECISION_PLAN_CHANGE, operator,
        {"from": sub.plan_code, "to": plan.plan_code},
    )
    return plan.plan_code


async def schedule_cancellation(
    session: AsyncSession,
    *,
    subscription_id: uuid.UUID,
    operator: str,
    reason: str | None = None,
    now: datetime | None = None,
) -> datetime:
    """Cancela la RENOVACIÓN. Devuelve hasta cuándo conserva el acceso pago."""
    ahora = now or datetime.now(UTC)
    sub = await _bloquear(session, subscription_id)
    roll_forward(sub, now=ahora)
    if not _tiene_periodo_pago_en_curso_o_en_gracia(sub, ahora):
        raise BillingError(
            "NO_PAID_PERIOD",
            "No hay un período pago que conservar. Para cerrar la cuenta usá "
            "`set-status --status CANCELLED`.",
        )
    sub.cancel_at_period_end = True
    sub.next_plan_code = None if sub.next_period_end is None else sub.next_plan_code
    hasta = _utc(sub.next_period_end) or _utc(sub.current_period_end)
    assert hasta is not None  # noqa: S101
    _auditar(
        session, sub, DECISION_CANCEL, operator,
        {"access_until": hasta.isoformat(), "reason": reason},
    )
    return hasta
