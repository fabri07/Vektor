"""Hojas de capital de socios en una importación: se reconocen por la columna de socio
(nunca por una celda), una fila legible es un movimiento y lo que no se puede leer sin
adivinar vuelve a «Otros» con su motivo."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.domain.capital_import import (
    KIND_APORTE,
    KIND_RETIRO,
    MOTIVO_SIN_FECHA,
    MOTIVO_SIN_MEDIO,
    MOTIVO_SIN_MONTO,
    MOTIVO_SIN_SENTIDO,
    detectar_hoja_de_capital,
    leer_filas,
    medio_de_pago,
    sentido_por_nombre_de_hoja,
)

HEADERS = ["Fecha", "Socio", "Concepto", "Forma", "Monto ARS"]
FILA = {
    "Fecha": "2026-10-20",
    "Socio": "marco f.",
    "Concepto": "Reposición de caja",
    "Forma": "Efectivo",
    "Monto ARS": 300000,
}


def test_la_hoja_de_aportes_se_reconoce_por_su_columna_de_socio() -> None:
    cols = detectar_hoja_de_capital(HEADERS)
    assert cols is not None
    assert (cols.socio, cols.monto, cols.fecha, cols.medio, cols.concepto) == (
        "Socio", "Monto ARS", "Fecha", "Forma", "Concepto"
    )


def test_una_hoja_de_gastos_con_concepto_y_forma_no_es_capital() -> None:
    assert detectar_hoja_de_capital(["Fecha", "Concepto", "Forma", "Monto ARS"]) is None
    assert detectar_hoja_de_capital(["Fecha", "Socio", "Concepto"]) is None  # sin monto


def test_sentido_por_nombre_de_hoja_solo_si_no_es_ambiguo() -> None:
    assert sentido_por_nombre_de_hoja("Aportes de socios") == KIND_APORTE
    assert sentido_por_nombre_de_hoja("Retiros") == KIND_RETIRO
    assert sentido_por_nombre_de_hoja("Aportes y retiros") is None
    assert sentido_por_nombre_de_hoja("Movimientos") is None


def test_medio_de_pago_reconocido_o_nada() -> None:
    assert medio_de_pago("Efectivo") == "cash"
    assert medio_de_pago("transferencia") == "transfer"
    assert medio_de_pago("Mercado Pago") == "qr"
    assert medio_de_pago("¿?") is None
    assert medio_de_pago(None) is None


def test_la_fila_del_archivo_de_prueba_es_un_aporte_en_efectivo() -> None:
    cols = detectar_hoja_de_capital(HEADERS)
    assert cols is not None
    movs, rechazadas = leer_filas("Aportes de socios", cols, [FILA])
    assert rechazadas == []
    (m,) = movs
    assert (m.kind, m.amount, m.occurred_on, m.payment_method) == (
        KIND_APORTE, Decimal("300000"), date(2026, 10, 20), "cash"
    )
    assert m.partner_name == "marco f."  # texto libre: no se "corrige" a Marco Fontana
    assert m.notes == "Reposición de caja"


def test_la_misma_fila_da_la_misma_huella_y_filas_iguales_son_dos_movimientos() -> None:
    cols = detectar_hoja_de_capital(HEADERS)
    assert cols is not None
    a, _ = leer_filas("Aportes de socios", cols, [FILA, FILA])
    b, _ = leer_filas("Aportes de socios", cols, [FILA, FILA])
    assert [m.source_ref for m in a] == [m.source_ref for m in b]
    assert a[0].source_ref != a[1].source_ref


def test_lo_que_no_se_puede_leer_se_rechaza_con_su_motivo() -> None:
    cols = detectar_hoja_de_capital(HEADERS)
    assert cols is not None
    casos = [
        ({**FILA, "Fecha": "no es fecha"}, MOTIVO_SIN_FECHA),
        ({**FILA, "Fecha": "2999-01-01"}, MOTIVO_SIN_FECHA),
        ({**FILA, "Monto ARS": 0}, MOTIVO_SIN_MONTO),
        ({**FILA, "Forma": "algo raro"}, MOTIVO_SIN_MEDIO),
    ]
    for fila, motivo in casos:
        movs, rechazadas = leer_filas("Aportes de socios", cols, [fila])
        assert movs == []
        assert [r.motivo for r in rechazadas] == [motivo]


def test_sin_columna_de_tipo_y_nombre_ambiguo_no_adivina_el_sentido() -> None:
    cols = detectar_hoja_de_capital(HEADERS)
    assert cols is not None
    movs, rechazadas = leer_filas("Movimientos de socios", cols, [FILA])
    assert movs == []
    assert [r.motivo for r in rechazadas] == [MOTIVO_SIN_SENTIDO]


def test_la_columna_de_tipo_manda_fila_por_fila() -> None:
    headers = [*HEADERS, "Tipo"]
    cols = detectar_hoja_de_capital(headers)
    assert cols is not None and cols.tipo == "Tipo"
    movs, rechazadas = leer_filas(
        "Movimientos de socios",
        cols,
        [{**FILA, "Tipo": "Aporte"}, {**FILA, "Tipo": "Retiro", "Monto ARS": 50000},
         {**FILA, "Tipo": "???"}],
    )
    assert [m.kind for m in movs] == [KIND_APORTE, KIND_RETIRO]
    assert [r.motivo for r in rechazadas] == [MOTIVO_SIN_SENTIDO]


def test_las_filas_vacias_se_ignoran() -> None:
    cols = detectar_hoja_de_capital(HEADERS)
    assert cols is not None
    vacia = {h: None for h in HEADERS}
    assert leer_filas("Aportes de socios", cols, [vacia]) == ([], [])


def test_una_fila_insertada_antes_no_cambia_la_huella_de_las_demas() -> None:
    """Un archivo corregido (una fila nueva arriba) no vuelve a cargar las que ya estaban."""
    cols = detectar_hoja_de_capital(HEADERS)
    assert cols is not None
    otra = {**FILA, "Monto ARS": 50000, "Fecha": "2026-10-21"}
    antes, _ = leer_filas("Aportes de socios", cols, [FILA])
    despues, _ = leer_filas("Aportes de socios", cols, [otra, FILA])
    assert antes[0].source_ref == despues[1].source_ref


def test_un_libro_de_ventas_con_columna_socio_no_es_capital() -> None:
    assert detectar_hoja_de_capital(["Fecha", "Socio", "Producto", "Total"]) is None
