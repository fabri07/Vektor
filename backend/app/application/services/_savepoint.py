"""Contrato de ordenamiento para get-or-create y guards de unicidad sobre SAVEPOINT.

Por qué existe este módulo
--------------------------
``Session.begin_nested()`` **flushea incondicionalmente antes de crear el
SAVEPOINT**. No es un autoflush: ``SessionTransaction._take_snapshot()``
(SQLAlchemy 2.0.48, ``orm/session.py:1082-1086``) hace ::

    is_begin = self.origin in (BEGIN, AUTOBEGIN)
    if not is_begin and not self.session._flushing:
        self.session.flush()

y para ``begin_nested()`` el origin es ``BEGIN_NESTED`` → ``is_begin`` es
``False`` → flush, **incluso con ``autoflush=False``** (que es como corre el
import en producción). Además ``_take_snapshot()`` se ejecuta en
``SessionTransaction.__init__``, *antes* de que se emita el ``SAVEPOINT`` (que es
lazy, en ``_connection_for_bind``).

Consecuencia: este patrón, que parece correcto, está MAL ::

    session.add(obj)                        # ← o setattr sobre un persistente
    try:
        async with session.begin_nested():  # ← flushea el INSERT/UPDATE acá,
            await session.flush()           #    FUERA del savepoint
    except IntegrityError:
        ...                                 # en PostgreSQL la transacción ya está
                                            # abortada → el re-query revienta con
                                            # InFailedSQLTransaction

El DML se emite en la transacción EXTERNA, así que la violación de unicidad la
aborta entera y el ``except`` no puede recuperarse. En SQLite el fallo es benigno
y la contención es rara, por eso una suite verde no lo detecta.

El orden correcto — lo que provee este módulo ::

    await session.flush()                   # 1. drenar lo pendiente, FUERA del try
    try:
        async with session.begin_nested():
            session.add(obj)                # 2. mutar/agregar DENTRO del savepoint
            await session.flush()           # 3. el DML se emite dentro
    except IntegrityError:
        ...                                 # transacción usable; ``obj`` quedó
                                            # transient por _restore_snapshot

El paso 1 importa por dos razones: acota el radio de explosión del rollback
(``_restore_snapshot`` en ``session.py:1094`` expunga ``set(self._new) |
set(session._new)``, o sea TODO lo pendiente de la sesión, no solo lo nuestro), y
evita que el flush de ``_take_snapshot`` emita objetos ajenos dentro del ``try``,
donde un fallo suyo se atribuiría al constraint que estamos vigilando.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession


def unique_violation_classifier(
    label: str, *, constraint: str, columns: tuple[str, ...] = ()
) -> Callable[[IntegrityError], str | None]:
    """Clasificador para UN índice único concreto; todo lo demás → ``None``.

    Hace falta porque ``except IntegrityError`` a secas también traga una FK rota o
    un NOT NULL del candidato y los reporta como "ya existía" — persistiendo una
    conclusión falsa en silencio.

    Dos formas de mensaje, porque los motores difieren: PostgreSQL/asyncpg nombran
    el constraint (``uq_...``), pero **SQLite reporta las columnas**
    (``UNIQUE constraint failed: tabla.col_a, tabla.col_b``). ``columns`` son los
    nombres calificados a reconocer en esa segunda forma.
    """

    def _classify(exc: IntegrityError) -> str | None:
        orig = getattr(exc, "orig", None)
        name = getattr(orig, "constraint_name", None)
        if name:
            return label if str(name) == constraint else None
        text = str(orig or exc)
        if constraint in text:
            return label
        if "UNIQUE" not in text.upper():
            return None
        return label if columns and all(col in text for col in columns) else None

    return _classify


class SavepointConflictError(Exception):
    """Un constraint vigilado se violó dentro del savepoint.

    ``constraint`` es la etiqueta que devolvió el clasificador del caller (p. ej.
    ``"sku"``), no el nombre crudo del índice.
    """

    def __init__(self, constraint: str, original: IntegrityError) -> None:
        super().__init__(f"constraint violado en savepoint: {constraint}")
        self.constraint = constraint
        self.original = original


@asynccontextmanager
async def guarded_savepoint(
    session: AsyncSession,
    classify: Callable[[IntegrityError], str | None],
) -> AsyncIterator[None]:
    """Ejecuta el bloque dentro de un SAVEPOINT, con el ordenamiento correcto.

    El caller hace ``session.add(...)`` o muta objetos **dentro** del bloque —
    nunca antes, o el DML se emite fuera del savepoint (ver docstring del módulo).

    ``classify`` recibe el ``IntegrityError`` y devuelve la etiqueta del constraint
    vigilado, o ``None`` si la violación es de otra cosa (una FK, un NOT NULL). Con
    ``None`` la excepción se re-propaga intacta: convertir una violación de FK en
    "ya existía, lo reuso" es peor que fallar.

    Raises:
        SavepointConflictError: si ``classify`` reconoció el constraint.
        IntegrityError: si la violación no era la vigilada.
    """
    # 1. Drenar lo pendiente FUERA del try. Sin esto, el flush incondicional de
    #    _take_snapshot emite objetos ajenos dentro del bloque protegido.
    await session.flush()
    try:
        # 2. El SAVEPOINT se crea acá; todo lo que el caller haga adentro se emite
        #    en el paso 3, ya protegido.
        async with session.begin_nested():
            yield
            # 3. Flush explícito ANTES de salir del bloque: dispara el constraint
            #    ahora, no en el commit del caller. (El commit del savepoint también
            #    flushearía, pero entonces la excepción saldría del __aexit__ y sería
            #    más difícil de atribuir.)
            await session.flush()
    except IntegrityError as exc:
        constraint = classify(exc)
        if constraint is None:
            raise
        raise SavepointConflictError(constraint, exc) from exc
