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

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

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


# ── Convenio de fecha por COLUMNA (E4/F2) ─────────────────────────────────────
#
# El orden de `BUSINESS_DATE_FORMATS` resuelve cada celda por su cuenta con la
# convención AR, y eso alcanza mientras el archivo sea argentino. No alcanza
# cuando no lo es: en una columna exportada en formato US, `"04/13/2026"` sólo se
# puede leer como mm/dd —no hay mes 13— pero `"03/04/2026"`, en esa MISMA columna,
# se leía como 3 de abril. El archivo decía 4 de marzo. Ninguna regla local
# distingue esas dos celdas; la columna sí, y por la misma razón que en los
# números: una celda inequívoca de la columna revela el convenio de todas.

#: Campos canónicos cuyo valor es una fecha de negocio. Definición de DOMINIO,
#: igual que ``CAMPOS_MONETARIOS``: qué columna hay que leer como fecha no
#: depende de por qué camino entró el archivo.
CAMPOS_DE_FECHA = frozenset({"transaction_date", "expense_date", "acquired_at"})

MOTIVO_FECHA_AMBIGUA = "convenio_de_fecha_ambiguo"
MOTIVO_FECHA_ILEGIBLE = "fecha_ilegible"

#: Serial de Excel: días desde el 1899-12-30. La base NO es el 31/12/1899 por el
#: bug del año bisiesto de 1900 que Excel arrastra por compatibilidad con Lotus.
_EPOCA_EXCEL = datetime(1899, 12, 30)

#: Ventana de seriales que se aceptan como fecha: 1990-01-01 a 2099-12-31. Un
#: número en una columna de fechas casi siempre es un serial, pero "casi siempre"
#: no alcanza para convertir cualquier número: un precio mal mapeado daría una
#: fecha perfectamente plausible de 1904 y nadie lo notaría. Fuera de la ventana
#: se devuelve `None` y la fila va a revisión con el valor original a la vista.
_SERIAL_MINIMO = 32874  # 1990-01-01
_SERIAL_MAXIMO = 73050  # 2099-12-31

_SOLO_DIGITOS = re.compile(r"^\d+(?:\.\d+)?$")

_SEPARADOR_DE_FECHA = re.compile(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})$")


@dataclass(frozen=True)
class ConvenioDeFecha:
    """Si el primer número de una fecha corta es el día o el mes.

    ``origen`` distingue "la columna lo dijo" de "nadie lo dijo y se usó la
    convención argentina", que es la información que necesita una pantalla para
    explicar por qué interpretó una fecha como la interpretó.
    """

    orden: Literal["dmy", "mdy"]
    origen: Literal["inequivoco", "default"]


@dataclass(frozen=True)
class FechaInterpretada:
    """El original, lo interpretado, y por qué no se pudo cuando no se pudo."""

    original: Any
    valor: datetime | None
    motivo: str | None = None

    @property
    def ausente(self) -> bool:
        return self.valor is None and self.motivo is None


def parse_excel_serial(raw: Any) -> datetime | None:
    """Un número de días desde la época de Excel, si está dentro de la ventana.

    Aparece cuando el `.xlsx` guarda la celda sin formato de fecha, y siempre en
    los CSV exportados desde Excel. openpyxl ya devuelve `datetime` para las
    celdas formateadas como fecha, así que esto cubre justo el caso que quedaba
    ilegible.
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        numero = float(raw)
        if numero != numero or numero in (float("inf"), float("-inf")):
            return None
    elif isinstance(raw, str) and _SOLO_DIGITOS.match(raw.strip()):
        # El parser de archivos normaliza TODA celda a texto antes de llegar acá,
        # así que un serial siempre viaja como `"45123"`: rechazar los strings
        # dejaría esta función sin ningún caso real. Se acepta sólo la forma
        # puramente numérica, y sólo en columnas que ya se creen de fecha.
        numero = float(raw.strip())
    else:
        return None
    if not (_SERIAL_MINIMO <= numero <= _SERIAL_MAXIMO):
        return None
    return _EPOCA_EXCEL + timedelta(days=numero)


def _senal_de_orden(texto: str) -> str | None:
    """Qué dice esta fecha corta sobre el orden de su columna, si dice algo.

    Sólo lo dice cuando uno de los dos primeros números es imposible como mes.
    ``"03/04/2026"`` no dice nada: es válida en los dos órdenes.
    """
    match = _SEPARADOR_DE_FECHA.match(texto)
    if match is None:
        return None
    primero, segundo = int(match.group(1)), int(match.group(2))
    if primero > 12 and segundo <= 12:
        return "dmy"
    if segundo > 12 and primero <= 12:
        return "mdy"
    return None


def inferir_convenio_de_fecha(valores: Iterable[Any]) -> ConvenioDeFecha | None:
    """Decide el orden de una columna de fechas.

    Devuelve ``None`` sólo ante una **contradicción**: la columna trae una fecha
    que sólo se lee dd/mm y otra que sólo se lee mm/dd. Ahí no hay convenio que
    valga para las dos y las ambiguas de esa columna van a revisión.

    Sin ninguna señal devuelve el default argentino, que es lo que el importador
    hace desde siempre: es una convención declarada, no una adivinanza, y
    cambiarla rompería todos los imports que hoy andan bien.
    """
    senales: set[str] = set()
    for bruto in valores:
        if bruto is None or isinstance(bruto, datetime | date):
            continue
        senal = _senal_de_orden(str(bruto).strip())
        if senal is not None:
            senales.add(senal)
    if len(senales) > 1:
        return None
    if senales:
        orden = senales.pop()
        return ConvenioDeFecha(orden=orden, origen="inequivoco")  # type: ignore[arg-type]
    return ConvenioDeFecha(orden="dmy", origen="default")


def parsear_fecha_de_columna(
    raw: Any, convenio: ConvenioDeFecha | None = None, *, century_pivot: int | None = None
) -> FechaInterpretada:
    """Interpreta una fecha bajo el convenio de su columna.

    Con ``convenio=None`` —columna contradictoria— sólo se resuelve lo que no
    necesita convenio: nativos, seriales y fechas cuyo orden es inequívoco. Una
    fecha ambigua ahí queda con ``MOTIVO_FECHA_AMBIGUA``, con el original a la
    vista: elegir uno de los dos órdenes le erraría a la mitad de las filas.
    """
    if raw is None:
        return FechaInterpretada(original=raw, valor=None)
    if isinstance(raw, datetime | date):
        return FechaInterpretada(original=raw, valor=parse_business_datetime(raw))

    serial = parse_excel_serial(raw)
    if serial is not None:
        return FechaInterpretada(original=raw, valor=serial)

    texto = str(raw).strip()
    if not texto:
        return FechaInterpretada(original=raw, valor=None)

    propia = _senal_de_orden(texto)
    orden = propia or (convenio.orden if convenio else None)
    if orden is None:
        # La columna no decidió y la celda no se explica sola. Sólo pasa si la
        # columna es contradictoria: sin señales, `inferir_convenio_de_fecha`
        # devuelve el default.
        motivo = (
            MOTIVO_FECHA_AMBIGUA
            if _SEPARADOR_DE_FECHA.match(texto)
            else MOTIVO_FECHA_ILEGIBLE
        )
        valor = parse_business_datetime(texto, century_pivot=century_pivot)
        # Una fecha larga o ISO no depende del orden: si se pudo leer, se usa.
        if valor is not None and motivo == MOTIVO_FECHA_ILEGIBLE:
            return FechaInterpretada(original=raw, valor=valor)
        return FechaInterpretada(original=raw, valor=None, motivo=motivo)

    valor = _parsear_con_orden(texto, orden, century_pivot=century_pivot)
    if valor is None:
        return FechaInterpretada(original=raw, valor=None, motivo=MOTIVO_FECHA_ILEGIBLE)
    return FechaInterpretada(original=raw, valor=valor)


def _parsear_con_orden(
    texto: str, orden: str, *, century_pivot: int | None = None
) -> datetime | None:
    """Prueba los formatos poniendo primero los del orden que pidió la columna."""
    if orden == "dmy":
        return parse_business_datetime(texto, century_pivot=century_pivot)
    match = _SEPARADOR_DE_FECHA.match(texto)
    if match is None:
        return parse_business_datetime(texto, century_pivot=century_pivot)
    # Reescribir a dd/mm y reusar el parser único, en vez de mantener una segunda
    # lista de formatos que pueda divergir de `BUSINESS_DATE_FORMATS`.
    mes, dia, anio = match.group(1), match.group(2), match.group(3)
    return parse_business_datetime(f"{dia}/{mes}/{anio}", century_pivot=century_pivot)
