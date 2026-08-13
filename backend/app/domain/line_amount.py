"""F-H4 — el monto de una línea cuando el archivo trae precio unitario y cantidad.

Un archivo real de ventas suele traer *precio* y *cantidad* y no traer el total:
el total es una cuenta, no un dato que alguien tipeó. Hasta acá el importador
exigía el monto y descartaba la fila sin él, así que esas planillas había que
reescribirlas antes de subirlas — la queja que originó todo este programa.

**La derivación va en un solo sentido y no contradice F10.** El precio unitario
NUNCA sale de ``monto / cantidad``: en una fila histórica no se sabe si el monto
es unitario o total, y adivinarlo fue exactamente el incidente ASTERIA. Al revés
sí es seguro, pero sólo porque el usuario **mapeó explícitamente** las dos
columnas: nadie dedujo por el nombre del header que "P.U." era un precio. Por eso
el caller sólo pasa valores que vinieron de un mapeo declarado (ver
``REQUIRED_ALTERNATIVES`` en ``column_mapping_service``); con columnas
autodetectadas esta función no se llama.

**Cuando los dos datos están y no coinciden, gana el cálculo.** Es la regla que
fijó el negocio: el precio unitario es el dato que manda y el monto es su
consecuencia. El monto del archivo no se tira — queda en la fila
(``_vektor_amount_original``) y en los contadores del import, porque una
diferencia sistemática entre las dos columnas casi siempre significa que la
planilla tiene un descuento, un impuesto o una cantidad que mide otra cosa, y eso
el dueño del negocio lo tiene que ver.

Sin sesión, sin ORM y sin leer nada del archivo: recibe tres valores ya
parseados. El módulo existe aparte para que los dos caminos de inserción —el
plano y el multi-hoja— no puedan responder distinto sobre la misma fila, que es
la asimetría que ya se pagó dos veces en este mismo importador.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

#: Precisión monetaria. El redondeo es ``ROUND_HALF_UP`` y no el bancario que
#: trae Python por default: ``0.005`` tiene que dar ``0.01`` como en cualquier
#: planilla, no ``0.00`` según la paridad del dígito anterior.
CENTAVO = Decimal("0.01")

#: Hasta cuánto se considera que el monto del archivo y el calculado son el mismo
#: número. Un centavo, explícito: la diferencia por redondeo de una planilla no es
#: una discrepancia de negocio, y sin tolerancia toda fila con decimales quedaría
#: reportada.
TOLERANCIA = Decimal("0.01")

#: Claves internas en ``custom_fields`` de la fila importada. El prefijo
#: ``_vektor_`` está RESERVADO: ``custom_fields`` guarda también los campos
#: propios que el usuario mapeó, cuyas claves salen de los headers del archivo
#: (``custom_field_slug()``, que normaliza a ``[a-z0-9_]`` y nunca arranca con
#: guión bajo). Sin el namespace, una planilla con una columna "amount source"
#: pisaría la evidencia de por qué el monto quedó como quedó.
#: Esa garantía la sostiene `app/tests/domain/test_custom_field_slug.py` — cuando
#: se escribió este comentario la función todavía no existía, así que el
#: namespace descansaba sobre una promesa que nada verificaba.
AMOUNT_ORIGINAL_FIELD = "_vektor_amount_original"
AMOUNT_SOURCE_FIELD = "_vektor_amount_source"

#: De dónde salió el monto que se va a guardar.
#: - ``file``: lo trajo el archivo (con o sin precio/cantidad al lado).
#: - ``calculated``: el archivo no traía monto; es ``precio × cantidad``.
#: - ``recalculated``: el archivo traía monto, difería, y se usó el cálculo.
AmountSource = Literal["file", "calculated", "recalculated"]


@dataclass(frozen=True)
class LineAmount:
    """Qué monto se guarda, de dónde salió y qué decía el archivo."""

    #: Monto a persistir. ``None`` = la fila no tiene monto ni forma de calcularlo.
    amount: Decimal | None
    #: ``None`` sólo cuando ``amount`` es ``None``.
    source: AmountSource | None
    #: Monto del archivo **cuando el cálculo lo reemplazó**. ``None`` en todos los
    #: demás casos, incluido el de coincidencia dentro de la tolerancia: ahí no hay
    #: nada que preservar porque el que se guarda es el del archivo.
    original: Decimal | None = None

    @property
    def discrepa(self) -> bool:
        """¿El archivo decía un monto distinto del que se guarda?"""
        return self.original is not None


def _positivo(valor: Decimal | None) -> Decimal | None:
    """``None`` para lo que no es un monto utilizable.

    Cero y negativo entran acá a propósito. Un precio de 0 no habilita a calcular
    un monto de 0: significa que la celda está vacía, en blanco o con un guión, y
    generar una venta de $0 desde eso es inventar una transacción. La misma regla
    ya rige en ``_parse_amount`` del importador, que descarta lo no positivo.
    """
    if valor is None or valor <= 0:
        return None
    return valor


def resolve_line_amount(
    *,
    amount: Decimal | None,
    unit_price: Decimal | None,
    quantity: int | None,
    tolerancia: Decimal = TOLERANCIA,
) -> LineAmount:
    """Resuelve el monto de una línea a partir de lo que el archivo declaró.

    ``quantity`` tiene que ser la cantidad **cruda** de la celda, no la que usan
    el gate de replay y la inserción (que tienen piso en 1 para no saltearse
    filas). Con el piso, una hoja donde la columna de cantidad está vacía
    calcularía ``precio × 1`` y le inventaría un monto a cada fila.
    """
    amount = _positivo(amount)
    unit_price = _positivo(unit_price)
    qty = quantity if quantity is not None and quantity > 0 else None

    if unit_price is None or qty is None:
        # Sin la pareja completa no se calcula nada. En particular: con monto y
        # cantidad NO se deriva el precio unitario (F10), y sólo con precio no se
        # inventa una cantidad.
        return LineAmount(amount, "file" if amount is not None else None)

    calculado = (unit_price * qty).quantize(CENTAVO, rounding=ROUND_HALF_UP)
    if amount is None:
        return LineAmount(calculado, "calculated")
    if abs(amount - calculado) <= tolerancia:
        # Coinciden: se guarda el del archivo tal como vino. Pisarlo con el
        # cálculo cambiaría montos ya importados por una diferencia que el propio
        # umbral declara irrelevante.
        return LineAmount(amount, "file")
    return LineAmount(calculado, "recalculated", original=amount)
