"""Formato de celda para la exportación completa — Cambio 3 del plan de
conservación y acceso a datos de negocio (docs/plans/
conservacion-y-acceso-datos-negocio.md).

Qué problema resuelve
---------------------
Un CSV para negocio no es lo mismo que un CSV para mostrar en pantalla: la
tabla redondea precios para que se lean cómodos, pero un valor EXPORTADO tiene
que ser el dato exacto — un `Decimal("15000.005")` truncado a "$15.000" en la
exportación sería, en los hechos, pérdida de datos con otro nombre. Este
módulo es la única fuente de formato de celda para exportar: no reusa los
formateadores de UI (`formatCustomFieldValue` en el frontend) a propósito,
porque esos SÍ redondean/narran para lectura humana.

`formula_safe_cell` es la mitigación estándar (OWASP) de inyección de fórmula
CSV: un valor que EMPIEZA con `=`, `+`, `-`, `@`, tab o retorno de carro puede
ejecutarse como fórmula al abrir el archivo en Excel/Sheets — un nombre de
producto literal `"=cmd|...` no es hipotético, es el vector real. Prefijar con
un apóstrofe neutraliza la interpretación sin alterar el valor visible.
"""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from typing import Any

#: Primer carácter que un cliente de planillas puede interpretar como el
#: inicio de una fórmula. Tab (`\t`) y retorno de carro (`\r`) también valen
#: como vector, no solo los símbolos visibles.
_FORMULA_LEAD_CHARS = ("=", "+", "-", "@", "\t", "\r")


def formula_safe_cell(value: str) -> str:
    """Prefija con `'` si el valor podría interpretarse como fórmula.

    Un apóstrofe inicial es invisible para quien lee el CSV como texto/dato
    (la mayoría de los parsers no Excel lo ignoran o lo conservan tal cual,
    que sigue siendo correcto: el dato original nunca tuvo un apóstrofe), y en
    Excel/Sheets fuerza la celda a texto plano en vez de evaluarla.
    """
    if value and value[0] in _FORMULA_LEAD_CHARS:
        return f"'{value}"
    return value


def format_export_value(value: Any, data_type: str) -> str:
    """Representación EXACTA para exportar, no la de la UI.

    - `None` → cadena vacía (ausencia real, no un "—" pensado para lectura).
    - number: `Decimal`/`int` se emiten con `str()` — la representación EXACTA,
      nunca `float()` (que puede introducir error de punto flotante) ni
      redondeo de presentación.
    - date/datetime: ISO 8601 — la única forma que no depende de qué columna
      cree el lector que es "el mes" (ver domain/date_parsing.py, E4).
    - boolean: tokens literales `"true"`/`"false"`, no "Sí"/"No" — esto es
      para reprocesar, no para leer en pantalla.
    - text/enum/lo demás: `str()` tal cual, preservando ceros a la izquierda y
      cualquier forma que el dato ya tenía (nunca se "limpia").
    """
    if value is None:
        return ""
    if data_type == "number":
        if isinstance(value, Decimal | int):
            return str(value)
        if isinstance(value, float):
            return repr(value)  # no se espera en la práctica; nunca se trunca
        return str(value)
    if data_type == "date":
        if isinstance(value, _dt.datetime | _dt.date):
            return value.isoformat()
        return str(value)
    if data_type == "boolean":
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)
    return str(value)
