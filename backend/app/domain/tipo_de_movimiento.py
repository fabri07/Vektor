"""Libro mixto: qué es cada fila según su "tipo de movimiento".

Un libro mixto registra ventas, gastos, compras y movimientos de caja en la MISMA
hoja, y una columna dice qué es cada fila ("Venta", "Flete / envío", "Aporte de
socio"…). Clasificar la hoja entera como una sola cosa —lo que pasaba— rutea
todas las filas al mismo destino: en ASTERIA (2026-10-05) 2.957 movimientos
terminaron propuestos como productos.

Reglas, a propósito conservadoras:

* Se clasifica por PALABRAS del tipo, en orden, y la primera que matchea gana:
  "devolución de proveedor" tiene que ser devolución antes que gasto de
  proveedor, y "mercadería recibida" no es una compra.
* Sólo venta, gasto y compra se importan. Todo movimiento de capital o de caja
  (aporte, retiro, préstamo, saldo inicial, ajuste, devolución) va a «Otros» con
  un motivo que dice qué es: hoy no son operaciones de Véktor, y forzarlos a
  venta o gasto inventaría facturación o costos.
* Un tipo desconocido NUNCA se adivina: «Otros», con el valor a la vista.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

Destino = Literal["venta", "gasto", "compra", "otros"]

#: Versión del vocabulario. Cambiar una regla cambia a dónde va una fila; la
#: versión permite saber con qué reglas se clasificó un archivo.
VERSION = 1


@dataclass(frozen=True)
class Clasificacion:
    destino: Destino
    #: Por qué va a «Otros» (sólo para ese destino), en castellano.
    motivo: str | None = None
    #: Categoría de gasto sugerida; ``None`` deja que la infiera el catálogo.
    categoria: str | None = None


def _normalizar(texto: str) -> str:
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFD", texto.lower()) if unicodedata.category(c) != "Mn"
    )
    return re.sub(r"[^a-z0-9]+", " ", sin_tildes).strip()


_SALDO_INICIAL = Clasificacion(
    "otros", "Saldo inicial de caja: es el punto de partida, no una venta ni un gasto"
)
_APORTE = Clasificacion(
    "otros", "Aporte de socio: es plata que pone el dueño, no una venta"
)
_RETIRO = Clasificacion(
    "otros",
    "Retiro de socios: no es un gasto del negocio. Si es un retiro de ganancias, "
    "cargalo como tal desde el Balance",
)
_PRESTAMO = Clasificacion(
    "otros", "Préstamo: es una deuda que entra o sale, no una venta ni un gasto"
)
_DEVOLUCION = Clasificacion(
    "otros", "Devolución: Véktor todavía no registra devoluciones; revisala a mano"
)
_AJUSTE = Clasificacion("otros", "Ajuste: revisá a mano qué corrige antes de importarlo")
_MERCADERIA_RECIBIDA = Clasificacion(
    "otros",
    "Ingreso de mercadería sin pago: es la otra cara de una compra. Importarlo "
    "además de la compra contaría dos veces el costo",
)

#: (palabras que tienen que aparecer TODAS, clasificación). En orden.
_REGLAS: tuple[tuple[tuple[str, ...], Clasificacion], ...] = (
    (("saldo", "inicial"), _SALDO_INICIAL),
    (("saldo", "anterior"), _SALDO_INICIAL),
    (("aporte",), _APORTE),
    (("retiro",), _RETIRO),
    (("prestamo",), _PRESTAMO),
    (("devolucion",), _DEVOLUCION),
    (("devolvio",), _DEVOLUCION),
    (("reintegro",), _DEVOLUCION),
    (("nota", "credito"), _DEVOLUCION),
    (("ajuste",), _AJUSTE),
    (("mercaderia", "recibida"), _MERCADERIA_RECIBIDA),
    (("recepcion",), _MERCADERIA_RECIBIDA),
    (("ingreso", "mercaderia"), _MERCADERIA_RECIBIDA),
    (("compra",), Clasificacion("compra", categoria="Mercadería")),
    (("venta",), Clasificacion("venta")),
    (("cobro",), Clasificacion("venta")),
    (("gasto",), Clasificacion("gasto")),
    (("flete",), Clasificacion("gasto")),
    (("envio",), Clasificacion("gasto")),
    (("publicidad",), Clasificacion("gasto")),
    (("packaging",), Clasificacion("gasto")),
    (("insumo",), Clasificacion("gasto")),
    (("servicio",), Clasificacion("gasto")),
    (("alquiler",), Clasificacion("gasto")),
    (("sueldo",), Clasificacion("gasto")),
    (("impuesto",), Clasificacion("gasto")),
    (("comision",), Clasificacion("gasto")),
)


def clasificar(tipo: object) -> Clasificacion:
    """La clasificación de un valor de la columna de tipo."""
    texto = _normalizar(str(tipo)) if tipo is not None else ""
    if not texto:
        return Clasificacion("otros", "La fila no dice qué tipo de movimiento es")
    palabras = set(texto.split())
    for claves, clasificacion in _REGLAS:
        # Coincidencia por PREFIJO de palabra: "ventas", "gastos", "envíos".
        if all(any(p.startswith(c) for p in palabras) for c in claves):
            return clasificacion
    return Clasificacion("otros", f"Tipo de movimiento desconocido: «{str(tipo).strip()}»")


def es_reconocido(tipo: object) -> bool:
    """¿El valor es un tipo del vocabulario (aunque su destino sea «Otros»)?"""
    motivo = clasificar(tipo).motivo
    return motivo is None or not motivo.startswith(("Tipo de movimiento desconocido", "La fila"))
