"""Parser único de valores numéricos de negocio — convenio por COLUMNA (F2/E4).

Existía un parser por servicio y discrepaban sobre la MISMA celda. Verificado
antes de escribir esto:

* ``_parse_amount`` (importador) no tenía rama para "sólo punto", así que
  ``"12.500"`` daba **12,5** y ``"1.234.567"`` daba ``None``.
* ``normalize_numeric`` (file_parsing, remitos) y ``parse_money`` (agentes)
  asumen formato AR cuando hay coma y punto, sin mirar cuál viene último:
  ``"12,500.00"`` da **12,5**.
* ``validation_gate`` borraba TODOS los puntos antes de parsear: ``12.5`` daba
  **125**, incluso sobre celdas numéricas nativas de Excel. Como es el gate que
  vigila los montos, medía sobre un valor distinto del que se persistía y por eso
  no podía detectar la discrepancia que existe para detectar.

Ninguno servía de referencia: los tres fallan distinto. Lo que sigue es la
política, decidida contra un corpus, no copiada de un parser.

Reglas fijas
------------

1. **Un valor numérico nativo no se reinterpreta.** openpyxl y JSON devuelven
   ``float``/``int`` para las celdas numéricas: ahí no hay convenio que adivinar
   y tocar su formato visual sólo puede romperlo. Se rechazan ``NaN``/``inf``.

2. **El convenio se decide por COLUMNA, no por celda.** ``"12.500"`` aislado es
   ambiguo —$12.500 o $12,50— y ninguna regla local puede resolverlo. Pero una
   columna suele traer la respuesta: si en algún lado aparece ``"12.500,50"``, el
   punto de esa columna es separador de miles y ``"12.500"`` son doce mil
   quinientos. Ver ``inferir_convenio``.

3. **Nunca inventa.** Un valor que el convenio de su columna no alcanza a
   resolver devuelve ``None`` con un motivo, para que el caller lo derive a
   revisión con el original a la vista. No se elige "la interpretación más
   probable" en silencio: es plata.

4. **``Decimal``, nunca ``float``.** Un monto que pasa por ``float`` pierde
   exactitud antes de llegar a la base.

Agregar una regla acá y sumarle un caso a ``app/tests/domain/test_numeric_parsing.py``:
es el contrato que comparten importador, parser de archivos y gate de calidad.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

__all__ = [
    "MOTIVO_AMBIGUO",
    "MOTIVO_ILEGIBLE",
    "MOTIVO_NO_FINITO",
    "ConvenioNumerico",
    "ValorNumerico",
    "inferir_convenio",
    "parsear_cantidad",
    "parsear_monto",
]

#: Textos que significan "no hay dato", no "dato ilegible". No generan motivo:
#: una celda vacía no es un problema a revisar.
_VACIOS = frozenset({"-", "--", "n/a", "na", "none", "null", "nan", "s/d", "sin datos"})

#: Símbolos y separadores de miles que no aportan información numérica.
_RUIDO = re.compile(r"[$\s  ]")

#: Forma de un número: signo opcional, dígitos y separadores. Un texto que no la
#: cumple no es "ambiguo" —no es que no sepamos leerlo— sino directamente ilegible.
_FORMA_NUMERICA = re.compile(r"^[+-]?[\d.,]+$")

MOTIVO_AMBIGUO = "convenio_ambiguo"
MOTIVO_ILEGIBLE = "ilegible"
MOTIVO_NO_FINITO = "no_finito"
MOTIVO_FRACCIONARIA = "cantidad_fraccionaria"
MOTIVO_NEGATIVA = "cantidad_negativa"


@dataclass(frozen=True)
class ConvenioNumerico:
    """Qué significa cada separador en una columna.

    ``origen`` dice de dónde salió, y eso importa para explicarlo: no es lo mismo
    "la columna traía ``1.234,56``, así que el punto es de miles" que "ningún
    valor era inequívoco y se desempató por la forma".
    """

    decimal: Literal[",", "."]
    origen: Literal["inequivoco", "forma"]

    @property
    def miles(self) -> str:
        return "." if self.decimal == "," else ","


@dataclass(frozen=True)
class ValorNumerico:
    """El original, lo interpretado, y por qué no se pudo cuando no se pudo.

    Los tres juntos a propósito: el contrato del programa pide que preview y
    "Otros" muestren el valor original al lado de su interpretación, y eso es
    imposible si el parser devuelve sólo un número o ``None``.
    """

    original: Any
    valor: Decimal | None
    motivo: str | None = None

    @property
    def ok(self) -> bool:
        return self.valor is not None

    @property
    def ausente(self) -> bool:
        """Vacío ≠ ilegible: no hay nada que revisar en una celda en blanco."""
        return self.valor is None and self.motivo is None


def _limpiar(bruto: Any) -> str:
    return _RUIDO.sub("", str(bruto).strip())


def _separadores(texto: str) -> tuple[bool, bool]:
    return ("," in texto, "." in texto)


def _es_numero_nativo(valor: Any) -> bool:
    # `bool` es subclase de `int`: un True no es la cantidad 1.
    return isinstance(valor, int | float | Decimal) and not isinstance(valor, bool)


def _senal_inequivoca(texto: str) -> str | None:
    """El separador decimal, cuando el valor solo lo dice sin ayuda.

    Sólo lo dice si trae los DOS separadores: ahí el último es el decimal, porque
    los miles nunca van después de la coma decimal. Con uno solo no alcanza —
    ``"1.234"`` son mil doscientos treinta y cuatro o uno coma doscientos treinta
    y cuatro, y el valor por sí mismo no distingue.
    """
    tiene_coma, tiene_punto = _separadores(texto)
    if tiene_coma and tiene_punto:
        return "," if texto.rfind(",") > texto.rfind(".") else "."
    return None


def _senal_por_forma(texto: str) -> str | None:
    """El desempate cuando hay un solo separador: cuántos dígitos quedan detrás.

    Un grupo final de exactamente 3 dígitos es un grupo de miles (``1.500``); uno
    de 1 ó 2 es la parte decimal (``12.50``). La moneda argentina usa 2 decimales,
    no 3, así que la forma discrimina bien en la práctica. Devuelve ``None``
    cuando ni eso alcanza (4+ dígitos detrás, o varios separadores iguales).
    """
    tiene_coma, tiene_punto = _separadores(texto)
    if tiene_coma == tiene_punto:  # ninguno, o los dos (eso lo resuelve la señal fuerte)
        return None
    sep = "," if tiene_coma else "."
    partes = texto.split(sep)
    if len(partes) > 2:
        # `1.234.567`: varios separadores iguales sólo pueden ser miles.
        return "," if sep == "." else "."
    cola = partes[-1]
    if not cola.isdigit():
        return None
    if len(cola) == 3:
        return "," if sep == "." else "."  # el separador es de miles
    if len(cola) in (1, 2):
        return sep  # el separador es el decimal
    return None


def inferir_convenio(valores: Iterable[Any]) -> ConvenioNumerico | None:
    """Decide el convenio de una columna, o ``None`` si la columna no lo dice.

    Prioridad: primero los valores que lo dicen solos (los que traen ambos
    separadores), y sólo si ninguno lo hace se desempata por la forma del resto.

    Devuelve ``None`` —y entonces todo valor ambiguo de esa columna va a revisión—
    en dos casos, los dos reales:

    * **contradicción**: dos valores inequívocos que piden convenios distintos, o
      ``12.500`` junto a ``12.50`` en la misma columna. Una columna así está mal
      armada y elegir uno de los dos rompería la mitad de las filas;
    * **silencio**: ningún valor trae separadores, o todos son ilegibles. Acá no
      hace falta convenio: sin separadores no hay nada que interpretar.
    """
    inequivocas: set[str] = set()
    por_forma: set[str] = set()
    for bruto in valores:
        if _es_numero_nativo(bruto):
            continue  # un nativo no aporta convenio: no tiene formato
        texto = _limpiar(bruto)
        if not texto or texto.lower() in _VACIOS:
            continue
        fuerte = _senal_inequivoca(texto)
        if fuerte is not None:
            inequivocas.add(fuerte)
            continue
        forma = _senal_por_forma(texto)
        if forma is not None:
            por_forma.add(forma)

    if len(inequivocas) == 1:
        return ConvenioNumerico(decimal=inequivocas.pop(), origen="inequivoco")  # type: ignore[arg-type]
    if inequivocas:
        return None  # contradicción entre valores que se decían inequívocos
    if len(por_forma) == 1:
        return ConvenioNumerico(decimal=por_forma.pop(), origen="forma")  # type: ignore[arg-type]
    return None


def parsear_monto(bruto: Any, convenio: ConvenioNumerico | None = None) -> ValorNumerico:
    """Interpreta un monto bajo el convenio de su columna.

    Sin convenio sólo se resuelven los valores que no lo necesitan: nativos, sin
    separadores, o con los dos (que se dicen solos). Todo lo demás queda en
    ``MOTIVO_AMBIGUO`` — no se adivina.
    """
    if bruto is None:
        return ValorNumerico(original=bruto, valor=None)

    if _es_numero_nativo(bruto):
        if isinstance(bruto, float) and not math.isfinite(bruto):
            return ValorNumerico(original=bruto, valor=None, motivo=MOTIVO_NO_FINITO)
        # `str()` del float y no `Decimal(float)`: el segundo arrastra el error
        # binario (`Decimal(0.1)` son 55 dígitos), el primero respeta el repr.
        return ValorNumerico(original=bruto, valor=Decimal(str(bruto)))

    # El vacío se decide sobre el ORIGINAL, no sobre el limpiado: `"$$"` limpia
    # a cadena vacía, pero había algo escrito ahí y no era un número. Tratarlo
    # como ausente lo haría desaparecer sin dejar nada que revisar.
    crudo = str(bruto).strip()
    if not crudo or crudo.lower() in _VACIOS:
        return ValorNumerico(original=bruto, valor=None)
    texto = _limpiar(bruto)
    if not texto or not _FORMA_NUMERICA.match(texto):
        # No es que no sepamos con qué convenio leerlo: no es un número.
        return ValorNumerico(original=bruto, valor=None, motivo=MOTIVO_ILEGIBLE)
        return ValorNumerico(original=bruto, valor=None, motivo=MOTIVO_ILEGIBLE)

    decimal = _senal_inequivoca(texto) or (convenio.decimal if convenio else None)
    tiene_coma, tiene_punto = _separadores(texto)
    if decimal is None:
        if tiene_coma or tiene_punto:
            return ValorNumerico(original=bruto, valor=None, motivo=MOTIVO_AMBIGUO)
        decimal = "."  # sin separadores: da igual cuál sea

    miles = "." if decimal == "," else ","
    normalizado = texto.replace(miles, "").replace(decimal, ".")
    try:
        return ValorNumerico(original=bruto, valor=Decimal(normalizado))
    except InvalidOperation:
        return ValorNumerico(original=bruto, valor=None, motivo=MOTIVO_ILEGIBLE)


def parsear_cantidad(bruto: Any, convenio: ConvenioNumerico | None = None) -> ValorNumerico:
    """Cantidad de unidades: entera, no negativa, y **nunca truncada**.

    ``int(float("1.500"))`` daba 1 — una compra de mil quinientas unidades entraba
    como una. Y una coma, un negativo o un texto daban 0, que es un número válido
    y por lo tanto indistinguible de "el archivo dijo cero". Acá cada caso tiene
    su motivo y la fila va a revisión con el original a la vista.

    El cero sí es un dato legítimo y se devuelve como tal.
    """
    monto = parsear_monto(bruto, convenio)
    if monto.valor is None:
        return monto
    if monto.valor < 0:
        return ValorNumerico(original=bruto, valor=None, motivo=MOTIVO_NEGATIVA)
    if monto.valor != monto.valor.to_integral_value():
        return ValorNumerico(original=bruto, valor=None, motivo=MOTIVO_FRACCIONARIA)
    return ValorNumerico(original=bruto, valor=monto.valor)
