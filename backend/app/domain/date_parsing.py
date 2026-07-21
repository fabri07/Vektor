"""Parser único de fechas de negocio — convención argentina (F6-C1).

Existía un parser por servicio, cada uno con su lista de formatos, y discrepaban
sobre el MISMO archivo: ``validation_gate`` aceptaba 4 formatos contra los 14 del
importador, así que marcaba como "fecha fallida" un ``2026-06-05T14:30:00`` que el
import levantaba perfecto. Este módulo es la fuente única.

Reglas fijas:

- **Convención AR**: ``%d/%m/%Y`` se prueba ANTES que ``%m/%d/%Y``, así que
  ``03/04/2026`` es el 3 de ABRIL. Solo se cae a mm/dd cuando dd/mm es imposible
  (mes > 12), nunca por preferencia.
- **Formatos con hora antes que los de solo fecha**: ``transaction_date`` es
  DATETIME y soporta intradía; si solo viene la fecha, queda a medianoche.
- **Nunca inventa**: un valor ilegible devuelve ``None``. El fallback a "hoy" es
  responsabilidad del caller, y después de F6 casi ningún caller tiene derecho a
  ejercerlo (ver invariante 2d y la no-invention rule).

Agregar un formato acá y sumarle un caso a ``app/tests/domain/test_date_parsing.py``:
es el contrato que comparten importador, gate de calidad y carga manual.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

# Orden = prioridad. Con hora primero (una fecha con hora también matchearía el
# formato de solo fecha si se truncara, y perderíamos la hora).
BUSINESS_DATE_FORMATS: tuple[str, ...] = (
    # con hora
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%d-%m-%Y %H:%M:%S",
    "%d-%m-%Y %H:%M",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
    # solo fecha — dd/mm ANTES que mm/dd (convención AR)
    "%d/%m/%Y",
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d/%m/%y",
    "%d-%m-%y",  # F6-C2: faltaba, y "05-06-26" quedaba sin parsear
    "%Y/%m/%d",
    "%m/%d/%Y",
)

# Pivote de siglo para años de 2 dígitos en fechas de PERSONAS (cumpleaños):
# alguien nacido en "50" es de 1950, no 2050. strptime usa 69 por defecto, que
# para cumpleaños da resultados absurdos. Las transacciones usan el default.
BIRTHDAY_CENTURY_PIVOT = 30


def parse_business_datetime(raw: Any, *, century_pivot: int | None = None) -> datetime | None:
    """Parsea una fecha de negocio a ``datetime``. ``None`` si no es legible.

    ``century_pivot``: para años de 2 dígitos, ``yy > pivot`` cae en 1900 y el
    resto en 2000. Omitirlo deja el comportamiento de ``strptime`` (pivote 69).
    """
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, date):
        return datetime.combine(raw, datetime.min.time())

    text = str(raw).strip()
    if not text:
        return None

    # ISO 8601 primero: "2026-06-05T14:30:00", "2026-06-05 14:30:00", "2026-06-05".
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass

    for fmt in BUSINESS_DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if century_pivot is not None and "%y" in fmt:
            parsed = _apply_century_pivot(parsed, century_pivot)
        return parsed
    return None


def parse_business_date(raw: Any, *, century_pivot: int | None = None) -> date | None:
    """Igual que :func:`parse_business_datetime` pero devuelve ``date``."""
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    parsed = parse_business_datetime(raw, century_pivot=century_pivot)
    return parsed.date() if parsed is not None else None


def _apply_century_pivot(parsed: datetime, pivot: int) -> datetime:
    two_digit = parsed.year % 100
    year = (1900 if two_digit > pivot else 2000) + two_digit
    return parsed.replace(year=year)
