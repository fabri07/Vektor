"""Espejo SQL compartido del predicado de flags de ``custom_fields``.

``models/_sentinel.is_flag_true`` es la fuente única del predicado en Python;
esta es su única forma SQL — un solo lugar para el truco cross-dialect, usado
por los repos de proveedores y clientes (antes cada repo tenía su copia y ya
habían divergido en el manejo del booleano JSON).
"""

from typing import Any

from sqlalchemy import String, cast, func
from sqlalchemy.sql import ColumnElement

# Valores que el espejo SQL reconoce como "flag activo". Debe coincidir con
# ``is_flag_true`` de models/_sentinel.py: el booleano JSON ``true`` rinde
# ``'true'`` en Postgres (``->>``) pero ``1`` (entero) en SQLite
# (``json_extract``, donde ``.as_string()`` no castea de verdad).
_TRUE_VALUES = ["true", "1"]


def flag_is_true_sql(column: Any, key: str) -> ColumnElement[bool]:
    """``custom_fields[key]`` es un flag activo ("true"/"1"/bool true), en SQL.

    El ``cast`` explícito a String normaliza el entero de SQLite; NO usar
    ``coalesce`` acá — key ausente → NULL → ``IN`` es NULL → falsy, que es lo
    correcto para "¿tiene el flag?".
    """
    return cast(column[key].as_string(), String()).in_(_TRUE_VALUES)


def flag_not_true_sql(column: Any, key: str) -> ColumnElement[bool]:
    """Negación segura de ``flag_is_true_sql`` para usar en WHERE.

    ``coalesce(..., '')`` es clave: en las filas comunes la key está ausente →
    la extracción es NULL, y ``NULL NOT IN (...)`` es NULL — sin el coalesce el
    filtro las descartaría a TODAS.
    """
    return func.coalesce(cast(column[key].as_string(), String()), "").not_in(_TRUE_VALUES)
