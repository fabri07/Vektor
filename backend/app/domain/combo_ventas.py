"""Combos de venta: «Jean + buzo» $41.000 → qué jean y qué buzo suman ese importe.

Una fila de «Otros» dice un solo monto y un texto con varios productos. Véktor no
adivina: busca, entre los productos que SÍ pueden venderse en esa fecha, las
combinaciones cuyo precio de venta vigente sumado da EXACTAMENTE el monto, y se las
propone a la persona. Una sola combinación posible se prellena; varias o ninguna se
muestran o se dejan para armar a mano. Nunca se escribe nada acá: es una cuenta pura.

El precio es el VIGENTE del producto, no el de la fecha de la venta (puede haber
sido otro): por eso es una propuesta que la persona confirma, no un dato.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from itertools import product as producto_cartesiano
from typing import Final

from app.domain.sale_unit import importe
from app.domain.text_norm import normalize_text

#: Referencias máximas de un combo y unidades máximas por línea: acotan la búsqueda
#: (el espacio crece como candidatos^referencias × cantidades^referencias).
MAX_REFERENCIAS: Final = 4
MAX_CANTIDAD_POR_LINEA: Final = 10
MAX_CANDIDATOS_POR_REFERENCIA: Final = 12
MAX_SOLUCIONES: Final = 20
#: Tope de combinaciones a revisar: con 3-4 referencias se bajan las unidades por
#: línea en vez de colgar la request.
MAX_ESPACIO_DE_BUSQUEDA: Final = 400_000

_SEPARADOR = re.compile(r"\s*(?:\+|,|;|/|&|\by\b|\be\b)\s*", re.IGNORECASE)
_CANTIDAD_ADELANTE = re.compile(r"^\d+\s*x?\s+")


def plegar_plural(token: str) -> str:
    """«jeans»/«jean», «buzos»/«buzo»: la misma raíz (sólo palabras; mismo criterio que
    el plegado de la identidad de productos de la importación)."""
    if any(c.isdigit() for c in token):
        return token
    if len(token) > 3 and token.endswith("s"):
        token = token[:-1]
    if len(token) > 3 and token.endswith("e"):
        token = token[:-1]
    return token


def _raices(texto: str) -> list[str]:
    return [plegar_plural(t) for t in normalize_text(texto).split() if t]


def referencias_de(texto: str | None) -> list[str]:
    """Las referencias de un combo: «Jean + buzo» → ``["jean", "buzo"]``.

    Una sola referencia (o ninguna) no es un combo: devuelve ``[]``. Más de
    ``MAX_REFERENCIAS`` tampoco (no se acota a la fuerza: se deja a mano).
    """
    if not texto or not texto.strip():
        return []
    partes = [
        _CANTIDAD_ADELANTE.sub("", p.strip())
        for p in _SEPARADOR.split(texto.strip())
        if p and p.strip()
    ]
    partes = [p for p in partes if normalize_text(p)]
    if len(partes) < 2 or len(partes) > MAX_REFERENCIAS:
        return []
    return partes


@dataclass(frozen=True)
class Candidato:
    """Un producto que puede venderse en la fecha del combo."""

    product_id: str
    nombre: str
    #: Precio de venta vigente por unidad de VENTA.
    precio: Decimal
    #: Unidades base por unidad de venta (1 salvo kg/L).
    factor: int = 1


@dataclass(frozen=True)
class LineaDeCombo:
    product_id: str
    nombre: str
    cantidad: int
    precio: Decimal
    importe: Decimal


def candidatos_de_referencia(
    referencia: str, productos: Iterable[Candidato]
) -> list[Candidato]:
    """Los productos cuyo nombre contiene TODAS las palabras de la referencia
    (con plural plegado): «jean» → «Jean azul T40» y «Jeans azules T42»."""
    buscadas = _raices(referencia)
    if not buscadas:
        return []
    salida = []
    for p in productos:
        del_producto = set(_raices(p.nombre))
        if all(b in del_producto for b in buscadas):
            salida.append(p)
    return sorted(salida, key=lambda c: (c.nombre, c.product_id))[:MAX_CANDIDATOS_POR_REFERENCIA]


def _importe_de(c: Candidato, cantidad: int) -> Decimal:
    return importe(c.precio, cantidad * c.factor, c.factor)


def resolver(
    monto: Decimal,
    por_referencia: Sequence[Sequence[Candidato]],
    *,
    max_cantidad: int = MAX_CANTIDAD_POR_LINEA,
    max_soluciones: int = MAX_SOLUCIONES,
) -> list[list[LineaDeCombo]]:
    """Las combinaciones —un producto DISTINTO por referencia, de 1 a ``max_cantidad``
    unidades cada uno— cuyo importe sumado es EXACTAMENTE ``monto``.

    Sin solución (o con una referencia sin candidatos) → ``[]``. Las soluciones salen
    en un orden estable: menos unidades primero, después por nombre.
    """
    if not por_referencia or any(len(c) == 0 for c in por_referencia):
        return []
    productos_posibles = 1
    for c in por_referencia:
        productos_posibles *= len(c)
    while max_cantidad > 1 and (
        productos_posibles * max_cantidad ** len(por_referencia) > MAX_ESPACIO_DE_BUSQUEDA
    ):
        max_cantidad -= 1
    soluciones: list[tuple[int, list[LineaDeCombo]]] = []
    for elegidos in producto_cartesiano(*por_referencia):
        ids = [c.product_id for c in elegidos]
        if len(set(ids)) != len(ids):
            continue  # un mismo producto en dos referencias no es un combo
        for cantidades in producto_cartesiano(range(1, max_cantidad + 1), repeat=len(elegidos)):
            total = sum(
                (_importe_de(c, q) for c, q in zip(elegidos, cantidades, strict=True)),
                Decimal("0"),
            )
            if total != monto:
                continue
            lineas = [
                LineaDeCombo(c.product_id, c.nombre, q, c.precio, _importe_de(c, q))
                for c, q in zip(elegidos, cantidades, strict=True)
            ]
            soluciones.append((sum(cantidades), lineas))
    soluciones.sort(key=lambda s: (s[0], [(ln.nombre, ln.cantidad) for ln in s[1]]))
    return [lineas for _, lineas in soluciones[:max_soluciones]]
