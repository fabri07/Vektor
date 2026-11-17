"""«Cobro saldo fiado»: la plata de una venta fiada que ya se contó.

Un cobro de cuenta corriente NO es una venta: la venta fiada ya movió el stock y se
contó como ingreso el día que se hizo; lo que entra ahora es plata a la caja y baja la
deuda del cliente (`payment_method = "inflow"`, ver `_ventas_scope.py`). Una hoja de
ventas lo trae como una fila más: «Cobro saldo fiado» en el detalle y el medio real con
que se cobró (efectivo, transferencia) en la forma de pago.

La señal es el TEXTO del detalle, así que se exige un par de palabras y no una sola: un
«Cobro» a secas es una venta de mostrador, y «venta fiada» no es un cobro. Se corrobora
además con el resto de la fila (sin producto y con un medio de pago que no sea fiado);
lo que quede ambiguo sigue siendo una venta, nunca se adivina.
"""

from __future__ import annotations

from typing import Final

from app.domain.text_norm import normalize_text

_ACCION: Final = ("cobro", "cobranza", "cobre", "pago", "abono", "cancelacion", "cancelo")
_DEUDA: Final = (
    "saldo",
    "deuda",
    "fiado",
    "fiada",
    "cuenta corriente",
    "cta cte",
    "cta. cte",
    "adeudado",
)


def es_texto_de_cobro_de_fiado(texto: str | None) -> bool:
    """El detalle dice que se cobró una deuda («Cobro saldo fiado», «Pago de deuda»).

    Hace falta una palabra de ACCIÓN y una de DEUDA a la vez: «Cobro» solo es una venta
    y «Venta fiada» no es un cobro. Sin distinguir mayúsculas ni tildes.
    """
    t = normalize_text(texto or "")
    if not t:
        return False
    palabras = set(t.replace(".", " ").split())
    hay_accion = any(a in palabras for a in _ACCION)
    hay_deuda = any(d in t for d in _DEUDA)
    return hay_accion and hay_deuda
