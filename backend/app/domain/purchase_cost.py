"""F-H6.c — qué costó realmente cada línea de una compra.

Una compra no termina en el precio de la factura. El flete, el descuento y los
impuestos son parte de lo que el negocio pagó por esa mercadería, y hasta acá
ninguno llegaba al costo del producto: el flete quedaba como un gasto de logística
suelto y el descuento no se mapeaba. El resultado es un margen que se calcula
contra un costo que nadie pagó.

**Véktor no reparte solo.** El default es no distribuir, que es lo que ya eligió el
remito manual, y cambiarlo en silencio alteraría el costo de todos los imports que
ya existen. Distribuir es una decisión del usuario por hoja, y sólo se le ofrece
cuando el archivo permite saber a qué líneas pertenece la cifra compartida (ver
``identidad_de_comprobante`` en ``purchase_shipping``).

**Los dos fletes son cosas distintas y se gobiernan por separado.** Una cifra
compartida por el comprobante entero hay que repartirla; una que el archivo ya
asignó a la línea, no — sólo hay que decidir si entra al costo o queda como gasto.
Fusionarlos obligaría a repartir algo que ya venía repartido.

**El monto de la fila puede ser bruto o neto, y eso no se adivina.** Una planilla
con «Subtotal | Descuento | IVA | Total» mapea el monto a una de las dos puntas, y
restarle el descuento a un total que ya lo tiene descontado lo cuenta dos veces.
Por eso la base la declara el usuario (``BASE_INCLUYE`` / ``BASE_APLICAR``) y el
default nunca cambia números por su cuenta.

Sin sesión, sin ORM y sin leer el archivo: recibe valores ya parseados. Vive
aparte para que el camino plano y el multi-hoja no puedan responder distinto sobre
la misma fila — la asimetría que este importador ya pagó dos veces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

#: Precisión monetaria y modo de redondeo, los mismos que ``line_amount``:
#: ``0.005`` da ``0.01`` como en cualquier planilla, no ``0.00`` como haría el
#: redondeo bancario que Python trae por default.
CENTAVO = Decimal("0.01")

#: Qué hacer con el envío que pertenece al comprobante ENTERO.
#:
#: - ``no_distribuir`` (default): queda como gasto de logística aparte y no toca
#:   el costo de ningún producto. Es lo que ya decidió el remito manual.
#: - ``por_subtotal``: se reparte entre las líneas del grupo en proporción a lo
#:   que cada una pesa en la compra.
COMPARTIDO_NO = "no_distribuir"
COMPARTIDO_SUBTOTAL = "por_subtotal"
SHARED_MODES: frozenset[str] = frozenset({COMPARTIDO_NO, COMPARTIDO_SUBTOTAL})

#: Qué hacer con el envío que el archivo YA asignó a cada línea. No se reparte
#: nada: el reparto lo hizo quien armó la planilla. Lo único que falta decidir es
#: si ese costo se capitaliza en la mercadería o queda como gasto.
LINEA_GASTO = "gasto_aparte"
LINEA_AL_COSTO = "al_costo"
LINE_MODES: frozenset[str] = frozenset({LINEA_GASTO, LINEA_AL_COSTO})

#: Cómo leer el monto de la fila frente a las columnas de descuento e impuestos.
#:
#: - ``monto_incluye`` (default): el monto ya los trae adentro. No se toca nada.
#: - ``monto_sin_ajustes``: el monto es el bruto y hay que aplicárselos.
BASE_INCLUYE = "monto_incluye"
BASE_APLICAR = "monto_sin_ajustes"
COST_BASES: frozenset[str] = frozenset({BASE_INCLUYE, BASE_APLICAR})

#: Claves de traza en ``custom_fields`` de la fila importada. Prefijo ``_vektor_``
#: RESERVADO por la misma razón que en ``line_amount``: ``custom_fields`` comparte
#: namespace con los campos propios que mapeó el usuario, cuyas claves salen de los
#: headers y nunca arrancan con guión bajo.
COSTO_BASE_FIELD = "_vektor_costo_base"

#: Valores de ``COSTO_BASE_FIELD`` en el PRODUCTO. Sin esto, comparar dos costos
#: es comparar cosas distintas sin saberlo: un costo de 110 con el flete adentro y
#: uno de 100 facturado describen la misma compra, y el segundo no es más barato.
#: La ausencia de la clave significa «no sé», que NO es lo mismo que ``SIN_FLETE``.
CON_FLETE = "con_flete"
SIN_FLETE = "sin_flete"
DESCUENTO_FIELD = "_vektor_descuento"
IMPUESTOS_FIELD = "_vektor_impuestos"
ENVIO_ASIGNADO_FIELD = "_vektor_envio_asignado"
COSTO_UNITARIO_FINAL_FIELD = "_vektor_costo_unitario_final"

#: Marca en ``custom_fields`` del GASTO de flete cuyo importe ya entró al costo del
#: stock. No lleva el prefijo ``_vektor_`` porque no es traza de una fila importada
#: sino un hecho que los agregados tienen que leer: la plata salió de la caja (y el
#: gasto se registra igual, si no la reversa del archivo no tendría qué revertir),
#: pero descontarla del RESULTADO además de tenerla capitalizada en el inventario
#: la contaría dos veces. Los agregados de caja NO lo filtran, a propósito.
ATRIBUIDO_A_INVENTARIO_FIELD = "attributed_to_inventory"

#: Por qué una cifra compartida no se pudo repartir aunque el usuario lo pidió.
MOTIVO_SIN_BASE = "sin_base_para_repartir"


@dataclass(frozen=True)
class CostLine:
    """Lo que una fila de compra declara sobre su propio costo.

    ``amount`` es el monto ya resuelto por ``resolve_line_amount`` (F-H4): puede
    venir del archivo o ser ``precio × cantidad``. Los demás valen ``0`` cuando su
    columna no está mapeada, que es lo mismo que decir que el archivo no los trae.
    """

    row_index: int
    amount: Decimal
    quantity: int = 0
    discount: Decimal = Decimal("0")
    taxes: Decimal = Decimal("0")
    #: Envío que el ARCHIVO asignó a esta línea. No se reparte: ya está repartido.
    shipping_line: Decimal = Decimal("0")


@dataclass(frozen=True)
class LineCost:
    """Qué terminó costando una línea, y de qué partes."""

    row_index: int
    #: Monto después de aplicar (o no) descuento e impuestos, según la base.
    base: Decimal
    #: Parte de la cifra COMPARTIDA que le tocó. ``0`` si no se distribuyó.
    shipping_allocated: Decimal
    #: Envío propio de la línea que SE CAPITALIZÓ (``0`` con ``gasto_aparte``).
    #: Se expone aparte de ``total`` porque el importador necesita poder afirmar
    #: si el costo incluye flete, y desde el total no se puede reconstruir: un
    #: `propio` de 0 y un modo `gasto_aparte` dan el mismo número.
    shipping_line_applied: Decimal
    #: ``base`` + el envío de línea (si se capitaliza) + lo asignado.
    total: Decimal
    #: ``total / quantity``. ``None`` sin cantidad: no se inventa un divisor.
    unit_cost_final: Decimal | None
    #: El descuento declarado se comía el monto entero. La base quedó en ``0`` en
    #: vez de negativa, y el import lo reporta: un costo negativo no existe, y
    #: silenciarlo dejaría al producto valuado en cualquier cosa.
    descuento_mayor_que_monto: bool = False


@dataclass
class DistributionPlan:
    """Qué le tocó a cada línea y qué no se pudo repartir."""

    lines: list[LineCost] = field(default_factory=list)
    #: Cuánto de la cifra compartida se repartió efectivamente.
    repartido: Decimal = Decimal("0")
    #: Cuánto quedó sin repartir. Con ``no_distribuir`` es la cifra entera y es lo
    #: esperado; con ``por_subtotal`` sólo pasa si no hubo base contra la cual
    #: prorratear, y entonces ``motivo_sin_repartir`` lo dice.
    sin_repartir: Decimal = Decimal("0")
    motivo_sin_repartir: str | None = None

    def by_row(self) -> dict[int, LineCost]:
        """Índice por fila, que es como lo consume el importador."""
        return {line.row_index: line for line in self.lines}


def _base_de(line: CostLine, basis: str) -> tuple[Decimal, bool]:
    """Monto de la línea sobre el que se calcula todo, y si el descuento lo comió."""
    if basis == BASE_INCLUYE:
        return line.amount, False
    neto = line.amount - line.discount + line.taxes
    if neto <= 0:
        # Ni peso 0 ni costo negativo inventado: se declara y el import lo cuenta.
        return Decimal("0"), True
    return neto, False


def _repartir(
    bases: list[tuple[int, Decimal]], shared: Decimal
) -> tuple[dict[int, Decimal], Decimal, str | None]:
    """Prorratea ``shared`` por peso y devuelve un reparto que SUMA ``shared``.

    El redondeo por línea no cierra solo: tres líneas iguales sobre $10 dan
    3,33 × 3 = 9,99. El centavo que falta (o sobra) va a la línea de mayor base, y
    ante empate a la de menor índice, para que dos corridas sobre el mismo archivo
    den exactamente el mismo reparto.
    """
    total_base = sum((b for _, b in bases), Decimal("0"))
    if total_base <= 0:
        # Sin base no hay proporción posible. Repartir en partes iguales sería
        # elegir un criterio que el usuario no pidió.
        return {}, shared, MOTIVO_SIN_BASE

    shares = {
        row: (shared * base / total_base).quantize(CENTAVO, rounding=ROUND_HALF_UP)
        for row, base in bases
    }
    resto = shared - sum(shares.values(), Decimal("0"))
    if resto:
        row_mayor = min(bases, key=lambda item: (-item[1], item[0]))[0]
        shares[row_mayor] += resto
    return shares, Decimal("0"), None


def build_line_costs(
    lines: list[CostLine],
    *,
    shared_shipping: Decimal = Decimal("0"),
    shared_mode: str = COMPARTIDO_NO,
    line_mode: str = LINEA_GASTO,
    basis: str = BASE_INCLUYE,
) -> DistributionPlan:
    """Calcula el costo final de cada línea de un grupo de compra.

    ``shared_shipping`` es la cifra que pertenece al comprobante entero, ya
    resuelta por ``plan_shipping_charges`` (una cifra repetida en diez filas llega
    acá **una** vez). ``lines`` son las líneas de ESE grupo: repartir sobre líneas
    de otro comprobante le cargaría a una compra el flete de otra.

    Los tres ejes son independientes y ninguno implica a otro: se puede capitalizar
    el envío de línea sin repartir el compartido, y aplicar descuentos sin tocar
    ningún envío.
    """
    if shared_mode not in SHARED_MODES:
        raise ValueError(f"shared_mode desconocido: {shared_mode!r}")
    if line_mode not in LINE_MODES:
        raise ValueError(f"line_mode desconocido: {line_mode!r}")
    if basis not in COST_BASES:
        raise ValueError(f"basis desconocida: {basis!r}")

    bases: list[tuple[int, Decimal]] = []
    comidas: dict[int, bool] = {}
    for line in lines:
        base, comida = _base_de(line, basis)
        bases.append((line.row_index, base))
        comidas[line.row_index] = comida

    reparte = shared_mode == COMPARTIDO_SUBTOTAL and shared_shipping > 0
    if reparte:
        shares, sin_repartir, motivo = _repartir(bases, shared_shipping)
    else:
        # `no_distribuir` no es un fallo: la cifra queda íntegra como gasto de
        # logística, que es exactamente lo que el usuario pidió. Sin motivo.
        shares, sin_repartir, motivo = {}, shared_shipping, None

    plan = DistributionPlan(sin_repartir=sin_repartir, motivo_sin_repartir=motivo)
    for (row_index, base), line in zip(bases, lines, strict=True):
        asignado = shares.get(row_index, Decimal("0"))
        propio = line.shipping_line if line_mode == LINEA_AL_COSTO else Decimal("0")
        total = (base + propio + asignado).quantize(CENTAVO, rounding=ROUND_HALF_UP)
        plan.lines.append(
            LineCost(
                row_index=row_index,
                base=base,
                shipping_allocated=asignado,
                shipping_line_applied=propio,
                total=total,
                unit_cost_final=(
                    (total / line.quantity).quantize(CENTAVO, rounding=ROUND_HALF_UP)
                    if line.quantity > 0
                    else None
                ),
                descuento_mayor_que_monto=comidas[row_index],
            )
        )
    plan.repartido = sum(shares.values(), Decimal("0"))
    return plan
