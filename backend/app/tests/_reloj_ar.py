"""Reloj de server en UTC para los tests de «fecha de negocio AR».

No hay freezegun en el repo. `reloj_utc` fija el instante como lo vería el
server de producción (Railway, en UTC): parchea el `datetime` de
`business_time` (lo usan `today_ar()` y `now_ar_naive()` de todos lados) y el
`date` de los módulos que se le pasan, cuyo `date.today()` devuelve el día UTC.
De 21 a 24 ART ese día ya es mañana: un `date.today()` que quede da otro día.

Mismo andamio que `test_fechas_chat_t7._reloj`, con la lista de módulos como
parámetro.
"""

from __future__ import annotations

import contextlib
import importlib
import unittest.mock
from collections.abc import Iterable, Iterator
from datetime import UTC, date, datetime
from typing import Any


@contextlib.contextmanager
def reloj_utc(instante: datetime, modulos: Iterable[str] = ()) -> Iterator[None]:
    """El server en UTC en ``instante``: `datetime.now(tz)` y `date.today()`."""

    class _Datetime(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Any:
            if tz is None:
                return instante.astimezone(UTC).replace(tzinfo=None)
            return instante.astimezone(tz)

    class _Date(date):
        @classmethod
        def today(cls) -> Any:
            return instante.astimezone(UTC).date()

    with contextlib.ExitStack() as pila:
        pila.enter_context(
            unittest.mock.patch("app.domain.business_time.datetime", _Datetime)
        )
        for nombre in modulos:
            if hasattr(importlib.import_module(nombre), "date"):
                pila.enter_context(unittest.mock.patch(f"{nombre}.date", _Date))
        yield
