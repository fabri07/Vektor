"""Hojas de capital de socios en una importación («Aportes de socios»).

Un aporte o retiro del socio es CAPITAL, nunca venta ni gasto (invariante del proyecto):
no entra al margen ni al resultado; sólo mueve la caja. Hasta ahora la importación no
tenía ese destino y «Reposición de caja» terminaba como compra de mercadería sin
artículos. Este módulo decide, POR COLUMNA y nunca por celda (convenio E4), si una hoja
es de capital y cómo se lee cada fila; no escribe nada.

Una hoja es de capital cuando tiene una columna de SOCIO y una de MONTO: el encabezado
«Socio» es la evidencia, y el nombre de la hoja sólo orienta el sentido (aporte o
retiro) cuando no hay una columna de tipo. Lo que no se puede leer sin adivinar —sin
fecha legible, sin medio de pago conocido, sin monto positivo, sentido ambiguo— NO se
carga: vuelve a «Otros» con el motivo, como el resto de la ingesta.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Final

from app.domain.date_parsing import parsear_fecha_de_columna
from app.domain.numeric_parsing import monto_positivo, parsear_monto
from app.domain.text_norm import normalize_text

KIND_APORTE: Final = "aporte"
KIND_RETIRO: Final = "retiro"
KIND_PRESTAMO: Final = "prestamo_socio"
KIND_DEVOLUCION: Final = "devolucion_prestamo"

MOTIVO_SIN_FECHA: Final = (
    "Aporte o retiro de socio sin fecha legible: indicá cuándo fue (no se carga con la de hoy)"
)
MOTIVO_SIN_MONTO: Final = "Aporte o retiro de socio sin un monto positivo legible: revisalo"
MOTIVO_SIN_MEDIO: Final = (
    "Aporte o retiro de socio sin una forma de pago reconocible (efectivo, transferencia, "
    "cuenta corriente…): indicá cómo se movió la plata"
)
MOTIVO_SIN_SENTIDO: Final = (
    "No se sabe si es un aporte o un retiro del socio: indicalo (no se adivina)"
)

_SOCIO = {"socio", "socia", "socios", "aportante", "partner"}
_MONTO = {
    "monto", "monto ars", "importe", "importe ars", "total", "total ars", "valor",
    "aporte", "aporte ars", "retiro", "retiro ars", "monto del movimiento",
}
_FECHA = {"fecha", "fecha del movimiento", "dia", "date", "fecha de aporte", "fecha aporte"}
_CONCEPTO = {"concepto", "detalle", "descripcion", "motivo", "nota", "notas", "observaciones"}
_MEDIO = {"forma", "forma de pago", "medio", "medio de pago", "pago", "metodo de pago", "metodo"}
_TIPO = {"tipo", "tipo de movimiento", "movimiento", "operacion", "sentido"}

#: Forma de pago de la hoja → medio del CHECK de `owner_capital_movements`.
_MEDIOS: Final[dict[str, str]] = {
    "efectivo": "cash",
    "cash": "cash",
    "contado": "cash",
    "transferencia": "transfer",
    "transferencia bancaria": "transfer",
    "transfer": "transfer",
    "deposito": "transfer",
    "cbu": "transfer",
    "debito": "debit_card",
    "tarjeta de debito": "debit_card",
    "credito": "credit_card",
    "tarjeta de credito": "credit_card",
    "tarjeta": "credit_card",
    "qr": "qr",
    "mercado pago": "qr",
    "mercadopago": "qr",
    "cuenta corriente": "account",
    "cta cte": "account",
    "otro": "other",
    "otros": "other",
}

#: Texto de la columna de tipo (o del nombre de hoja) → sentido del movimiento.
_SENTIDOS: Final[tuple[tuple[tuple[str, ...], str], ...]] = (
    (("devolucion de prestamo", "devolucion prestamo", "pago de prestamo"), KIND_DEVOLUCION),
    (("prestamo",), KIND_PRESTAMO),
    (("retiro", "retiros", "extraccion", "extracciones", "saco"), KIND_RETIRO),
    (("aporte", "aportes", "aporto", "ingreso de capital", "capital"), KIND_APORTE),
)


@dataclass(frozen=True)
class ColumnasDeCapital:
    """Los encabezados reales de la hoja que cumplen cada rol (``None`` = no hay)."""

    socio: str
    monto: str
    fecha: str | None
    concepto: str | None
    medio: str | None
    tipo: str | None


@dataclass(frozen=True)
class MovimientoDeCapital:
    kind: str
    amount: Decimal
    occurred_on: date
    payment_method: str
    partner_name: str | None
    notes: str | None
    #: Huella estable de la fila: la misma fila del mismo archivo, vuelta a cargar (otra
    #: subida, una relectura), da la misma y no se carga dos veces.
    source_ref: str


@dataclass(frozen=True)
class FilaRechazada:
    row: Mapping[str, Any]
    motivo: str


def _por_rol(headers: Sequence[str], nombres: set[str]) -> str | None:
    for h in headers:
        if normalize_text(h) in nombres:
            return h
    return None


def detectar_hoja_de_capital(headers: Sequence[str]) -> ColumnasDeCapital | None:
    """Las columnas de una hoja de capital, o ``None`` si la hoja no lo es.

    Hace falta una columna de socio Y una de monto. El socio es la evidencia: un
    encabezado «Concepto» o «Forma» solos no vuelven capital a una hoja de gastos.
    """
    socio = _por_rol(headers, _SOCIO)
    monto = _por_rol(headers, _MONTO)
    if socio is None or monto is None:
        return None
    return ColumnasDeCapital(
        socio=socio,
        monto=monto,
        fecha=_por_rol(headers, _FECHA),
        concepto=_por_rol(headers, _CONCEPTO),
        medio=_por_rol(headers, _MEDIO),
        tipo=_por_rol(headers, _TIPO),
    )


def _sentido_de(texto: str | None) -> str | None:
    t = normalize_text(texto or "")
    if not t:
        return None
    for claves, kind in _SENTIDOS:
        if any(c in t for c in claves):
            return kind
    return None


def sentido_por_nombre_de_hoja(nombre: str) -> str | None:
    """«Aportes de socios» → aporte; «Retiros» → retiro. Sólo orienta cuando la hoja no
    trae columna de tipo; una hoja que menciona los dos («Aportes y retiros») es
    ambigua y no decide."""
    t = normalize_text(nombre)
    hay_aporte = any(c in t for c in ("aporte", "capital"))
    hay_retiro = any(c in t for c in ("retiro", "extraccion"))
    if hay_aporte and not hay_retiro:
        return KIND_APORTE
    if hay_retiro and not hay_aporte:
        return KIND_RETIRO
    return None


def medio_de_pago(texto: Any) -> str | None:
    """El medio del CHECK de capital para lo que dice la hoja; ``None`` si no se
    reconoce (no se asume efectivo)."""
    return _MEDIOS.get(normalize_text(texto if texto is not None else ""))


def _huella(hoja: str, orden: int, kind: str, monto: Decimal, dia: date, socio: str, medio: str,
            concepto: str) -> str:
    base = "|".join(
        [normalize_text(hoja), str(orden), kind, str(monto), dia.isoformat(),
         normalize_text(socio), medio, normalize_text(concepto)]
    )
    return "arch:" + hashlib.sha256(base.encode("utf-8")).hexdigest()[:40]


def leer_filas(
    nombre_de_hoja: str,
    columnas: ColumnasDeCapital,
    filas: Sequence[Mapping[str, Any]],
) -> tuple[list[MovimientoDeCapital], list[FilaRechazada]]:
    """Cada fila a un movimiento de capital, o rechazada con su motivo.

    La fecha se interpreta con el convenio de su COLUMNA (resuelto una vez) y una
    fecha ilegible o futura nunca se reemplaza por hoy; el medio y el sentido se
    reconocen o la fila no se carga. Orden estable: el índice de la fila entra en la
    huella, así que dos filas idénticas del mismo archivo son dos movimientos.
    """
    sentido_hoja = sentido_por_nombre_de_hoja(nombre_de_hoja)
    movimientos: list[MovimientoDeCapital] = []
    rechazadas: list[FilaRechazada] = []
    for orden, fila in enumerate(filas):
        if all(v in (None, "") for k, v in fila.items() if not str(k).startswith("__")):
            continue
        dia = None
        if columnas.fecha is not None:
            f = parsear_fecha_de_columna(fila.get(columnas.fecha))
            dia = f.valor.date() if f.valor is not None else None
        if dia is None:
            rechazadas.append(FilaRechazada(fila, MOTIVO_SIN_FECHA))
            continue
        monto = monto_positivo(parsear_monto(fila.get(columnas.monto)))
        if monto is None:
            rechazadas.append(FilaRechazada(fila, MOTIVO_SIN_MONTO))
            continue
        medio = medio_de_pago(fila.get(columnas.medio)) if columnas.medio else None
        if medio is None:
            rechazadas.append(FilaRechazada(fila, MOTIVO_SIN_MEDIO))
            continue
        concepto = str(fila.get(columnas.concepto) or "").strip() if columnas.concepto else ""
        kind = None
        if columnas.tipo is not None:
            kind = _sentido_de(str(fila.get(columnas.tipo) or ""))
        if kind is None and columnas.tipo is None:
            kind = sentido_hoja
        if kind is None:
            rechazadas.append(FilaRechazada(fila, MOTIVO_SIN_SENTIDO))
            continue
        socio = str(fila.get(columnas.socio) or "").strip()
        movimientos.append(
            MovimientoDeCapital(
                kind=kind,
                amount=monto,
                occurred_on=dia,
                payment_method=medio,
                partner_name=socio[:120] or None,
                notes=concepto[:1000] or None,
                source_ref=_huella(
                    nombre_de_hoja, orden, kind, monto, dia, socio, medio, concepto
                ),
            )
        )
    return movimientos, rechazadas
