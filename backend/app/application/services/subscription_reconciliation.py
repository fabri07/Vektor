"""Conciliación de reservas de cupo que quedaron en `RESERVED`.

Una reserva se cuelga cuando el proceso muere entre reservar y resolver. Mientras
está colgada le resta cupo al cliente sin haberle dado nada, así que hay que
cerrarla — pero **nunca sólo por antigüedad**: liberar una reserva cuyo trabajo
sigue vivo lo dejaría ejecutando sin cupo que lo respalde, y liberar una cuyo
trabajo terminó bien sería regalar la unidad. Cada clase de operación decide con
la evidencia durable que deja:

* **Importación en segundo plano** (`import-attempt:{id}`): manda el intento.
  `COMPLETADO` → confirmar · `FALLADO`/`CANCELADO` → liberar · vivo
  (`PENDIENTE`/`EJECUTANDO`) → no tocar, tenga la edad que tenga. Sin intento
  (la fila no existe) → ambigua.
* **Chat** (`chat:{trace_id}`): si `decision_audit_log` tiene una fila de ese
  `trace_id` con tokens, el turno corrió → confirmar. Si no la tiene Y pasó el
  plazo, liberar: un turno de chat no tiene lease ni reintento, vive dentro de
  UN request HTTP, así que pasado el plazo es imposible que siga corriendo. Eso
  —no la edad sola— es lo que demuestra el abandono.
* **Lectura foto/PDF** (`read:{uuid}`): mismo argumento del request único; no
  deja resultado durable propio (sólo sugiere), así que pasado el plazo se
  libera a favor del cliente.
* **Cualquier otra clave**: ambigua. Se conserva y se reporta; la resuelve una
  persona (`resolver_manual`), con motivo y operador en la auditoría.

Las importaciones síncronas (`import:{file}:{token}`) no aparecen acá: reservan
y confirman en la misma transacción que los datos, así que no pueden colgarse.

Cada resolución usa el CAS de `subscription_service` (`WHERE state='RESERVED'`):
si el dueño real la resuelve al mismo tiempo, gana uno solo y el otro es no-op.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import subscription_service
from app.domain.import_attempt import CANCELADO, COMPLETADO, FALLADO
from app.domain.subscription import (
    CHAT_OPERATION_PREFIX,
    IMPORT_ATTEMPT_OPERATION_PREFIX,
    READ_OPERATION_PREFIX,
    QuotaResource,
)
from app.observability.logger import get_logger
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.import_attempt import ImportAttempt
from app.persistence.models.tenant import SubscriptionQuotaReservation

logger = get_logger(__name__)

#: Pasado esto, un request HTTP de chat o de lectura no puede seguir vivo (los
#: timeouts del proveedor y del proxy son de minutos). Holgado a propósito.
PLAZO_REQUEST: Final[timedelta] = timedelta(hours=2)

#: Tope por corrida: transacciones cortas y trabajo acotado.
LOTE: Final[int] = 1000

CONFIRMAR: Final = "confirmar"
LIBERAR: Final = "liberar"
NO_TOCAR: Final = "no_tocar"
AMBIGUA: Final = "ambigua"

DECISION_RECONCILE: Final = "SUBSCRIPTION_RESERVATION_RECONCILED"


@dataclass(frozen=True)
class Veredicto:
    reservation_id: uuid.UUID
    tenant_id: uuid.UUID
    resource: str
    operation_id: str
    accion: str
    motivo: str


@dataclass
class ResultadoDeConciliacion:
    veredictos: list[Veredicto] = field(default_factory=list)
    aplicadas: int = 0

    def contar(self, accion: str) -> int:
        return sum(1 for v in self.veredictos if v.accion == accion)


def _utc(valor: datetime) -> datetime:
    return valor if valor.tzinfo is not None else valor.replace(tzinfo=UTC)


async def _veredicto(
    session: AsyncSession, reserva: SubscriptionQuotaReservation, ahora: datetime
) -> tuple[str, str]:
    op = reserva.operation_id
    vencio_el_plazo = ahora - _utc(reserva.created_at) >= PLAZO_REQUEST

    if op.startswith(IMPORT_ATTEMPT_OPERATION_PREFIX):
        try:
            attempt_id = uuid.UUID(op.removeprefix(IMPORT_ATTEMPT_OPERATION_PREFIX))
        except ValueError:
            return AMBIGUA, "clave_de_intento_ilegible"
        estado = (
            await session.execute(
                sa.select(ImportAttempt.status).where(
                    ImportAttempt.id == attempt_id,
                    ImportAttempt.tenant_id == reserva.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if estado is None:
            return AMBIGUA, "intento_inexistente"
        if estado == COMPLETADO:
            return CONFIRMAR, "intento_completado"
        if estado in (FALLADO, CANCELADO):
            return LIBERAR, f"intento_{estado.lower()}"
        return NO_TOCAR, f"intento_vivo_{estado.lower()}"

    if op.startswith(CHAT_OPERATION_PREFIX):
        trace_id = op.removeprefix(CHAT_OPERATION_PREFIX)
        auditado = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(DecisionAuditLog)
                .where(
                    DecisionAuditLog.trace_id == trace_id,
                    DecisionAuditLog.tenant_id == reserva.tenant_id,
                    DecisionAuditLog.tokens_total > 0,
                )
            )
        ).scalar_one()
        if auditado:
            return CONFIRMAR, "turno_auditado_con_tokens"
        if vencio_el_plazo:
            return LIBERAR, "request_terminado_sin_resultado"
        return NO_TOCAR, "request_posiblemente_vivo"

    if op.startswith(READ_OPERATION_PREFIX):
        if vencio_el_plazo:
            return LIBERAR, "request_terminado_sin_resultado"
        return NO_TOCAR, "request_posiblemente_vivo"

    return AMBIGUA, "clave_desconocida"


async def reconcile(
    session: AsyncSession,
    *,
    apply: bool,
    now: datetime | None = None,
    limit: int = LOTE,
) -> ResultadoDeConciliacion:
    """Evalúa (y con `apply` resuelve) un lote de reservas en `RESERVED`.

    No commitea: el llamador decide (script con `--apply`, o el job). Cada
    resolución queda en `decision_audit_log`, en la misma transacción.
    """
    ahora = now or datetime.now(UTC)
    reservas = (
        (
            await session.execute(
                sa.select(SubscriptionQuotaReservation)
                .where(SubscriptionQuotaReservation.state == "RESERVED")
                .order_by(SubscriptionQuotaReservation.created_at)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    resultado = ResultadoDeConciliacion()
    for reserva in reservas:
        accion, motivo = await _veredicto(session, reserva, ahora)
        veredicto = Veredicto(
            reservation_id=reserva.id,
            tenant_id=reserva.tenant_id,
            resource=reserva.resource,
            operation_id=reserva.operation_id,
            accion=accion,
            motivo=motivo,
        )
        resultado.veredictos.append(veredicto)
        if apply and accion in (CONFIRMAR, LIBERAR):
            await _resolver(session, veredicto, operador="conciliacion_automatica", ahora=ahora)
            resultado.aplicadas += 1
    if resultado.contar(AMBIGUA):
        logger.error(
            "subscription.reservations.ambiguas",
            cantidad=resultado.contar(AMBIGUA),
        )
    return resultado


async def resolver_manual(
    session: AsyncSession,
    *,
    reservation_id: uuid.UUID,
    accion: str,
    motivo: str,
    operador: str,
) -> bool:
    """Resolución a mano de UNA reserva (las ambiguas). Auditada. No commitea."""
    if accion not in (CONFIRMAR, LIBERAR):
        raise ValueError(f"acción inválida: {accion!r}")
    reserva = await session.get(SubscriptionQuotaReservation, reservation_id)
    if reserva is None or reserva.state != "RESERVED":
        return False
    veredicto = Veredicto(
        reservation_id=reserva.id,
        tenant_id=reserva.tenant_id,
        resource=reserva.resource,
        operation_id=reserva.operation_id,
        accion=accion,
        motivo=motivo,
    )
    return await _resolver(session, veredicto, operador=operador, ahora=datetime.now(UTC))


async def _resolver(
    session: AsyncSession, veredicto: Veredicto, *, operador: str, ahora: datetime
) -> bool:
    resolver = (
        subscription_service.commit
        if veredicto.accion == CONFIRMAR
        else subscription_service.release
    )
    hecho = await resolver(
        session,
        tenant_id=veredicto.tenant_id,
        resource=QuotaResource(veredicto.resource),
        operation_id=veredicto.operation_id,
        autocommit=False,
    )
    if hecho:
        session.add(
            DecisionAuditLog(
                id=uuid.uuid4(),
                tenant_id=veredicto.tenant_id,
                decision_type=DECISION_RECONCILE,
                decision_data={
                    "reservation_id": str(veredicto.reservation_id),
                    "resource": veredicto.resource,
                    "operation_id": veredicto.operation_id,
                    "accion": veredicto.accion,
                    "motivo": veredicto.motivo,
                    "operador": operador,
                },
                triggered_by=operador,
                created_at=ahora,
            )
        )
    return hecho
