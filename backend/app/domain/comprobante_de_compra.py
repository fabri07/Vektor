"""B·5b: una compra de mercadería es un comprobante con LÍNEAS de artículos.

Decisión del dueño (2026-10-20): una compra de mercadería que no dice qué
artículos la componen no es un dato útil. Toda ruta que registra una compra
(carga manual, «Otros», chat, remito) pasa por esta misma regla, así que los
mismos datos dan el mismo costo y el mismo contrato (M·1b) en cualquiera.

Puro: sin sesión ni ORM.

**Línea válida:** producto, cantidad entera ≥ 1 en unidad de venta, importe > 0.
Una devolución, una cantidad negativa o una línea en 0 no son una compra (una
bonificación se carga como menor precio de la línea pagada).

**El importe de la línea es lo que dice el comprobante.** El IVA dice qué es ese
importe:

- ``incluido``: el importe ya trae el IVA. El costo por unidad es importe /
  cantidad y se guarda el IVA contenido por unidad (``importe × a / (1 + a)``).
- ``adicional``: el importe es neto y el IVA se paga aparte. El costo por unidad
  es el neto; el IVA queda registrado (``importe × a`` por la línea) y el gasto
  es lo que salió: neto + IVA.
- ``no_aplica`` / ``desconocido``: no hay alícuota. Nunca un 21 % por defecto.

Que un Responsable Inscripto recupere ese IVA lo decide después la base
comparable por régimen (M·1d), no esta regla.

**El envío del comprobante:**

- ``repartir``: se reparte por subtotal entre las líneas con el MISMO motor que
  el import (``purchase_cost.build_line_costs``: el centavo de redondeo va a la
  línea de mayor subtotal) y entra al costo → ``incluido``;
- ``gasto_aparte``: un gasto de logística aparte, fuera del costo → ``adicional``;
- ``no_hubo`` → ``no_aplica``; sin dato → ``desconocido``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from app.domain.contrato_de_costo import ContratoDeCosto
from app.domain.costo_de_referencia import Tratamiento
from app.domain.purchase_cost import (
    BASE_INCLUYE,
    COMPARTIDO_NO,
    COMPARTIDO_SUBTOTAL,
    CostLine,
    build_line_costs,
)

TratamientoIva = Literal["incluido", "adicional", "no_aplica", "desconocido"]
ModoEnvio = Literal["repartir", "gasto_aparte", "no_hubo", "desconocido"]

TRATAMIENTOS_IVA: frozenset[str] = frozenset(
    {"incluido", "adicional", "no_aplica", "desconocido"}
)
MODOS_ENVIO: frozenset[str] = frozenset({"repartir", "gasto_aparte", "no_hubo", "desconocido"})

_CENTAVO = Decimal("0.01")

#: Motivo que ven el usuario y «Otros» cuando una compra no trae artículos.
MOTIVO_COMPRA_SIN_ARTICULOS = (
    "Compra de mercadería sin artículos: indicá qué productos la componen y cuántos"
)


class ComprobanteInvalidoError(ValueError):
    """La compra no se puede registrar tal como vino. El mensaje es para el usuario."""


@dataclass(frozen=True)
class LineaDeCompra:
    product_id: uuid.UUID
    #: En unidad de VENTA del producto («5» de queso = 5 kg).
    cantidad: int
    #: Lo que dice el comprobante para esta línea (ver el docstring del módulo).
    importe: Decimal


@dataclass(frozen=True)
class Envio:
    monto: Decimal = Decimal("0")
    modo: ModoEnvio = "desconocido"


@dataclass(frozen=True)
class Iva:
    tratamiento: TratamientoIva = "desconocido"
    #: Fracción (0,21 = 21 %). Sólo con ``incluido`` / ``adicional``.
    alicuota: Decimal | None = None


@dataclass(frozen=True)
class LineaCosteada:
    product_id: uuid.UUID
    cantidad: int
    #: Lo que se registra como gasto de mercadería: lo que salió por la línea
    #: (importe + su parte del envío repartido + el IVA si se pagó aparte).
    importe_gasto: Decimal
    #: Costo por unidad de venta que entra al inventario.
    costo_unitario: Decimal
    contrato: ContratoDeCosto


@dataclass(frozen=True)
class ComprobanteCosteado:
    lineas: list[LineaCosteada]
    #: Envío que va como gasto de logística aparte (0 si no hay).
    gasto_envio: Decimal

    @property
    def total(self) -> Decimal:
        return sum((linea.importe_gasto for linea in self.lineas), Decimal("0")) + self.gasto_envio


def validar_linea(cantidad: int, importe: Decimal) -> None:
    """Regla de línea compartida por todas las rutas (también los importadores)."""
    if cantidad < 1:
        raise ComprobanteInvalidoError(
            "La cantidad de cada artículo tiene que ser un entero de 1 o más. "
            "Una devolución no se carga como compra."
        )
    if importe <= 0:
        raise ComprobanteInvalidoError(
            "El importe de cada artículo tiene que ser mayor que cero. Una "
            "bonificación se carga como menor precio de la línea que se pagó."
        )


def _validar_iva(iva: Iva) -> None:
    if iva.tratamiento not in TRATAMIENTOS_IVA:
        raise ComprobanteInvalidoError(f"Tratamiento de IVA desconocido: {iva.tratamiento!r}.")
    con_alicuota = iva.tratamiento in ("incluido", "adicional")
    if con_alicuota and (iva.alicuota is None or not (0 < iva.alicuota < 1)):
        raise ComprobanteInvalidoError(
            "Indicá la alícuota del IVA (por ejemplo 21 %), o marcá que no aplica o "
            "que no lo sabés."
        )
    if not con_alicuota and iva.alicuota is not None:
        raise ComprobanteInvalidoError("Una alícuota sólo va con IVA incluido o adicional.")


def _validar_envio(envio: Envio) -> None:
    if envio.modo not in MODOS_ENVIO:
        raise ComprobanteInvalidoError(f"Modo de envío desconocido: {envio.modo!r}.")
    if envio.monto < 0:
        raise ComprobanteInvalidoError("El envío no puede ser negativo.")
    if envio.modo in ("repartir", "gasto_aparte") and envio.monto <= 0:
        raise ComprobanteInvalidoError("Indicá cuánto costó el envío.")
    if envio.modo in ("no_hubo", "desconocido") and envio.monto > 0:
        raise ComprobanteInvalidoError(
            "Hay un monto de envío: indicá si entra al costo de los artículos o va "
            "como gasto aparte."
        )


_ENVIO_A_TRATAMIENTO: dict[str, Tratamiento] = {
    "repartir": "incluido",
    "gasto_aparte": "adicional",
    "no_hubo": "no_aplica",
    "desconocido": "desconocido",
}


def costear_comprobante(
    lineas: list[LineaDeCompra],
    *,
    envio: Envio | None = None,
    iva: Iva | None = None,
    document_ref: str | None = None,
    permitir_repetidos: bool = False,
) -> ComprobanteCosteado:
    """El costo y el contrato de cada línea de una compra. Ver el docstring.

    ``permitir_repetidos``: el remito acepta el mismo artículo en dos líneas (dos
    lotes con precio distinto), como siempre; la carga manual pide unificarlas.
    """
    envio = envio or Envio()
    iva = iva or Iva()
    if not lineas:
        raise ComprobanteInvalidoError(MOTIVO_COMPRA_SIN_ARTICULOS + ".")
    vistos: set[uuid.UUID] = set()
    for linea in lineas:
        validar_linea(linea.cantidad, linea.importe)
        if linea.product_id in vistos and not permitir_repetidos:
            raise ComprobanteInvalidoError(
                "Hay un artículo repetido en la compra. Unificá la cantidad en una "
                "sola línea."
            )
        vistos.add(linea.product_id)
    _validar_iva(iva)
    _validar_envio(envio)

    reparte = envio.modo == "repartir"
    plan = build_line_costs(
        [
            CostLine(row_index=i, amount=linea.importe, quantity=linea.cantidad)
            for i, linea in enumerate(lineas)
        ],
        shared_shipping=envio.monto if reparte else Decimal("0"),
        shared_mode=COMPARTIDO_SUBTOTAL if reparte else COMPARTIDO_NO,
        basis=BASE_INCLUYE,
    )
    por_fila = plan.by_row()
    rate = iva.alicuota
    costeadas: list[LineaCosteada] = []
    for i, linea in enumerate(lineas):
        lc = por_fila[i]
        costo_unitario = (lc.total / linea.cantidad).quantize(_CENTAVO, ROUND_HALF_UP)
        tax_amount: Decimal | None = None
        iva_aparte = Decimal("0")
        if rate is not None and iva.tratamiento == "incluido":
            tax_amount = (linea.importe * rate / (1 + rate) / linea.cantidad).quantize(
                _CENTAVO, ROUND_HALF_UP
            )
        elif rate is not None and iva.tratamiento == "adicional":
            iva_aparte = (linea.importe * rate).quantize(_CENTAVO, ROUND_HALF_UP)
            tax_amount = (linea.importe * rate / linea.cantidad).quantize(
                _CENTAVO, ROUND_HALF_UP
            )
        costeadas.append(
            LineaCosteada(
                product_id=linea.product_id,
                cantidad=linea.cantidad,
                importe_gasto=(lc.total + iva_aparte).quantize(_CENTAVO, ROUND_HALF_UP),
                costo_unitario=costo_unitario,
                contrato=ContratoDeCosto(
                    unit_cost_final=costo_unitario,
                    shipping_treatment=_ENVIO_A_TRATAMIENTO[envio.modo],
                    tax_treatment=iva.tratamiento,
                    tax_rate=rate,
                    tax_amount=tax_amount,
                    document_ref=(document_ref or "").strip()[:100] or None,
                ),
            )
        )
    return ComprobanteCosteado(
        lineas=costeadas,
        gasto_envio=(
            envio.monto.quantize(_CENTAVO, ROUND_HALF_UP)
            if envio.modo == "gasto_aparte"
            else Decimal("0")
        ),
    )
