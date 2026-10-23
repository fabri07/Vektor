"""El costo promedio de una venta (M·P3): un DATO al lado del costo principal.

Puro: sin sesión ni ORM. El historial materializado (``product_cost_history``)
guarda el estado del producto después de cada evento que lo cambia; el costo
promedio de una venta del día D es el del último registro con día ≤ D (al cierre
del día: una compra del mismo día ya cuenta, igual que en ``costo_promedio``,
donde los créditos del día van antes que las salidas).

- ``medido`` o ``incompleto`` (decisión del dueño, 2026-10-14: la incompleta
  guarda el promedio parcial de las unidades con costo, con su estado): el
  promedio por unidad de venta redondeado a centavos y la línea con
  ``importe(unitario, unidades base, factor)``, la MISMA regla que ``cost_ars``.
- ``sin_medicion``: sin valores, con el estado y el motivo.
- Sin registro a esa fecha: ``sin_medicion`` con «todavía no hay compras con
  costo».
- Una venta cuya cantidad no está confirmada en unidades base, de un producto
  fraccionado: sin valores (lo mismo que el costo principal).
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from app.domain.costo_promedio import MOTIVO_SIN_COMPRAS, Estado
from app.domain.sale_unit import importe

CENTAVO: Final = Decimal("0.01")


@dataclass(frozen=True)
class PuntoDelHistorial:
    """Lo que hace falta de un registro del historial para costear una venta."""

    dia: date
    promedio: Decimal | None
    estado: Estado
    aproximado: bool
    motivo: str | None


@dataclass(frozen=True)
class CostoPromedioDeVenta:
    unitario: Decimal | None
    linea: Decimal | None
    estado: Estado | None
    aproximado: bool | None
    motivo: str | None

    def columnas(self, *, pendiente: bool) -> dict[str, object]:
        """Los valores para las columnas ``cost_avg_*`` de ``sales_entries``."""
        return {
            "cost_avg_unit_ars": self.unitario,
            "cost_avg_ars": self.linea,
            "cost_avg_estado": self.estado,
            "cost_avg_aproximado": self.aproximado,
            "cost_avg_motivo": None if self.motivo is None else self.motivo[:200],
            "cost_avg_pendiente": pendiente,
        }


def punto_a(
    puntos: Sequence[PuntoDelHistorial], dias: Sequence[date], dia: date
) -> PuntoDelHistorial | None:
    """El último punto con día ≤ ``dia``. ``dias`` es ``[p.dia for p in puntos]``
    (en orden), aparte para no rearmarlo en cada venta."""
    i = bisect_right(dias, dia)
    return puntos[i - 1] if i else None


def costo_promedio_de_venta(
    punto: PuntoDelHistorial | None,
    *,
    unidades_base: int,
    base_units_per_sale_unit: int,
    unidad_dudosa: bool = False,
    motivo_dudosa: str | None = None,
) -> CostoPromedioDeVenta:
    if unidad_dudosa and base_units_per_sale_unit != 1:
        return CostoPromedioDeVenta(None, None, None, None, motivo_dudosa)
    if punto is None:
        return CostoPromedioDeVenta(None, None, "sin_medicion", False, MOTIVO_SIN_COMPRAS)
    if punto.promedio is None or punto.estado == "sin_medicion":
        return CostoPromedioDeVenta(None, None, punto.estado, punto.aproximado, punto.motivo)
    unitario = punto.promedio.quantize(CENTAVO, rounding=ROUND_HALF_UP)
    return CostoPromedioDeVenta(
        unitario,
        importe(unitario, unidades_base, base_units_per_sale_unit),
        punto.estado,
        punto.aproximado,
        punto.motivo,
    )
