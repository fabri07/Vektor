"""Por qué un monto de operación es negativo, dicho desde la fila (Fase 2).

Un negativo no es un error por sí mismo: puede ser una devolución, un signo
invertido al tipear, o un cálculo que salió mal. Lo único que NO se hace es
corregirlo: la fila va a «Otros» y este módulo elige el motivo que se le muestra
al usuario, con la evidencia que la misma fila trae. Nunca se invierte el signo
automáticamente (no-invention).

Sólo aplica a montos de OPERACIÓN (venta, gasto, compra) sobre el mapeo
efectivo. Una columna de saldo, flujo o resultado negativa es un dato —una
pérdida es un dato— y nunca llega acá.
"""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from app.domain.numeric_parsing import parsear_monto

Motivo = Literal[
    "parece_devolucion",
    "signo_probablemente_invertido",
    "no_cuadra_con_la_fila",
    "negativo_sin_contexto",
]

_DEVOLUCION = re.compile(
    r"\b(devol|anulac|anulad|nota de credito|nc\b|reintegr|reembols|cancelac)"
)
#: Tolerancia para comparar |monto| con cantidad × precio: un centavo por el
#: redondeo de quien armó la planilla.
_TOLERANCIA = Decimal("0.01")


def _normalizar(texto: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", texto.lower()) if unicodedata.category(c) != "Mn"
    )


def _decimal(valor: Any) -> Decimal | None:
    if valor is None or not str(valor).strip():
        return None
    if isinstance(valor, Decimal):
        return valor
    try:
        return Decimal(str(valor)) if isinstance(valor, int | float) else parsear_monto(valor).valor
    except InvalidOperation:
        return None


def motivo_de_negativo(
    monto: Decimal,
    fila: dict[str, Any] | None,
    cantidad: Any = None,
    precio_unitario: Any = None,
) -> Motivo:
    """El motivo, en orden de evidencia: lo que la fila DICE, después lo que la
    aritmética de la fila PRUEBA, y si no hay nada, que no hay contexto."""
    textos = " ".join(
        str(v) for v in (fila or {}).values() if isinstance(v, str) and not v.startswith("sheet:")
    )
    if _DEVOLUCION.search(_normalizar(textos)):
        return "parece_devolucion"
    cant = _decimal(cantidad)
    precio = _decimal(precio_unitario)
    if cant is not None and precio is not None and cant != 0:
        esperado = abs(cant * precio)
        if abs(abs(monto) - esperado) <= _TOLERANCIA:
            return "signo_probablemente_invertido"
        return "no_cuadra_con_la_fila"
    return "negativo_sin_contexto"


def texto_de_motivo(motivo: Motivo, crudo: Any) -> str:
    """Lo que ve el usuario en «Otros». Siempre con el valor original."""
    valor = str(crudo).strip()
    return {
        "parece_devolucion": (
            f"Monto negativo ({valor}) en una fila que parece una devolución o "
            "anulación: Véktor todavía no registra devoluciones, revisala a mano"
        ),
        "signo_probablemente_invertido": (
            f"Monto negativo ({valor}) que coincide con cantidad × precio de la fila: "
            "probablemente el signo está invertido. Corregilo y volvé a importarla"
        ),
        "no_cuadra_con_la_fila": (
            f"Monto negativo ({valor}) que no coincide con cantidad × precio de la "
            "fila: puede ser un error de cálculo o de tipeo"
        ),
        "negativo_sin_contexto": (
            f"Monto negativo ({valor}): no se registra como operación. Si es una "
            "pérdida, un ajuste o una devolución, revisala a mano"
        ),
    }[motivo]
