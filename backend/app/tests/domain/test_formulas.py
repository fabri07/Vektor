"""Fase 3 — el evaluador acotado de fórmulas.

Cada caso con su resultado esperado escrito a mano (lo que da Excel), no
calculado por el mismo código que se prueba.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.domain.formulas import Evaluador, Pendiente, analizar


def _hoja(valores: dict[str, Any], formulas: dict[str, str]) -> Evaluador:
    from app.domain.formulas import columna_a_numero

    def coord(c: str) -> tuple[int, int]:
        letras = "".join(ch for ch in c if ch.isalpha())
        return int(c[len(letras) :]), columna_a_numero(letras)

    grilla = {coord(k): v for k, v in valores.items()}
    return Evaluador(
        valor_de=lambda f, c: grilla.get((f, c)),
        formulas={coord(k): v for k, v in formulas.items()},
    )


def _calc(valores: dict[str, Any], formulas: dict[str, str], celda: str = "Z1") -> Any:
    from app.domain.formulas import columna_a_numero

    letras = "".join(ch for ch in celda if ch.isalpha())
    return _hoja(valores, formulas).calcular(int(celda[len(letras) :]), columna_a_numero(letras))


@pytest.mark.parametrize(
    ("formula", "esperado"),
    [
        ("=1+2*3", "7"),
        ("=(1+2)*3", "9"),
        ("=-2^2", "4"),  # en Excel el menos unario liga más fuerte que ^
        ("=2^3^2", "64"),  # ^ asocia a izquierda en Excel
        ("=10/4", "2.5"),
        ("=1/3", "0.333333333333333"),  # 15 dígitos significativos, como Excel
        ("=ROUND(2.5,0)", "3"),
        ("=ROUND(-2.5,0)", "-3"),  # mitad alejándose del cero
        ("=ROUND(1234.5678,2)", "1234.57"),
        ("=ROUND(1234.5678,-2)", "1200"),
        ("=1000*1000", "1000000"),  # nunca "1E+6"
    ],
)
def test_aritmetica(formula: str, esperado: str) -> None:
    resultado = _calc({}, {"Z1": formula})
    assert resultado.motivo is None, resultado.motivo
    assert str(resultado.valor) == esperado


def test_las_formulas_de_asteria() -> None:
    """Las formas reales del archivo: envío prorrateado, compra+envío y la lista
    con un porcentaje en una celda fija (H$2)."""
    valores = {"E4": 6975, "F3": 17300, "H2": 0.8, "I2": 0.4}
    formulas = {
        "F4": "=((E4/169800)*F$3)",
        "G4": "=(E4+F4)",
        "H4": "=G4+(G4*H$2)",
        "I4": "=((G4*I$2)+G4)",
    }
    hoja = _hoja(valores, formulas)
    f4 = hoja.calcular(4, 6).valor
    g4 = hoja.calcular(4, 7).valor
    h4 = hoja.calcular(4, 8).valor
    # Esperados verificados con aritmética exacta (Fraction), a 15 dígitos:
    # 6975 / 169800 * 17300 = 710.6448763250883…
    assert f4 == Decimal("710.644876325088")
    assert g4 == Decimal("7685.64487632509")
    assert h4 == Decimal("13834.1607773852")
    assert hoja.calcular(4, 9).valor == Decimal("10759.9028268551")


def test_sum_con_rango_ignora_texto_y_vacias() -> None:
    resultado = _calc({"A1": 10, "A2": "nota", "A4": 5.5}, {"Z1": "=SUM(A1:A4, 2)"})
    assert resultado.valor == Decimal("17.5")


def test_celda_vacia_en_aritmetica_es_cero_y_se_registra() -> None:
    resultado = _calc({"A1": 10}, {"Z1": "=A1+B1"})
    assert resultado.valor == Decimal("10")
    assert resultado.uso_vacias is True


@pytest.mark.parametrize(
    ("valores", "formula", "en_el_motivo"),
    [
        ({"A1": 0}, "=10/A1", "divide por cero"),
        ({"A1": "hola"}, "=A1*2", "texto"),
        ({"A1": "#N/A"}, "=A1+1", "error de Excel"),
        ({}, "=Hoja2!A1+1", "otra hoja"),
        ({}, "='Precios'!A1", "otra hoja"),
        ({}, "=IF(A1>0,1,2)", "IF"),
        ({}, "=A1&B1", "«&"),
        ({}, "=10%", "«%"),
        ({}, "=2^0.5", "decimales"),
        ({}, "=A1:A3", "SUM"),
        ({"A1": True}, "=A1+1", "VERDADERO"),
    ],
)
def test_lo_que_no_se_calcula_queda_pendiente_con_motivo(
    valores: dict[str, Any], formula: str, en_el_motivo: str
) -> None:
    resultado = _calc(valores, {"Z1": formula})
    assert resultado.valor is None
    assert resultado.motivo is not None and en_el_motivo in resultado.motivo


def test_referencia_circular() -> None:
    hoja = _hoja({}, {"A1": "=B1+1", "B1": "=A1+1"})
    resultado = hoja.calcular(1, 1)
    assert resultado.valor is None
    assert "circular" in (resultado.motivo or "")


def test_una_dependencia_pendiente_arrastra_a_la_que_la_usa() -> None:
    hoja = _hoja({}, {"A1": "=VLOOKUP(1,B:C,2)", "A2": "=A1*2"})
    resultado = hoja.calcular(2, 1)
    assert resultado.valor is None
    assert "A1" in (resultado.motivo or "")


def test_cadena_demasiado_profunda() -> None:
    formulas = {f"A{i}": f"=A{i + 1}+1" for i in range(1, 100)}
    formulas["A100"] = "=1"
    resultado = _hoja({}, formulas).calcular(1, 1)
    assert resultado.valor is None
    assert "encadenadas" in (resultado.motivo or "")


def test_rango_demasiado_grande() -> None:
    resultado = _calc({}, {"Z1": "=SUM(A1:J2000)"})
    assert resultado.valor is None


def test_formula_mal_formada() -> None:
    with pytest.raises(Pendiente):
        analizar("=(1+2")
