"""Abrir, cerrar y conciliar el turno de una caja (B8).

Lo que este módulo protege
--------------------------

1. **Un arqueo cerrado no se reescribe.** Es el invariante del bloque. Una venta
   que sincroniza después del cierre —el caso normal de una caja que estuvo sin
   red— no cambia el ``expected_cash_ars`` que el cajero firmó: se registra como
   ``PosLateSale`` y alguien la concilia. Reescribirlo convertiría el arqueo en
   un número que cambia solo, y entonces no sirve para nada.

2. **El turno lo identifica el cliente.** ``client_session_id`` lo genera la
   caja al abrir, incluso sin red. El servidor lo **adopta**: si llega una venta
   de un turno que no existe, se crea abierto. Exigir que el turno existiera
   antes rompería el arranque sin conexión de B7b.

3. **Al cajón sólo entra el efectivo.** El esperado es
   ``fondo inicial + efectivo cobrado en el turno``. Tarjeta, transferencia y
   fiado no están en el cajón. Se lee de ``pos_tenders``, así que en un pago
   mixto (B9) entra sólo la parte en efectivo.

4. **El arqueo del turno no es el arqueo del día.** ``cash_closes`` es uno por
   (tenant, fecha) y es del negocio entero; esto es por cajero y por caja. Son
   granos distintos y **no se cruzan**: sumarlos contaría dos veces.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services._savepoint import (
    SavepointConflictError,
    guarded_savepoint,
)
from app.persistence.models.pos_cash_session import (
    ESTADO_ABIERTO,
    ESTADO_CERRADO,
    PosCashSession,
    PosLateSale,
)
from app.persistence.models.pos_operation import PosOperation, PosTender

#: El único método que termina en el cajón.
METODO_EFECTIVO = "cash"

#: Una operación anulada no aporta plata: su ticket existe, pero no cuenta.
ESTADO_OPERACION_VIVA = "COMPLETED"


class SesionYaCerradaError(Exception):
    """Se intentó cerrar un turno que ya estaba cerrado."""


_UNICO_DE_TURNO = "uq_pos_cash_sessions_client_session"


def _clasificar_unico_de_turno(exc: IntegrityError) -> str | None:
    """Reconoce SÓLO el UNIQUE ``(tenant, client_session_id)``.

    Cualquier otra violación (una FK, un CHECK) se propaga: convertirla en "ya
    existía, la reuso" escondería un error real.
    """
    mensaje = str(exc.orig)
    if _UNICO_DE_TURNO in mensaje or (
        "pos_cash_sessions.client_session_id" in mensaje and "UNIQUE" in mensaje
    ):
        return _UNICO_DE_TURNO
    return None


async def obtener_por_client_id(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    client_session_id: str,
    *,
    para_actualizar: bool = False,
) -> PosCashSession | None:
    stmt = select(PosCashSession).where(
        PosCashSession.tenant_id == tenant_id,
        PosCashSession.client_session_id == client_session_id,
    )
    if para_actualizar:
        # `populate_existing` por la misma razón que en `pos_discrepancy_service`:
        # si la fila ya está en la identity map, el candado se tomaría bien y el
        # `status` se leería viejo — justo el caso que el candado cubre.
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return (await session.execute(stmt)).scalar_one_or_none()


async def abrir_o_adoptar(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    client_session_id: str,
    *,
    user_id: uuid.UUID | None,
    terminal_id: uuid.UUID | None,
    opening_float_ars: Decimal = Decimal("0"),
    ahora: datetime | None = None,
) -> PosCashSession:
    """Devuelve el turno, creándolo si la caja lo abrió sin red.

    Idempotente y a prueba de la carrera real: la apertura explícita y la venta
    que llega primero pueden ejecutarse a la vez, y el UNIQUE
    ``(tenant, client_session_id)`` decide. El perdedor re-lee en vez de fallar.

    **No reabre un turno cerrado.** Si ya se firmó su arqueo, una venta tardía no
    lo vuelve a abrir: se devuelve tal cual y el llamador la registra como
    tardía.
    """
    existente = await obtener_por_client_id(session, tenant_id, client_session_id)
    if existente is not None:
        return existente

    nueva = PosCashSession(
        tenant_id=tenant_id,
        client_session_id=client_session_id,
        user_id=user_id,
        terminal_id=terminal_id,
        opening_float_ars=opening_float_ars,
        opened_at=ahora or datetime.now(UTC),
        status=ESTADO_ABIERTO,
    )
    # Dentro de un SAVEPOINT y no con ``session.rollback()``: quien llama puede
    # ser ``create_pos_operation``, que ya hizo flush de la venta, sus líneas,
    # sus pagos y el reclamo de idempotencia. Un rollback de la transacción
    # entera ante la carrera se llevaba puesta la venta, y el handler seguía
    # adelante con objetos desasociados. El savepoint deshace sólo este INSERT.
    try:
        async with guarded_savepoint(session, _clasificar_unico_de_turno):
            session.add(nueva)
    except SavepointConflictError:
        # Otra transacción lo creó en el medio. Su fila es tan válida como ésta.
        ganadora = await obtener_por_client_id(session, tenant_id, client_session_id)
        if ganadora is None:  # pragma: no cover - sólo si el UNIQUE no existiera
            raise
        return ganadora
    return nueva


async def efectivo_del_turno(
    session: AsyncSession, tenant_id: uuid.UUID, client_session_id: str
) -> Decimal:
    """Cuánto efectivo cobró el turno, según las ventas VIVAS que le pertenecen.

    Se lee de ``pos_tenders`` y no de ``payment_method`` de la venta porque el
    tender es donde vive el monto por método: en un pago mixto (B9) la venta
    lleva el marcador ``mixed`` y sólo el tender sabe cuánto fue en efectivo.

    Una operación anulada no suma: su plata volvió al cliente.
    """
    total = (
        await session.execute(
            select(func.coalesce(func.sum(PosTender.amount_ars), 0)).where(
                PosTender.tenant_id == tenant_id,
                PosTender.payment_method == METODO_EFECTIVO,
                PosTender.operation_id.in_(
                    select(PosOperation.id).where(
                        PosOperation.tenant_id == tenant_id,
                        PosOperation.client_session_id == client_session_id,
                        PosOperation.status == ESTADO_OPERACION_VIVA,
                    )
                ),
            )
        )
    ).scalar_one()
    return Decimal(str(total))


async def cerrar(
    session: AsyncSession,
    sesion: PosCashSession,
    *,
    counted_cash_ars: Decimal,
    user_id: uuid.UUID | None,
    notes: str | None = None,
    ahora: datetime | None = None,
) -> PosCashSession:
    """Congela el arqueo del turno. Después de esto sus tres números no cambian.

    ``esperado = fondo inicial + efectivo cobrado``. La diferencia se guarda
    calculada y no derivada al leer: es lo que el cajero firmó, y recalcularla
    más tarde sobre datos que pueden haber cambiado daría otro número.
    """
    if sesion.status == ESTADO_CERRADO:
        raise SesionYaCerradaError(str(sesion.id))

    cobrado = await efectivo_del_turno(session, sesion.tenant_id, sesion.client_session_id)
    esperado = Decimal(sesion.opening_float_ars) + cobrado
    sesion.expected_cash_ars = esperado
    sesion.counted_cash_ars = counted_cash_ars
    sesion.difference_ars = counted_cash_ars - esperado
    sesion.closed_at = ahora or datetime.now(UTC)
    sesion.closed_by_user_id = user_id
    sesion.notes = notes
    sesion.status = ESTADO_CERRADO
    await session.flush()
    return sesion


async def registrar_tardia(
    session: AsyncSession,
    sesion: PosCashSession,
    operacion: PosOperation,
    *,
    cash_ars: Decimal,
    ahora: datetime | None = None,
) -> PosLateSale | None:
    """Una venta que llegó después del cierre de su turno.

    Devuelve ``None`` si ya estaba registrada: el reintento de una venta que el
    servidor ya tiene no puede duplicar su fila de conciliación.
    """
    ya = (
        await session.execute(
            select(PosLateSale).where(PosLateSale.operation_id == operacion.id)
        )
    ).scalar_one_or_none()
    if ya is not None:
        return None
    tardia = PosLateSale(
        tenant_id=sesion.tenant_id,
        session_id=sesion.id,
        operation_id=operacion.id,
        cash_ars=cash_ars,
        created_at=ahora or datetime.now(UTC),
    )
    session.add(tardia)
    await session.flush()
    return tardia


async def listar(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    solo_abiertos: bool = False,
    user_id: uuid.UUID | None = None,
    limite: int = 50,
) -> list[PosCashSession]:
    """Los turnos del negocio, del más nuevo al más viejo.

    ``user_id`` acota a los de un cajero: es lo que hace que un cajero vea los
    suyos y no los de sus compañeros, mismo criterio que el ticket en B12.
    """
    stmt = select(PosCashSession).where(PosCashSession.tenant_id == tenant_id)
    if solo_abiertos:
        stmt = stmt.where(PosCashSession.status == ESTADO_ABIERTO)
    if user_id is not None:
        stmt = stmt.where(PosCashSession.user_id == user_id)
    stmt = stmt.order_by(PosCashSession.opened_at.desc()).limit(limite)
    return list((await session.execute(stmt)).scalars().all())


async def listar_tardias(
    session: AsyncSession, tenant_id: uuid.UUID, session_id: uuid.UUID
) -> list[PosLateSale]:
    return list(
        (
            await session.execute(
                select(PosLateSale)
                .where(
                    PosLateSale.tenant_id == tenant_id,
                    PosLateSale.session_id == session_id,
                )
                .order_by(PosLateSale.created_at)
            )
        )
        .scalars()
        .all()
    )


__all__ = [
    "METODO_EFECTIVO",
    "SesionYaCerradaError",
    "abrir_o_adoptar",
    "cerrar",
    "efectivo_del_turno",
    "listar",
    "listar_tardias",
    "obtener_por_client_id",
    "registrar_tardia",
]
