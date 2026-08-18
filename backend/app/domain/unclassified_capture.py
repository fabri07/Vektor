"""F-O.4 — reglas puras de qué fila NO debe capturarse en la bandeja «Otros».

Sin esto, dos hallazgos reales quedaban sin corregir: filas 100% vacías (celdas
en blanco al final de una hoja) llenaban la bandeja de ruido, y una fila de
agregado (``Subtotal``/``Total``) se capturaba como si fuera una operación.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.domain.text_norm import normalize_text

_AGGREGATE_WORDS = frozenset({"subtotal", "subtotales", "total", "totales"})


def _is_aggregate_value(value: Any) -> bool:
    if value is None:
        return False
    return normalize_text(str(value)).strip() in _AGGREGATE_WORDS


def is_aggregate_row(row: Mapping[str, Any], *, anchor_column: str | None = None) -> bool:
    """¿Esta fila es un renglón de agregado (Subtotal/Total), no un movimiento?

    Con ``anchor_column`` conocida (la mayoría de los call sites, que ya
    resolvieron qué columna es la fecha/ancla de la hoja): coincidencia de
    PALABRA COMPLETA contra ESA columna sola — nunca una coincidencia parcial
    que confundiría una fecha real que contenga la subcadena.

    Sin ``anchor_column`` (la hoja completamente no clasificada, sin target de
    fecha confirmado — el caso que motivó F-O.4): más conservador. Sólo
    dispara si hay una celda que matchea exacto Y el resto de la fila tiene
    poco contenido (a lo sumo una celda más con valor) — nunca porque
    "alguna celda contiene la palabra Total" en una fila con datos reales.
    """
    if anchor_column is not None:
        return _is_aggregate_value(row.get(anchor_column))

    values = [v for k, v in row.items() if k != "__context__"]
    if not any(_is_aggregate_value(v) for v in values):
        return False
    non_empty = sum(
        1
        for v in values
        if v is not None and str(v).strip().lower() not in {"", "none", "nan"}
    )
    return non_empty <= 2
