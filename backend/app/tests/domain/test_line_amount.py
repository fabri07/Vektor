"""F-H4 — la tabla de precio unitario × cantidad, fila por fila.

Las siete combinaciones del plan (`docs/plans/ingestion-mapping-overhaul.md`,
§F-H4) más los bordes que deciden si una fila se calcula o no: la tolerancia de un
centavo, el redondeo y todo lo que NO habilita el cálculo. La última fila de la
tabla ("cantidad sin producto → no toca stock") no es de esta función: se verifica
en el importador, que es quien mueve inventario.
"""

from decimal import Decimal

import pytest

from app.domain.line_amount import (
    CENTAVO,
    TOLERANCIA,
    LineAmount,
    resolve_line_amount,
)


class TestTablaDePrecio:
    """Las filas del plan, en el mismo orden."""

    def test_unitario_y_cantidad_calculan_el_monto(self) -> None:
        r = resolve_line_amount(amount=None, unit_price=Decimal("150.50"), quantity=3)
        assert r == LineAmount(Decimal("451.50"), "calculated", None)
        assert not r.discrepa

    def test_monto_coincidente_se_importa_tal_cual(self) -> None:
        r = resolve_line_amount(
            amount=Decimal("451.50"), unit_price=Decimal("150.50"), quantity=3
        )
        assert r.amount == Decimal("451.50")
        assert r.source == "file"
        assert not r.discrepa

    def test_monto_distinto_usa_el_calculo_y_conserva_el_original(self) -> None:
        r = resolve_line_amount(
            amount=Decimal("400.00"), unit_price=Decimal("150.50"), quantity=3
        )
        assert r.amount == Decimal("451.50")
        assert r.source == "recalculated"
        assert r.original == Decimal("400.00")
        assert r.discrepa

    def test_solo_monto_se_importa_sin_tocar_nada(self) -> None:
        r = resolve_line_amount(amount=Decimal("980"), unit_price=None, quantity=None)
        assert r == LineAmount(Decimal("980"), "file", None)

    def test_monto_y_cantidad_no_derivan_el_unitario(self) -> None:
        """F10: el precio unitario JAMÁS sale de monto/cantidad."""
        r = resolve_line_amount(amount=Decimal("900"), unit_price=None, quantity=3)
        # El monto entra como vino y la función no devuelve ningún unitario:
        # `LineAmount` ni siquiera tiene dónde ponerlo, a propósito.
        assert r == LineAmount(Decimal("900"), "file", None)
        assert not hasattr(r, "unit_price")

    def test_solo_unitario_no_inventa_cantidad_ni_monto(self) -> None:
        r = resolve_line_amount(amount=None, unit_price=Decimal("150.50"), quantity=None)
        assert r == LineAmount(None, None, None)


class TestTolerancia:
    """Un centavo: `<= 0.01` es el mismo número, `> 0.01` es una discrepancia."""

    def test_un_centavo_de_diferencia_coincide(self) -> None:
        # 3 × 33.33 = 99.99; el archivo dice 100.00.
        r = resolve_line_amount(
            amount=Decimal("100.00"), unit_price=Decimal("33.33"), quantity=3
        )
        assert r.source == "file"
        assert r.amount == Decimal("100.00")

    def test_dos_centavos_de_diferencia_discrepan(self) -> None:
        r = resolve_line_amount(
            amount=Decimal("100.01"), unit_price=Decimal("33.33"), quantity=3
        )
        assert r.source == "recalculated"
        assert r.amount == Decimal("99.99")
        assert r.original == Decimal("100.01")

    def test_el_borde_es_inclusivo_en_los_dos_sentidos(self) -> None:
        calculado = Decimal("50.00")
        for delta in (TOLERANCIA, -TOLERANCIA):
            r = resolve_line_amount(
                amount=calculado + delta, unit_price=Decimal("25.00"), quantity=2
            )
            assert r.source == "file", f"delta {delta} debería coincidir"

    def test_la_tolerancia_es_un_parametro_explicito(self) -> None:
        assert Decimal("0.01") == TOLERANCIA
        r = resolve_line_amount(
            amount=Decimal("100.00"),
            unit_price=Decimal("33.33"),
            quantity=3,
            tolerancia=Decimal("0"),
        )
        assert r.source == "recalculated"


class TestRedondeo:
    def test_redondea_a_dos_decimales_half_up(self) -> None:
        # 3 × 0.335 = 1.005 → 1.01 con ROUND_HALF_UP. Con el redondeo bancario que
        # trae Python por default daría 1.00, que no es lo que muestra ninguna
        # planilla.
        r = resolve_line_amount(amount=None, unit_price=Decimal("0.335"), quantity=3)
        assert r.amount == Decimal("1.01")

    def test_el_calculo_siempre_tiene_dos_decimales(self) -> None:
        r = resolve_line_amount(amount=None, unit_price=Decimal("10"), quantity=2)
        assert r.amount is not None
        assert r.amount == Decimal("20.00")
        assert r.amount.as_tuple().exponent == CENTAVO.as_tuple().exponent


class TestLoQueNoHabilitaElCalculo:
    """Nada de esto genera un monto: son celdas vacías disfrazadas."""

    @pytest.mark.parametrize("precio", [None, Decimal("0"), Decimal("-10.50")])
    def test_precio_ausente_cero_o_negativo_no_genera_monto(
        self, precio: Decimal | None
    ) -> None:
        r = resolve_line_amount(amount=None, unit_price=precio, quantity=4)
        assert r == LineAmount(None, None, None)

    @pytest.mark.parametrize("cantidad", [None, 0, -3])
    def test_cantidad_ausente_cero_o_negativa_no_genera_monto(
        self, cantidad: int | None
    ) -> None:
        r = resolve_line_amount(
            amount=None, unit_price=Decimal("150.50"), quantity=cantidad
        )
        assert r == LineAmount(None, None, None)

    @pytest.mark.parametrize("cantidad", [None, 0, -3])
    def test_una_cantidad_invalida_no_pisa_el_monto_del_archivo(
        self, cantidad: int | None
    ) -> None:
        """Con monto válido la fila entra igual: la pareja incompleta no la rompe."""
        r = resolve_line_amount(
            amount=Decimal("980"), unit_price=Decimal("150.50"), quantity=cantidad
        )
        assert r == LineAmount(Decimal("980"), "file", None)

    @pytest.mark.parametrize("monto", [Decimal("0"), Decimal("-25")])
    def test_monto_no_positivo_se_trata_como_ausente(self, monto: Decimal) -> None:
        # Sin pareja no hay nada que guardar…
        assert resolve_line_amount(
            amount=monto, unit_price=None, quantity=None
        ) == LineAmount(None, None, None)
        # …y con pareja el cálculo no cuenta como discrepancia contra un "monto"
        # que en realidad era una celda vacía.
        r = resolve_line_amount(amount=monto, unit_price=Decimal("10"), quantity=2)
        assert r == LineAmount(Decimal("20.00"), "calculated", None)

    def test_sin_ningun_dato_no_hay_monto_ni_origen(self) -> None:
        r = resolve_line_amount(amount=None, unit_price=None, quantity=None)
        assert r == LineAmount(None, None, None)
        assert r.source is None
