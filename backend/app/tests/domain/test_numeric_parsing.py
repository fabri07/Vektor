"""El corpus que fija la política numérica (F2/E4).

Cada caso está acá porque alguno de los parsers viejos lo resolvía distinto. Las
tres semánticas que había —verificadas antes de escribir el módulo— fallaban en
lugares distintos, así que ninguna servía de referencia y la política se decidió
contra estos casos, no copiándole a un parser.

Lo que se afirma no es "el parser anda", sino **qué significa cada forma**: si un
caso de acá cambia, cambió una decisión de negocio sobre plata.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.numeric_parsing import (
    MOTIVO_AMBIGUO,
    MOTIVO_FRACCIONARIA,
    MOTIVO_ILEGIBLE,
    MOTIVO_NEGATIVA,
    MOTIVO_NO_FINITO,
    ConvenioNumerico,
    inferir_convenio,
    parsear_cantidad,
    parsear_monto,
)

_AR = ConvenioNumerico(decimal=",", origen="inequivoco")
_US = ConvenioNumerico(decimal=".", origen="inequivoco")


class TestUnValorQueSeExplicaSolo:
    """Con los dos separadores, el último es el decimal. No hace falta contexto."""

    @pytest.mark.parametrize(
        ("bruto", "esperado"),
        [
            ("1.234,56", "1234.56"),
            ("12.500,00", "12500.00"),
            ("1.234.567,89", "1234567.89"),
            # Formato US: `normalize_numeric` y `parse_money` lo leían como 12,5
            # porque asumían AR sin mirar cuál separador viene último.
            ("12,500.00", "12500.00"),
            ("1,234.56", "1234.56"),
        ],
    )
    def test_el_ultimo_separador_manda(self, bruto: str, esperado: str) -> None:
        assert parsear_monto(bruto).valor == Decimal(esperado)

    def test_no_necesita_convenio_ni_lo_deja_pisar(self) -> None:
        """Un valor que se explica solo gana sobre el convenio de la columna: es
        evidencia directa contra una inferencia."""
        assert parsear_monto("12,500.00", _AR).valor == Decimal("12500.00")
        assert parsear_monto("12.500,00", _US).valor == Decimal("12500.00")


class TestUnValorAmbiguoNecesitaSuColumna:
    """`12.500` es $12.500 o $12,50 — y ninguna regla local lo resuelve.

    `_parse_amount` no tenía rama para este caso, así que caía a `Decimal("12.500")`
    y devolvía **12,5**: una planilla argentina con montos sin centavos se
    importaba dividida por mil.
    """

    def test_sin_convenio_no_se_adivina(self) -> None:
        resultado = parsear_monto("12.500")
        assert resultado.valor is None
        assert resultado.motivo == MOTIVO_AMBIGUO
        assert resultado.original == "12.500", "el original se conserva para revisarlo"

    def test_con_convenio_ar_son_doce_mil_quinientos(self) -> None:
        assert parsear_monto("12.500", _AR).valor == Decimal("12500")

    def test_con_convenio_us_son_doce_con_cincuenta(self) -> None:
        assert parsear_monto("12.500", _US).valor == Decimal("12.500")

    def test_la_coma_sola_es_igual_de_ambigua(self) -> None:
        """No es simétrico por casualidad: en formato US `1,234` son miles.
        Tratar la coma como decimal "porque estamos en Argentina" es la misma
        clase de suposición que rompía el punto."""
        assert parsear_monto("1,234").motivo == MOTIVO_AMBIGUO
        assert parsear_monto("1,234", _AR).valor == Decimal("1.234")
        assert parsear_monto("1,234", _US).valor == Decimal("1234")

    def test_sin_separadores_no_hay_nada_que_decidir(self) -> None:
        assert parsear_monto("1500").valor == Decimal("1500")
        assert parsear_monto("0").valor == Decimal("0")
        assert parsear_monto("-350").valor == Decimal("-350")


class TestUnNumeroNativoNoSeReinterpreta:
    """openpyxl y JSON devuelven float/int para las celdas numéricas.

    `validation_gate` borraba todos los puntos antes de parsear, así que un
    `12.5` nativo se volvía **125** y un `1234.56` **123456** — y como es el gate
    que vigila los montos, medía sobre un valor distinto del que se persistía.
    """

    @pytest.mark.parametrize("bruto", [12.5, 1234.56, 0.1, 12500, 0, -350])
    def test_pasa_tal_cual(self, bruto: float | int) -> None:
        assert parsear_monto(bruto).valor == Decimal(str(bruto))

    def test_el_convenio_no_lo_toca(self) -> None:
        """No tiene formato que interpretar: el convenio es de los strings."""
        assert parsear_monto(12.5, _AR).valor == Decimal("12.5")

    def test_sin_error_binario(self) -> None:
        """`Decimal(0.1)` son 55 dígitos; `Decimal(str(0.1))` es 0.1."""
        assert parsear_monto(0.1).valor == Decimal("0.1")

    @pytest.mark.parametrize("bruto", [float("nan"), float("inf"), float("-inf")])
    def test_no_finito_se_rechaza_con_motivo(self, bruto: float) -> None:
        resultado = parsear_monto(bruto)
        assert resultado.valor is None
        assert resultado.motivo == MOTIVO_NO_FINITO

    def test_un_booleano_no_es_un_numero(self) -> None:
        """`bool` es subclase de `int`: sin el guard, un True sería la cantidad 1."""
        assert parsear_monto(True).motivo == MOTIVO_ILEGIBLE


class TestVacioNoEsIlegible:
    """Una celda en blanco no es un problema a revisar; una celda con basura sí."""

    @pytest.mark.parametrize("bruto", [None, "", "   ", "-", "N/A", "n/a", "nan", "s/d"])
    def test_ausente_no_genera_motivo(self, bruto: object) -> None:
        resultado = parsear_monto(bruto)
        assert resultado.valor is None
        assert resultado.motivo is None
        assert resultado.ausente is True

    @pytest.mark.parametrize("bruto", ["abc", "1.2.a", "$$", "12-34"])
    def test_ilegible_si_genera_motivo(self, bruto: str) -> None:
        resultado = parsear_monto(bruto)
        assert resultado.valor is None
        assert resultado.motivo == MOTIVO_ILEGIBLE
        assert resultado.ausente is False

    def test_el_ruido_de_moneda_no_estorba(self) -> None:
        assert parsear_monto("$ 1.234,56").valor == Decimal("1234.56")
        assert parsear_monto("$12500").valor == Decimal("12500")


class TestElConvenioSaleDeLaColumna:
    def test_un_valor_inequivoco_fija_toda_la_columna(self) -> None:
        """Basta uno: si en algún lado aparece `12.500,50`, el punto de esa
        columna es de miles y `12.500` son doce mil quinientos."""
        convenio = inferir_convenio(["1.500", "12.500,50", "350.000"])
        assert convenio is not None
        assert convenio.decimal == "," and convenio.origen == "inequivoco"
        assert parsear_monto("1.500", convenio).valor == Decimal("1500")

    def test_sin_inequivocos_desempata_la_forma(self) -> None:
        """Un grupo final de 3 dígitos es de miles; uno de 1-2, decimal. La
        moneda argentina usa 2 decimales, no 3."""
        miles = inferir_convenio(["1.500", "12.500", "350.000"])
        assert miles is not None
        assert miles.decimal == "," and miles.origen == "forma"

        decimales = inferir_convenio(["12.50", "8.90", "125.00"])
        assert decimales is not None
        assert decimales.decimal == "."

    def test_varios_separadores_iguales_son_miles(self) -> None:
        """`1.234.567` no puede ser un decimal. `_parse_amount` lo mandaba a
        `InvalidOperation` y perdía el monto entero."""
        convenio = inferir_convenio(["1.234.567"])
        assert convenio is not None and convenio.decimal == ","
        assert parsear_monto("1.234.567", convenio).valor == Decimal("1234567")

    def test_una_columna_contradictoria_no_decide(self) -> None:
        """`12.500` junto a `12.50`: elegir uno rompe la mitad de las filas."""
        assert inferir_convenio(["12.500", "12.50"]) is None

    def test_dos_inequivocos_opuestos_tampoco(self) -> None:
        assert inferir_convenio(["1.234,56", "1,234.56"]) is None

    def test_una_columna_sin_separadores_no_necesita_convenio(self) -> None:
        assert inferir_convenio(["1500", "350", ""]) is None
        assert parsear_monto("1500").valor == Decimal("1500")

    def test_los_nativos_no_aportan_convenio(self) -> None:
        """Un float no tiene formato: no dice nada sobre cómo leer los strings."""
        assert inferir_convenio([12.5, 1234.56]) is None


class TestCantidadesQueNoSeTruncan:
    """`int(float("1.500"))` daba 1: mil quinientas unidades entraban como una.

    Y una coma, un negativo o un texto daban 0 — un número válido, indistinguible
    de "el archivo dijo cero".
    """

    def test_mil_quinientas_unidades_no_son_una(self) -> None:
        assert parsear_cantidad("1.500", _AR).valor == Decimal("1500")

    def test_una_fraccion_va_a_revision_no_se_trunca(self) -> None:
        resultado = parsear_cantidad("2,5", _AR)
        assert resultado.valor is None
        assert resultado.motivo == MOTIVO_FRACCIONARIA
        assert resultado.original == "2,5"

    def test_una_negativa_va_a_revision(self) -> None:
        resultado = parsear_cantidad("-3")
        assert resultado.valor is None
        assert resultado.motivo == MOTIVO_NEGATIVA

    def test_el_cero_es_un_dato_valido(self) -> None:
        resultado = parsear_cantidad("0")
        assert resultado.valor == Decimal("0")
        assert resultado.motivo is None

    def test_vacio_sigue_siendo_ausente(self) -> None:
        assert parsear_cantidad("").ausente is True

    def test_ilegible_no_es_cero(self) -> None:
        resultado = parsear_cantidad("dos")
        assert resultado.valor is None
        assert resultado.motivo == MOTIVO_ILEGIBLE
