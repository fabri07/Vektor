"""Costo congelado de una línea de venta (M·2a).

Puro: sin sesión ni ORM. Responde «¿qué costo se lleva esta venta, a su fecha de
negocio, y es una base comparable confirmada?». Lo escribe el servidor al
confirmar la venta (``persistence/costeo_de_ventas.py``) y no se revalúa: una
compra posterior no cambia una venta pasada.

- **La fuente es la elegible al DÍA de la venta**, con la misma regla que el costo
  vigente (M·1c, ``decidir_costo_vigente``): la más reciente entre la última
  compra o stock inicial a ese día (M·1a), la última edición manual y la última
  carga de catálogo, si son de ese día o anteriores. Una edición POSTERIOR a la
  venta no es elegible. Del historial de ediciones sólo se guarda la última: si
  es posterior a la venta, se ignora (no se sabe qué valía antes).
- **La base comparable es la de M·1d** (``base_comparable``): sólo una compra con
  envío e impuesto conocidos y régimen definido da un costo confirmado. Una
  edición manual, el catálogo, el stock inicial o el costo cargado sin fecha son
  ESTIMACIÓN, con su motivo.
- **Unidades:** el costo es por UNIDAD DE VENTA y ``quantity`` está en unidades
  base (``domain/sale_unit.py``). El costo de la línea es ``importe(...)``: la
  misma cuenta, con el mismo redondeo, que el precio.
- Sin costo → ``sin_costo``: la venta entra con el margen PENDIENTE, nunca con
  costo 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Final, Literal

from app.domain.base_comparable import CENTAVO, base_comparable
from app.domain.costo_de_referencia import CostoDeReferencia
from app.domain.costo_vigente import CostoFechado, decidir_costo_vigente
from app.domain.sale_unit import importe

#: Versión del snapshot (``sales_entries.cost_snapshot_version``).
VERSION_SNAPSHOT: Final = 1

OrigenDeVenta = Literal[
    "compra", "stock_inicial", "manual", "catalogo", "costo_cargado", "sin_costo"
]

MOTIVO_EDICION: Final = "el costo sale de una carga manual o del catálogo, no de una compra"


@dataclass(frozen=True)
class CostoDeVenta:
    #: Costo por unidad de venta (la base comparable, o la estimación).
    costo_unitario: Decimal | None
    #: Costo de la línea: ``costo_unitario × unidades base / factor``.
    costo_linea: Decimal | None
    confirmado: bool
    origen: OrigenDeVenta
    motivo: str | None


def _elegible(edicion: CostoFechado | None, dia: date) -> CostoFechado | None:
    return edicion if edicion is not None and edicion.fecha.date() <= dia else None


def fuente_para_venta(
    referencia: CostoDeReferencia,
    manual: CostoFechado | None,
    catalogo: CostoFechado | None,
    dia_de_venta: date,
) -> tuple[CostoDeReferencia, OrigenDeVenta]:
    """La fuente elegible al día de la venta, como ``CostoDeReferencia``.

    ``referencia`` es el costo de referencia a ese mismo día (M·1a). Si gana una
    edición, se devuelve como un costo sin compra (estimación), con su origen.
    """
    vigente = decidir_costo_vigente(
        referencia, _elegible(manual, dia_de_venta), _elegible(catalogo, dia_de_venta)
    )
    if vigente is None or vigente.origen in ("compra", "stock_inicial"):
        return referencia, referencia.origen
    edicion = CostoDeReferencia(
        origen="costo_cargado",
        costo_unitario=vigente.costo,
        fecha=vigente.fecha,
        motivo=MOTIVO_EDICION,
    )
    return edicion, vigente.origen


def costo_de_venta(
    referencia: CostoDeReferencia,
    manual: CostoFechado | None,
    catalogo: CostoFechado | None,
    *,
    dia_de_venta: date,
    unidades_base: int,
    base_units_per_sale_unit: int,
    fiscal_condition_crudo: str | None,
) -> CostoDeVenta:
    """El snapshot de costo de una línea de venta (ver reglas arriba)."""
    fuente, origen = fuente_para_venta(referencia, manual, catalogo, dia_de_venta)
    base = base_comparable(fuente, fiscal_condition_crudo)
    if base.costo is None:
        return CostoDeVenta(None, None, False, "sin_costo", base.motivo)
    motivo = None if base.confirmada else (
        MOTIVO_EDICION if origen in ("manual", "catalogo") else base.motivo
    )
    # El unitario se guarda en centavos (``Numeric(12, 2)``): la línea sale de ESE
    # valor, para que ``cost_unit_ars × cantidad`` y ``cost_ars`` no difieran en
    # un centavo (un costo cargado de 800,567 daba 800,57 y una línea de 1601,13).
    unitario = base.costo.quantize(CENTAVO, rounding=ROUND_HALF_UP)
    return CostoDeVenta(
        costo_unitario=unitario,
        costo_linea=importe(unitario, unidades_base, base_units_per_sale_unit),
        confirmado=base.confirmada,
        origen=origen,
        motivo=motivo,
    )
