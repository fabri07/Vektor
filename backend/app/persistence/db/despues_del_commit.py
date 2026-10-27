"""Acciones que corren sólo después del commit REAL de la transacción (V-1).

El ``after_commit`` de SQLAlchemy también se dispara al LIBERAR un savepoint
(``SessionTransaction.commit`` lo despacha para ``nested``), y un listener con
``once=True`` se consume en el primero: un ``guarded_savepoint`` entre la venta y
el commit del request alcanzaba para que el evento saliera con la transacción
abierta, y el worker leía la base sin el cambio. Además, con ``once=True`` un
rollback no lo borraba: quedaba colgado de la sesión hasta el próximo commit.

Acá cada acción se anota en ``session.info`` junto a la transacción en la que se
registró, y:

- corre en el ``after_commit`` de la transacción de NIVEL SUPERIOR (el de un
  savepoint se ignora, igual que ``costo_promedio_pendiente._marcar_al_confirmar``);
- se descarta si hace rollback la transacción donde se registró o una que la
  contiene (el rollback de un savepoint descarta sólo lo anotado adentro);
- se descarta si la transacción de nivel superior termina sin commit (``close``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

_CLAVE = "vektor.despues_del_commit"

_Pendiente = tuple[SessionTransaction, Callable[[], None]]


def al_confirmar(session: AsyncSession | Session, accion: Callable[[], None]) -> None:
    """Agenda ``accion`` para después del commit real; con rollback no corre nunca."""
    sync = session.sync_session if isinstance(session, AsyncSession) else session
    # Sin transacción todavía (nada ejecutado), se abre la que el autobegin abriría
    # con la primera sentencia: la acción queda atada a ella, y un rollback que
    # llegue antes de cualquier SQL también la descarta.
    tx = sync.get_nested_transaction() or sync.get_transaction() or sync.begin()
    pendientes: list[_Pendiente] = sync.info.setdefault(_CLAVE, [])
    pendientes.append((tx, accion))


def _contiene(transaccion: SessionTransaction, tx: SessionTransaction) -> bool:
    """¿``tx`` es ``transaccion`` o está anidada adentro de ella?"""
    actual: SessionTransaction | None = tx
    while actual is not None:
        if actual is transaccion:
            return True
        actual = actual.parent
    return False


@event.listens_for(Session, "after_commit")
def _correr(session: Session) -> None:
    if session.in_nested_transaction():
        # Liberar un savepoint no es el commit: la transacción sigue abierta.
        return
    pendientes: list[_Pendiente] = session.info.pop(_CLAVE, [])
    for _tx, accion in pendientes:
        # La transacción ya commiteó: una acción que falla no puede convertirse en
        # un 500 de una operación que se completó, ni frenar a las que siguen.
        try:
            accion()
        except Exception as exc:  # noqa: BLE001
            from app.observability.logger import get_logger  # noqa: PLC0415

            get_logger(__name__).warning(
                "despues_del_commit.accion_fallo",
                error=str(exc),
                error_type=type(exc).__name__,
            )


@event.listens_for(Session, "after_soft_rollback")
def _descartar(session: Session, previous_transaction: SessionTransaction) -> None:
    pendientes: list[_Pendiente] | None = session.info.get(_CLAVE)
    if not pendientes:
        return
    if previous_transaction.parent is None:
        session.info.pop(_CLAVE, None)
        return
    session.info[_CLAVE] = [
        (tx, accion) for tx, accion in pendientes if not _contiene(previous_transaction, tx)
    ]


@event.listens_for(Session, "after_transaction_end")
def _cerrar(session: Session, transaction: Any) -> None:
    # La de nivel superior terminó: si commiteó, ``_correr`` ya vació la lista; si
    # se cerró sin commit (``close()`` no despacha rollback), lo anotado no vale.
    if transaction.parent is None:
        session.info.pop(_CLAVE, None)
