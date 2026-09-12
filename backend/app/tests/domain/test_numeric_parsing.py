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
    MOTIVO_INCOMPATIBLE,
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


class TestElDesempateEsOpcionalYExplicito:
    """Dos situaciones que se parecen y no son lo mismo.

    Que la columna sea CONTRADICTORIA (`12.500` junto a `12.50`) no es lo mismo
    que no tener columna que mirar. En el primer caso adivinar rompería la mitad
    de las filas y taparía que el archivo está mal armado; en el segundo la forma
    del valor es la única evidencia que hay, y usarla es mejor que devolver nada.

    El default es el estricto a propósito: un caller que no piensa en esto no
    debería terminar adivinando escalas.
    """

    def test_por_defecto_no_desempata(self) -> None:
        assert parsear_monto("12.500").motivo == MOTIVO_AMBIGUO
        assert parsear_monto("800.00").motivo == MOTIVO_AMBIGUO

    def test_con_desempate_lee_la_forma(self) -> None:
        assert parsear_monto("12.500", desempatar_por_forma=True).valor == Decimal("12500")
        assert parsear_monto("800.00", desempatar_por_forma=True).valor == Decimal("800.00")
        assert parsear_monto("1,234", desempatar_por_forma=True).valor == Decimal("1234")

    def test_el_convenio_le_gana_al_desempate(self) -> None:
        """Si la columna decidió, su convenio manda: es evidencia más fuerte que
        la forma de una celda suelta."""
        assert parsear_monto("12.500", _US, desempatar_por_forma=True).valor == Decimal("12.500")

    def test_lo_que_ni_la_forma_resuelve_sigue_ambiguo(self) -> None:
        """Cuatro dígitos detrás del separador no son ni miles ni centavos."""
        assert parsear_monto("12.5000", desempatar_por_forma=True).motivo == MOTIVO_AMBIGUO

    def test_las_cantidades_tambien_lo_exponen(self) -> None:
        assert parsear_cantidad("1.500").motivo == MOTIVO_AMBIGUO
        assert parsear_cantidad("1.500", desempatar_por_forma=True).valor == Decimal("1500")


class TestElConvenioNoAutorizaABorrarElSeparador:
    """Elegir qué separador es cuál no alcanza: la celda tiene que TENER esa forma.

    Con el convenio decidido, la versión anterior borraba el separador de miles
    sin mirar qué grupos dejaba. Una columna AR (punto = miles) leía `"12.50"`
    como **1250** y `"1.2.3"` como **123**: montos multiplicados por cien o por
    mil, en silencio y sin nada que revisar. El convenio dice cómo LEER, no
    autoriza a reescribir una celda que está en otro formato.
    """

    def test_un_grupo_de_dos_digitos_no_es_de_miles(self) -> None:
        resultado = parsear_monto("12.50", _AR)
        assert resultado.valor is None, "1250 sería cien veces el valor escrito"
        assert resultado.motivo == MOTIVO_INCOMPATIBLE
        assert resultado.original == "12.50"

    def test_separadores_sueltos_no_son_un_numero(self) -> None:
        assert parsear_monto("1.2.3", _AR).motivo == MOTIVO_INCOMPATIBLE
        assert parsear_monto("12.5000", _AR).motivo == MOTIVO_INCOMPATIBLE

    def test_dos_separadores_decimales_tampoco(self) -> None:
        assert parsear_monto("12,50,5", _AR).motivo == MOTIVO_INCOMPATIBLE

    def test_incompatible_no_es_ambiguo(self) -> None:
        """Son dos problemas distintos y la pantalla los explica distinto: en uno
        la columna no pudo decidir; en el otro decidió y esta celda no encaja."""
        assert parsear_monto("12.500").motivo == MOTIVO_AMBIGUO
        assert parsear_monto("12.50", _AR).motivo == MOTIVO_INCOMPATIBLE

    @pytest.mark.parametrize(
        ("bruto", "convenio", "esperado"),
        [
            ("1.234.567", _AR, "1234567"),  # varios grupos de tres: válido
            ("12,500.00", _US, "12500.00"),
            ("12.500", _US, "12.500"),  # tres decimales: legítimo
            ("12.5000", _US, "12.5000"),  # cuatro también: no dice nada de la escala
            (",50", _AR, "0.50"),  # medio peso escrito sin el cero
            ("1500,", _AR, "1500"),  # separador final sin decimales
            ("-1.500", _AR, "-1500"),  # el signo no rompe el grupo
        ],
    )
    def test_lo_que_sigue_siendo_valido(
        self, bruto: str, convenio: ConvenioNumerico, esperado: str
    ) -> None:
        assert parsear_monto(bruto, convenio).valor == Decimal(esperado)

    def test_una_celda_rota_no_le_fija_el_convenio_a_la_columna(self) -> None:
        """`1.2.3` "parecía" decir que el punto es de miles, y con eso decidía por
        todas las demás filas. Una celda inválida no es evidencia de nada."""
        assert inferir_convenio(["1.2.3"]) is None
        assert inferir_convenio(["12.50,5"]) is None

    def test_la_columna_sana_sigue_decidiendo_con_una_celda_rota_al_lado(self) -> None:
        convenio = inferir_convenio(["1.234,56", "1.2.3", "8.900"])
        assert convenio is not None and convenio.decimal == ","
        assert parsear_monto("8.900", convenio).valor == Decimal("8900")
        assert parsear_monto("1.2.3", convenio).motivo == MOTIVO_INCOMPATIBLE

    def test_una_cantidad_incompatible_tampoco_se_interpreta(self) -> None:
        assert parsear_cantidad("1.50", _AR).motivo == MOTIVO_INCOMPATIBLE
