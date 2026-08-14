"""F-T — cuánto tarda cada etapa de una operación larga, sin sesión ni ORM.

Nace del confirm de ingestión, que hasta acá publicaba UN número
(``latency_ms``) que además no medía el confirm: cronometraba sólo
``insert_confirmed_data``, dejando afuera las validaciones pre-lease, el snapshot
de maestros, el aprendizaje de mapeos y el cierre del lease. Con ese número, un
usuario que dice "tarda mucho" y una traza que dice "800 ms" pueden ser las dos
verdades a la vez, y no hay por dónde seguir.

**Tres decisiones que hacen que el medidor no mienta:**

1. **Una etapa repetida SUMA.** La inserción se llama una vez por hoja; quedarse
   con la última mediría una hoja de nueve, justo en el archivo grande que es el
   único donde el número importa. Por eso también viaja ``calls``: sin él,
   "inserción: 4.000 ms" esconde si fue una hoja lenta o nueve normales.
2. **Las filas se declaran DESPUÉS del bloque** (``etapa.rows = n``). El
   importador devuelve sus conteos al terminar; exigirlos de antemano obligaría a
   adivinarlos. Y una etapa que no cuenta filas **no reporta ``rows``**: un cero
   ahí se leería como "no procesó nada", que es un dato de negocio y no un
   silencio.
3. **El total es tiempo de pared, no la suma de las etapas.** Con etapas anidadas
   la suma cuenta dos veces la interna, y entre etapas hay huecos que nadie mide.
   Sumar daría un total que no coincide con lo que el usuario esperó.

El tiempo de un bloque que **falla** se registra igual y la excepción se propaga:
un confirm que explota es justamente donde más se necesita saber dónde tardó.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any


@dataclass
class StageHandle:
    """Lo que ve el bloque medido. Sólo sirve para declarar cuántas filas movió."""

    rows: int | None = None


@dataclass
class _Accumulator:
    ms: int = 0
    calls: int = 0
    rows: int | None = None


@dataclass
class StageTimings:
    """Acumulador de tiempos por etapa. No es thread-safe y no lo necesita: mide
    un request, en el mismo hilo que lo atiende."""

    _stages: dict[str, _Accumulator] = field(default_factory=dict)
    _t0: float = field(default_factory=time.monotonic)
    #: Fin de la última etapa registrada. `mark` mide desde acá.
    _cursor: float = field(default_factory=time.monotonic)

    def _record(self, name: str, elapsed_ms: int, rows: int | None) -> None:
        acc = self._stages.setdefault(name, _Accumulator())
        acc.ms += elapsed_ms
        acc.calls += 1
        if rows is not None:
            acc.rows = (acc.rows or 0) + rows
        self._cursor = time.monotonic()

    @contextmanager
    def stage(self, name: str) -> Iterator[StageHandle]:
        """Mide el bloque y lo suma a ``name``.

        El registro va en ``finally`` a propósito: si el bloque lanza, el tiempo
        queda anotado y la excepción sigue su camino intacta.
        """
        handle = StageHandle()
        started = time.monotonic()
        try:
            yield handle
        finally:
            self._record(name, int((time.monotonic() - started) * 1000), handle.rows)

    def mark(self, name: str, *, rows: int | None = None) -> None:
        """Cierra una etapa que va **desde el checkpoint anterior** hasta acá.

        Existe por una razón práctica: el confirm tiene ~800 líneas entre que
        carga el archivo y toma el lease, y envolverlas en un ``with`` obligaría a
        re-indentarlas enteras. Un diff así tapa el cambio que se quiere revisar —
        el mismo motivo por el que este repo no corre ``ruff format``.

        ``stage`` y ``mark`` se pueden mezclar en el mismo nivel: las dos mueven el
        cursor, así que un ``mark`` posterior nunca vuelve a cobrar lo que un
        ``stage`` ya midió.
        """
        self._record(name, int((time.monotonic() - self._cursor) * 1000), rows)

    def as_detail(self) -> dict[str, Any]:
        """Forma serializable para el JSONB de ``pipeline_events``.

        ``total_ms`` se calcula al llamar, no al cerrar la última etapa: quien
        emite el evento quiere el tiempo hasta ESE momento, incluidos los huecos
        entre etapas.
        """
        stages: dict[str, dict[str, int]] = {}
        for name, acc in self._stages.items():
            entry: dict[str, int] = {"ms": acc.ms, "calls": acc.calls}
            if acc.rows is not None:
                entry["rows"] = acc.rows
            stages[name] = entry
        return {
            "stages": stages,
            "total_ms": int((time.monotonic() - self._t0) * 1000),
        }
