"""B·4: ¿esta fila es un TOTAL de la planilla y no una operación?

Las planillas traen al pie (o entre bloques) una fila «Total» / «Subtotal» que
suma las de arriba. Importarla como venta, gasto o producto duplica lo que ya
entró fila por fila. Pero el criterio tiene que ser conservador: un producto
llamado «Total Clean» o una venta cuyo detalle es «Total de la seña» SON datos,
y tomarlos por un total sería perderlos.

Por eso hacen falta las DOS cosas:

1. **La etiqueta es exactamente un total.** La celda de texto normalizada es
   ``total`` / ``subtotal`` / ``totales`` / ``total general``, con a lo sumo un
   sufijo de período («total mes», «total enero», «total 2026», «subtotal
   03/2026»). «Total Clean» no lo es.
2. **La fila tiene forma de agregado:** esa es la ÚNICA celda de texto (el
   resto son números, fechas o están vacías) y hay al menos un número. Una fila
   que además trae un SKU, un cliente o una descripción no es un total.

Lo que cumple las dos no se descarta: va a «Otros» con su motivo, para que el
usuario lo vea y, si se equivocó Véktor, lo clasifique a mano. Ante la duda,
la fila sigue su camino normal.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

MOTIVO_FILA_DE_TOTAL = "Fila de total de la planilla: no es una operación"

_MESES = (
    "enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|"
    "octubre|noviembre|diciembre|ene|feb|mar|abr|may|jun|jul|ago|sep|set|oct|nov|dic"
)
_SUFIJO_PERIODO = (
    rf"(?:del |de )?(?:mes|dia|semana|ano|mensual|semanal|diario|anual|{_MESES})"
    r"(?: (?:de )?\d{2,4})?"
    r"|\d{4}|\d{1,2} \d{2,4}"
)
_ETIQUETA_DE_TOTAL = re.compile(
    rf"^(?:sub ?total(?:es)?|totales|total)(?: general)?(?: (?:{_SUFIJO_PERIODO}))?$"
)
_FECHA_TEXTO = re.compile(
    r"^\d{1,4}[-/.]\d{1,2}(?:[-/.]\d{1,4})?(?:[ t]\d{1,2}:\d{2}(?::\d{2})?)?$"
)
_VACIOS = frozenset({"", "none", "nan", "null", "-"})


def normalizar_etiqueta(value: str) -> str:
    """lower, sin tildes, sin puntuación de borde, separadores a un espacio."""
    s = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    s = s.strip().lower()
    s = re.sub(r"[\s\-_/.:;]+", " ", s)
    return s.strip(" ()[]*#")


def es_etiqueta_de_total(value: str) -> bool:
    return bool(_ETIQUETA_DE_TOTAL.match(normalizar_etiqueta(value)))


def _es_numero(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int | float | Decimal):
        return True
    s = str(value).strip().replace("$", "").replace(" ", "").rstrip("%")
    if not s:
        return False
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    try:
        Decimal(s)
    except InvalidOperation:
        return False
    return True


def _es_fecha(value: Any) -> bool:
    if isinstance(value, date | datetime):
        return True
    return bool(_FECHA_TEXTO.match(str(value).strip().lower()))


def es_fila_de_total(valores: list[Any]) -> bool:
    """Las celdas de una fila (sin las claves del sistema) → ¿es un total?"""
    textos: list[str] = []
    numeros = 0
    for v in valores:
        if v is None:
            continue
        if str(v).strip().lower() in _VACIOS:
            continue
        if _es_numero(v):
            numeros += 1
        elif _es_fecha(v):
            continue
        else:
            textos.append(str(v))
    return numeros >= 1 and len(textos) == 1 and es_etiqueta_de_total(textos[0])


def es_fila_de_total_dict(row: dict[str, Any]) -> bool:
    """Igual que :func:`es_fila_de_total` para una fila del importador.

    Las claves ``__`` son del sistema (contexto, referencias) y no cuentan.
    """
    return es_fila_de_total([v for k, v in row.items() if not str(k).startswith("__")])
