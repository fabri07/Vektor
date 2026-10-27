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

4. **Un reintegro sale del cajón de HOY** (A-4). Anular un ticket de otro turno
   u otro día deja un ``PosCashRefund``: resta en el turno abierto de cuyo
   cajón salió la plata y le devuelve al turno de la venta lo que su cajón
   todavía tiene. El arqueo firmado del turno de la venta no se toca.

5. **El arqueo del turno no es el arqueo del día.** ``cash_closes`` es uno por
   (tenant, fecha) y es del negocio entero; esto es por cajero y por caja. Son
   granos distintos y **no se cruzan**: sumarlos contaría dos veces.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services._savepoint import (
    SavepointConflictError,
    guarded_savepoint,
)
from app.persistence.models.pos_cash_session import (
    ESTADO_ABANDONADO,
    ESTADO_ABIERTO,
    ESTADO_CERRADO,
    PosCashRefund,
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
    compartido: bool = False,
) -> PosCashSession | None:
    stmt = select(PosCashSession).where(
        PosCashSession.tenant_id == tenant_id,
        PosCashSession.client_session_id == client_session_id,
    )
    if compartido and not para_actualizar:
        # FOR SHARE: la venta no cambia el turno, pero tiene que impedir que se
        # cierre mientras ella está en vuelo. Ver ``abrir_o_adoptar``.
        stmt = stmt.with_for_update(read=True).execution_options(populate_existing=True)
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
    fondo_declarado: Decimal | None = None,
    ahora: datetime | None = None,
) -> PosCashSession:
    """Devuelve el turno, creándolo si la caja lo abrió sin red.

    Idempotente y a prueba de la carrera real: la apertura explícita y la venta
    que llega primero pueden ejecutarse a la vez, y el UNIQUE
    ``(tenant, client_session_id)`` decide. El perdedor re-lee en vez de fallar.

    ``fondo_declarado`` lo pasa SÓLO la apertura explícita; una venta que adopta
    el turno pasa ``None``. El fondo se declara por cualquiera de los tres
    caminos —el turno ya existía, se creó acá, o se perdió la carrera y se
    releyó—, porque el caso que esto resuelve es justamente el de la venta que
    llegó primero y adoptó el turno con fondo $0. Gana la primera declaración:
    el llamador recibe el turno con el fondo REAL, que puede no ser el que
    mandó.

    **No reabre un turno cerrado.** Si ya se firmó su arqueo, una venta tardía no
    lo vuelve a abrir: se devuelve tal cual y el llamador la registra como
    tardía.
    """
    momento = ahora or datetime.now(UTC)
    existente = await obtener_por_client_id(session, tenant_id, client_session_id)
    if existente is None:
        nueva = PosCashSession(
            tenant_id=tenant_id,
            client_session_id=client_session_id,
            user_id=user_id,
            terminal_id=terminal_id,
            opening_float_ars=(
                fondo_declarado if fondo_declarado is not None else Decimal("0")
            ),
            float_declared_at=momento if fondo_declarado is not None else None,
            opened_at=momento,
            status=ESTADO_ABIERTO,
        )
        # Dentro de un SAVEPOINT y no con ``session.rollback()``: quien llama
        # puede ser ``create_pos_operation``, que ya hizo flush de la venta, sus
        # líneas, sus pagos y el reclamo de idempotencia. Un rollback de la
        # transacción entera ante la carrera se llevaba puesta la venta, y el
        # handler seguía adelante con objetos desasociados. El savepoint deshace
        # sólo este INSERT.
        try:
            async with guarded_savepoint(session, _clasificar_unico_de_turno):
                session.add(nueva)
        except SavepointConflictError:
            # Otra transacción lo creó en el medio. Su fila es tan válida como ésta.
            existente = await obtener_por_client_id(session, tenant_id, client_session_id)
            if existente is None:  # pragma: no cover - sólo si el UNIQUE no existiera
                raise
        else:
            return nueva

    if fondo_declarado is None:
        # La VENTA relee el turno con FOR SHARE. Sin candado, una venta que lo
        # leyó abierto podía commitear DESPUÉS de que el cierre (``FOR UPDATE``)
        # sumara los tenders commiteados: quedaba fuera del arqueo y tampoco
        # figuraba como tardía. Con el candado compartido, el cierre espera a
        # que la venta commitee (y la suma), o la venta espera al cierre y ve el
        # turno cerrado (y queda como tardía). Varias ventas no se traban entre
        # sí: el candado es compartido.
        releida = await obtener_por_client_id(
            session, tenant_id, client_session_id, compartido=True
        )
        return releida if releida is not None else existente
    return await _declarar_fondo(session, existente, fondo_declarado, momento)


async def _declarar_fondo(
    session: AsyncSession,
    sesion: PosCashSession,
    fondo: Decimal,
    momento: datetime,
) -> PosCashSession:
    """Fija el fondo de un turno que lo tenía por adopción. Con candado.

    Sin el ``FOR UPDATE``, dos aperturas simultáneas con fondos distintos verían
    las dos ``float_declared_at IS NULL`` y la segunda pisaría a la primera. Con
    él se serializan: la segunda relee, ve el fondo ya declarado y lo devuelve
    sin tocarlo. Un turno cerrado o abandonado no se toca: su arqueo ya está.
    """
    bloqueada = await obtener_por_client_id(
        session, sesion.tenant_id, sesion.client_session_id, para_actualizar=True
    )
    if bloqueada is None:  # pragma: no cover - la fila no se borra
        return sesion
    if bloqueada.esta_abierto and bloqueada.float_declared_at is None:
        bloqueada.opening_float_ars = fondo
        bloqueada.float_declared_at = momento
        await session.flush()
    return bloqueada


async def efectivo_del_turno(
    session: AsyncSession, tenant_id: uuid.UUID, client_session_id: str
) -> Decimal:
    """Cuánto efectivo cobró el turno, según las ventas VIVAS que le pertenecen.

    Se lee de ``pos_tenders`` y no de ``payment_method`` de la venta porque el
    tender es donde vive el monto por método: en un pago mixto (B9) la venta
    lleva el marcador ``mixed`` y sólo el tender sabe cuánto fue en efectivo.

    Una operación anulada no suma: su plata volvió al cliente.

    A-4: si la plata se le devolvió desde OTRO turno (``PosCashRefund``), el
    cajón de este todavía la tiene y se vuelve a sumar; y lo que se devolvió
    desde ESTE cajón por un ticket ajeno o de otro día, se resta.
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
    ajuste = (await ajustes_por_reintegro(session, tenant_id, [client_session_id])).get(
        client_session_id, Decimal("0")
    )
    return Decimal(str(total)) + ajuste


async def ajustes_por_reintegro(
    session: AsyncSession, tenant_id: uuid.UUID, client_session_ids: list[str]
) -> dict[str, Decimal]:
    """Lo que los reintegros (A-4) le suman o le restan al cajón de cada turno.

    ``+`` lo devuelto por un ticket de este turno (su cajón lo cobró y lo sigue
    teniendo: la plata salió de otro); ``-`` lo devuelto DESDE este turno. Un
    reintegro del mismo turno de la venta se cancela con su propio ticket
    anulado: queda ``-`` su efectivo, que es lo que salió del cajón.
    """
    if not client_session_ids:
        return {}
    ajustes: dict[str, Decimal] = {}
    de_origen = (
        await session.execute(
            select(
                PosCashRefund.origin_client_session_id,
                func.coalesce(func.sum(PosCashRefund.cash_ars), 0),
            )
            .where(
                PosCashRefund.tenant_id == tenant_id,
                PosCashRefund.origin_client_session_id.in_(client_session_ids),
            )
            .group_by(PosCashRefund.origin_client_session_id)
        )
    ).tuples().all()
    for cid, monto in de_origen:
        if cid is not None:
            ajustes[cid] = ajustes.get(cid, Decimal("0")) + Decimal(str(monto))
    desde = (
        await session.execute(
            select(
                PosCashSession.client_session_id,
                func.coalesce(func.sum(PosCashRefund.cash_ars), 0),
            )
            .join(PosCashSession, PosCashSession.id == PosCashRefund.session_id)
            .where(
                PosCashRefund.tenant_id == tenant_id,
                PosCashSession.tenant_id == tenant_id,
                PosCashSession.client_session_id.in_(client_session_ids),
            )
            .group_by(PosCashSession.client_session_id)
        )
    ).tuples().all()
    for cid, monto in desde:
        ajustes[cid] = ajustes.get(cid, Decimal("0")) - Decimal(str(monto))
    return ajustes


async def turno_abierto_para_reintegro(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    solo_propios: bool,
    terminal_id: uuid.UUID | None,
    client_session_id: str | None,
) -> PosCashSession | None:
    """El turno ABIERTO de cuyo cajón sale un reintegro (A-4). Sin candado.

    Si la caja lo dice (``client_session_id``) es ése, y tiene que estar abierto
    y —para un cajero— ser suyo. Si no, el único abierto de esta caja (o de
    este usuario, sin caja). Con más de uno no se adivina: elegir mal mete la
    plata en el arqueo de otro cajón. ``None`` = no hay uno inequívoco.
    """
    if client_session_id is not None:
        sesion = await obtener_por_client_id(session, tenant_id, client_session_id)
        if sesion is None or not sesion.esta_abierto:
            return None
        if solo_propios and sesion.user_id != user_id:
            return None
        return sesion
    stmt = select(PosCashSession).where(
        PosCashSession.tenant_id == tenant_id,
        PosCashSession.status == ESTADO_ABIERTO,
    )
    if terminal_id is not None:
        stmt = stmt.where(PosCashSession.terminal_id == terminal_id)
    else:
        stmt = stmt.where(PosCashSession.user_id == user_id)
    if solo_propios:
        stmt = stmt.where(PosCashSession.user_id == user_id)
    candidatos = list((await session.execute(stmt.limit(2))).scalars().all())
    return candidatos[0] if len(candidatos) == 1 else None


async def registrar_reintegro(
    session: AsyncSession,
    operacion: PosOperation,
    *,
    sesion: PosCashSession | None,
    cash_ars: Decimal,
    refund_date: date,
    user_id: uuid.UUID | None,
    ahora: datetime | None = None,
) -> PosCashRefund:
    """Deja asentado que el efectivo de ``operacion`` salió hoy del cajón (A-4).

    El llamador ya bloqueó el turno con ``FOR SHARE`` y vio que sigue abierto:
    así un cierre en vuelo o lo suma, o espera a que esto commitee.
    """
    reintegro = PosCashRefund(
        tenant_id=operacion.tenant_id,
        operation_id=operacion.id,
        session_id=sesion.id if sesion is not None else None,
        origin_client_session_id=operacion.client_session_id,
        sale_date=operacion.operation_date.date(),
        refund_date=refund_date,
        cash_ars=cash_ars,
        user_id=user_id,
        created_at=ahora or datetime.now(UTC),
    )
    session.add(reintegro)
    await session.flush()
    return reintegro


async def resumen_de_turnos(
    session: AsyncSession, tenant_id: uuid.UUID, client_session_ids: list[str]
) -> dict[str, tuple[Decimal, int]]:
    """Efectivo cobrado y cantidad de tickets vivos de varios turnos, en DOS
    consultas y no dos por turno: el panel del encargado lista hasta 200.

    Misma regla que ``efectivo_del_turno`` —tenders en efectivo de operaciones
    vivas, más los ajustes por reintegro (A-4, dos consultas más, también en
    lote)—; la cantidad de tickets es lo que deja distinguir un turno basura
    (cero ventas) de uno que hay que contar.
    """
    if not client_session_ids:
        return {}
    efectivo = dict(
        (
            await session.execute(
                select(
                    PosOperation.client_session_id,
                    func.coalesce(func.sum(PosTender.amount_ars), 0),
                )
                .join(PosTender, PosTender.operation_id == PosOperation.id)
                .where(
                    PosOperation.tenant_id == tenant_id,
                    PosOperation.client_session_id.in_(client_session_ids),
                    PosOperation.status == ESTADO_OPERACION_VIVA,
                    PosTender.payment_method == METODO_EFECTIVO,
                )
                .group_by(PosOperation.client_session_id)
            )
        ).tuples().all()
    )
    tickets = dict(
        (
            await session.execute(
                select(PosOperation.client_session_id, func.count(PosOperation.id))
                .where(
                    PosOperation.tenant_id == tenant_id,
                    PosOperation.client_session_id.in_(client_session_ids),
                    PosOperation.status == ESTADO_OPERACION_VIVA,
                )
                .group_by(PosOperation.client_session_id)
            )
        ).tuples().all()
    )
    ajustes = await ajustes_por_reintegro(session, tenant_id, client_session_ids)
    return {
        cid: (
            Decimal(str(efectivo.get(cid, 0))) + ajustes.get(cid, Decimal("0")),
            int(tickets.get(cid, 0)),
        )
        for cid in client_session_ids
    }


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
    if not sesion.esta_abierto:
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


async def abandonar(
    session: AsyncSession,
    sesion: PosCashSession,
    *,
    user_id: uuid.UUID | None,
    reason: str,
    ahora: datetime | None = None,
) -> PosCashSession:
    """Cierra el turno SIN arqueo. Esperado, contado y diferencia quedan en NULL.

    Existe para los turnos que nadie va a contar —los que la caja creaba en cada
    recarga de la pantalla—. Cerrarlos con un conteo inventado firmaría un
    arqueo falso; esto dice la verdad: que no hubo arqueo, quién lo decidió y
    por qué. Las ventas que lleguen después van a tardías, como en un turno
    cerrado.
    """
    if not sesion.esta_abierto:
        raise SesionYaCerradaError(str(sesion.id))
    sesion.status = ESTADO_ABANDONADO
    sesion.closed_at = ahora or datetime.now(UTC)
    sesion.closed_by_user_id = user_id
    sesion.notes = reason
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
    terminal_id: uuid.UUID | None = None,
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
    if terminal_id is not None:
        stmt = stmt.where(PosCashSession.terminal_id == terminal_id)
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
    "abandonar",
    "abrir_o_adoptar",
    "resumen_de_turnos",
    "cerrar",
    "efectivo_del_turno",
    "listar",
    "listar_tardias",
    "obtener_por_client_id",
    "registrar_tardia",
]
