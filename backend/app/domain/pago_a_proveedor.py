"""«Pago a proveedor»: la plata con que se cancela una compra que quedó fiada.

Una compra a cuenta corriente ya se contó como costo el día que se hizo; el pago, después,
NO es otro gasto: saca plata de la caja y baja la deuda con el proveedor (decisión del
dueño, 2026-11-16). Una hoja de gastos lo trae como una fila más («Pago a proveedor»
con el medio con que se pagó) y entraba como gasto, contando el mismo costo dos veces.

La señal es el TEXTO de la fila y se exige un par de palabras: una de ACCIÓN (pago,
cancelación, abono) y «proveedor». Un «Proveedor» a secas es una compra y «Pago de
alquiler» es un gasto; lo ambiguo sigue siendo gasto, nunca se adivina.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

from app.domain.text_norm import normalize_text

_ACCION: Final = ("pago", "pagos", "abono", "cancelacion", "cancelo", "pague", "saldo")
_PROVEEDOR: Final = ("proveedor", "proveedores", "prov")


def es_texto_de_pago_a_proveedor(*textos: str | None) -> bool:
    """Alguno de los textos de la fila dice que se le pagó a un proveedor."""
    for texto in textos:
        t = normalize_text(texto or "")
        if not t:
            continue
        palabras = set(t.replace(".", " ").split())
        if any(a in palabras for a in _ACCION) and any(p in palabras for p in _PROVEEDOR):
            return True
    return False


def alguno_es_pago_a_proveedor(textos: Iterable[str | None]) -> bool:
    return es_texto_de_pago_a_proveedor(*textos)
