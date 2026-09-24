"""Cálculo puro de una operación de caja: líneas, descuentos y reparto de centavos.

Sin sesión, sin ORM y sin reloj: se le entra con números y se sale con números,
para que el caso que motivó el módulo se pueda probar sin levantar nada.

**Por qué el descuento no vive en el precio unitario.** ``unit_price`` es
``Numeric(12, 2)`` y el validador del schema exige ``decimal_places=2``. Un
descuento de $1 sobre tres unidades de $100 pide un unitario de $99,6666…, que
no existe con dos decimales: redondeando a 99,67 se cobran $100,01 de menos y a
99,66 se cobra $1,02 de descuento. Ninguna de las dos cierra. Por eso la unidad
sobre la que se descuenta es el TOTAL DE LÍNEA, y el reparto se hace en centavos
enteros: así la suma de las líneas es exactamente el total cobrado, siempre.

**Por qué el precio de lista no lo elige el cliente.** ``unit_price_list`` lo
carga el llamador desde el catálogo bloqueado, nunca desde el cuerpo de la
petición. Este módulo sólo garantiza que la aritmética cierra; que los precios
sean los del negocio es responsabilidad de quien arma las ``LineaPedida``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

CENTAVO = Decimal("0.01")


class OperacionInvalidaError(ValueError):
    """Base de los rechazos de cálculo. Cada subclase lleva su `CODE`."""

    CODE = "POS_OPERATION_INVALID"


class DescuentoExcedeElTotalError(OperacionInvalidaError):
    CODE = "DISCOUNT_EXCEEDS_TOTAL"


class OperacionVaciaError(OperacionInvalidaError):
    CODE = "EMPTY_OPERATION"


class TendersNoCuadranError(OperacionInvalidaError):
    CODE = "TENDERS_MISMATCH"


class VueltoNegativoError(OperacionInvalidaError):
    CODE = "CASH_RECEIVED_TOO_LOW"


def _a_centavos(monto: Decimal) -> int:
    """Decimal de dos decimales → centavos enteros.

    Todo el reparto ocurre en enteros: con floats o con Decimal redondeado por
    línea la suma de las partes deja de ser el total, que es exactamente el
    agujero que este módulo existe para cerrar.
    """
    return int((monto * 100).to_integral_value())


def _a_pesos(centavos: int) -> Decimal:
    return (Decimal(centavos) / 100).quantize(CENTAVO)


@dataclass(frozen=True)
class LineaPedida:
    """Una línea del carrito, ya resuelta contra el catálogo.

    ``quantity`` está en UNIDADES BASE (gramos, mililitros o unidades), igual que
    ``products.stock_units``: 0,750 kg de un producto en gramos con factor 1000
    son 750. La conversión a lo que ve un humano es de presentación y no entra
    acá — si entrara, la aritmética dejaría de ser exacta.
    """

    product_id: UUID
    quantity: int
    unit_price_list: Decimal
    discount_ars: Decimal = Decimal("0")


@dataclass(frozen=True)
class LineaCalculada:
    product_id: UUID
    quantity: int
    unit_price_list: Decimal
    #: Descuento explícito de la línea, tal como lo pidió el cajero.
    discount_line_ars: Decimal
    #: Parte del descuento global que le tocó a esta línea en el reparto.
    discount_global_share_ars: Decimal
    #: Lo que efectivamente se cobra por la línea.
    line_total: Decimal

    @property
    def discount_ars(self) -> Decimal:
        """Descuento total de la línea. Es el que entra en la aritmética."""
        return (self.discount_line_ars + self.discount_global_share_ars).quantize(CENTAVO)


@dataclass(frozen=True)
class OperacionCalculada:
    lineas: tuple[LineaCalculada, ...]
    #: Suma de las líneas ANTES del descuento global (ya con los de línea).
    subtotal: Decimal
    discount_global_ars: Decimal
    #: Lo que hay que cobrar. Siempre == suma de los `line_total`.
    total: Decimal


def calcular_operacion(
    lineas: Sequence[LineaPedida],
    descuento_global_ars: Decimal = Decimal("0"),
) -> OperacionCalculada:
    """Resuelve líneas y descuentos con cierre exacto al centavo.

    El descuento global se reparte proporcionalmente al total de cada línea, en
    centavos enteros truncados hacia abajo, y el residuo —siempre menor que la
    cantidad de líneas— se reparte de a un centavo por resto mayor. Los empates
    los resuelve el importe y después el orden del carrito, así que el
    resultado no depende del orden en que la base devolvió los productos.
    """
    if not lineas:
        raise OperacionVaciaError("La operación no tiene líneas.")

    if descuento_global_ars < 0:
        raise OperacionInvalidaError("El descuento global no puede ser negativo.")

    brutos: list[int] = []
    for linea in lineas:
        if linea.quantity <= 0:
            raise OperacionInvalidaError(f"La línea {linea.product_id} no tiene cantidad.")
        if linea.unit_price_list < 0 or linea.discount_ars < 0:
            raise OperacionInvalidaError(f"La línea {linea.product_id} tiene un importe negativo.")
        bruto = _a_centavos(linea.unit_price_list) * linea.quantity
        descuento = _a_centavos(linea.discount_ars)
        if descuento > bruto:
            raise DescuentoExcedeElTotalError(
                f"El descuento de la línea {linea.product_id} supera su importe."
            )
        brutos.append(bruto - descuento)

    subtotal_cent = sum(brutos)
    global_cent = _a_centavos(descuento_global_ars)

    if global_cent > subtotal_cent:
        # Cubre también el carrito que ya vale cero por descuentos de línea: ahí
        # CUALQUIER descuento global lo supera, y no hace falta un rechazo
        # aparte. El `if subtotal_cent` de abajo sigue siendo necesario porque
        # con subtotal y descuento en cero la división igual ocurriría.
        raise DescuentoExcedeElTotalError("El descuento global supera el importe de la venta.")

    # Reparto por RESTO MAYOR, un centavo a la vez. Cada línea recibe primero
    # su parte truncada; el residuo se reparte de a UN centavo a las líneas con
    # mayor fracción descartada (desempate: mayor importe, después la primera
    # del carrito). Darle el residuo entero a una sola línea podía pasarla de su
    # propio importe: $99,99 sobre $34+$33+$33 dejaba la de $34 en −$0,01. Con
    # resto mayor eso no puede pasar: sólo recibe un centavo extra una línea con
    # fracción > 0, o sea cuya parte exacta supera a la truncada, y la exacta
    # nunca supera el importe de la línea porque el descuento ≤ subtotal.
    repartido: list[int] = []
    fracciones: list[int] = []
    for bruto in brutos:
        if subtotal_cent:
            parte, resto = divmod(global_cent * bruto, subtotal_cent)
        else:
            parte, resto = 0, 0
        repartido.append(parte)
        fracciones.append(resto)
    residuo = global_cent - sum(repartido)
    orden = sorted(range(len(brutos)), key=lambda i: (-fracciones[i], -brutos[i], i))
    for i in orden[:residuo]:
        repartido[i] += 1

    calculadas = tuple(
        LineaCalculada(
            product_id=linea.product_id,
            quantity=linea.quantity,
            unit_price_list=linea.unit_price_list,
            discount_line_ars=linea.discount_ars.quantize(CENTAVO),
            discount_global_share_ars=_a_pesos(parte),
            line_total=_a_pesos(bruto - parte),
        )
        for linea, bruto, parte in zip(lineas, brutos, repartido, strict=True)
    )

    return OperacionCalculada(
        lineas=calculadas,
        subtotal=_a_pesos(subtotal_cent),
        discount_global_ars=_a_pesos(global_cent),
        total=_a_pesos(subtotal_cent - global_cent),
    )


def validar_tenders(total: Decimal, tenders: Sequence[Decimal]) -> None:
    """Σ tenders == total, al centavo.

    Es la igualdad que hace que el pago mixto no rompa los ocho lectores de
    caja (B9): si los pagos pudieran no sumar la venta, cada lector tendría que
    decidir por su cuenta a qué método imputar la diferencia.
    """
    if not tenders:
        raise TendersNoCuadranError("La operación no tiene pagos.")
    if any(t <= 0 for t in tenders):
        raise TendersNoCuadranError("Un pago no puede ser cero ni negativo.")
    suma = sum(_a_centavos(t) for t in tenders)
    if suma != _a_centavos(total):
        raise TendersNoCuadranError(
            f"Los pagos suman {_a_pesos(suma)} y la venta es {total.quantize(CENTAVO)}."
        )


def calcular_vuelto(cash_received: Decimal | None, cash_tender: Decimal) -> Decimal:
    """Vuelto = entregado − la PARTE EN EFECTIVO de la venta.

    Ni el entregado ni el vuelto son la venta: recibir $10.000 para pagar $8.500
    registra $8.500 de venta y $1.500 de vuelto. Confundirlos infla la caja.
    """
    if cash_received is None:
        return Decimal("0.00")
    entregado = _a_centavos(cash_received)
    efectivo = _a_centavos(cash_tender)
    if entregado < efectivo:
        raise VueltoNegativoError(
            f"Se entregaron {cash_received.quantize(CENTAVO)} para una parte en "
            f"efectivo de {cash_tender.quantize(CENTAVO)}."
        )
    return _a_pesos(entregado - efectivo)
