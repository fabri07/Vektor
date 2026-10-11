"""Contrato del costo de una compra, tal como lo escribe cada ruta (M·1b).

Puro: sin sesión ni ORM. Traduce lo que cada ruta SABE de una compra a las
columnas del contrato en ``inventory_movements`` (M·1a). La regla es una sola:
**lo que la ruta no sabe queda ``desconocido`` / NULL**, nunca un default que
parezca dato.

- **Compra manual, «Otros», chat y remito:** el costo por unidad es lo que el
  usuario declaró. Envío e impuesto quedan ``desconocido``; el remito marca el
  envío ``adicional`` cuando lo declaró aparte (va como gasto de logística).
- **Ingesta:** sale del motor de ``purchase_cost.py`` (``LineCost``), calculado al
  registrar y sobre el mismo comprobante:
  - envío ``incluido`` si se capitalizó (propio o repartido), ``adicional`` si
    había envío y quedó fuera del costo, ``desconocido`` si la hoja no dice nada;
  - impuesto ``incluido`` (con su importe por unidad) sólo si la hoja declaró
    ``monto_sin_ajustes`` y el impuesto se sumó al costo. Con ``monto_incluye`` el
    impuesto queda ``desconocido``: que el monto «ya traiga los ajustes» no prueba
    que haya IVA ni cuánto.
- **Nunca:** tasa del 21% por defecto, ``tax_recoverable`` confirmado ni
  ``no_aplica`` deducido de una celda vacía. Eso lo confirma el usuario por
  operación (pantalla de M·3 / B·5).

``tax_amount`` es el impuesto **por unidad**, en la misma unidad que
``unit_cost_final``, para que M·1d compare costos unitarios sin dividir de nuevo.

La versión (``CONTRATO_VERSION``) marca que el movimiento se escribió con el
contrato; no dice que envío o impuesto estén confirmados.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.domain.costo_de_referencia import CONTRATO_VERSION, TRATAMIENTOS, Tratamiento
from app.domain.purchase_cost import LineCost

#: Largo de ``inventory_movements.document_ref``.
MAX_DOCUMENT_REF = 100


@dataclass(frozen=True)
class ContratoDeCosto:
    unit_cost_final: Decimal | None
    shipping_treatment: Tratamiento = "desconocido"
    tax_treatment: Tratamiento = "desconocido"
    tax_rate: Decimal | None = None
    #: Por unidad (ver docstring del módulo).
    tax_amount: Decimal | None = None
    tax_recoverable: bool | None = None
    document_ref: str | None = None
    version: int = CONTRATO_VERSION

    def __post_init__(self) -> None:
        if self.shipping_treatment not in TRATAMIENTOS:
            raise ValueError(f"shipping_treatment inválido: {self.shipping_treatment!r}")
        if self.tax_treatment not in TRATAMIENTOS:
            raise ValueError(f"tax_treatment inválido: {self.tax_treatment!r}")
        if self.unit_cost_final is not None and self.unit_cost_final < 0:
            raise ValueError("unit_cost_final no puede ser negativo")
        if self.tax_amount is not None and self.tax_amount < 0:
            raise ValueError("tax_amount no puede ser negativo")

    def columnas(self) -> dict[str, Any]:
        """Los valores para las columnas homónimas de ``inventory_movements``."""
        return {
            "unit_cost_final": self.unit_cost_final,
            "shipping_treatment": self.shipping_treatment,
            "tax_treatment": self.tax_treatment,
            "tax_rate": self.tax_rate,
            "tax_amount": self.tax_amount,
            "tax_recoverable": self.tax_recoverable,
            "document_ref": self.document_ref,
            "cost_contract_version": self.version,
        }


def _documento(valor: str | None) -> str | None:
    if valor is None:
        return None
    limpio = valor.strip()
    return limpio[:MAX_DOCUMENT_REF] or None


def contrato_declarado(
    costo_unitario: Decimal | None,
    *,
    envio: Tratamiento = "desconocido",
    document_ref: str | None = None,
) -> ContratoDeCosto:
    """Compra cargada a mano (manual, «Otros», chat, remito): lo que se declaró."""
    return ContratoDeCosto(
        unit_cost_final=costo_unitario,
        shipping_treatment=envio,
        document_ref=_documento(document_ref),
    )


def contrato_de_linea_importada(
    costo_facturado: Decimal | None,
    linea: LineCost | None,
    *,
    document_ref: str | None = None,
) -> ContratoDeCosto:
    """Compra de un archivo: el contrato que deja el motor de costos de la hoja.

    ``linea`` es ``None`` cuando la hoja no mapeó ninguna columna de ajuste: no
    se calculó nada y el costo final es el facturado, con envío e impuesto
    desconocidos. El costo final es el mismo que el importador usa como costo de
    referencia: el del motor si dio un unitario, si no el facturado.
    """
    if linea is None:
        return ContratoDeCosto(
            unit_cost_final=costo_facturado, document_ref=_documento(document_ref)
        )
    envio: Tratamiento
    if linea.shipping_allocated or linea.shipping_line_applied:
        envio = "incluido"
    elif linea.envio_fuera_del_costo:
        envio = "adicional"
    else:
        envio = "desconocido"
    con_impuesto = linea.impuesto_unitario is not None and linea.impuesto_unitario > 0
    return ContratoDeCosto(
        unit_cost_final=linea.unit_cost_final or costo_facturado,
        shipping_treatment=envio,
        tax_treatment="incluido" if con_impuesto else "desconocido",
        tax_amount=linea.impuesto_unitario if con_impuesto else None,
        document_ref=_documento(document_ref),
    )
