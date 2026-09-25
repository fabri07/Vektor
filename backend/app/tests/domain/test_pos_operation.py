"""Compuertas del cálculo de una operación de caja (B3).

El invariante que atraviesa todo el archivo: **la suma de las líneas es
exactamente lo que se cobra**. Un centavo de diferencia por ticket es plata que
no cuadra en el arqueo y que nadie puede explicar después.
"""

from decimal import Decimal
from uuid import uuid4

import pytest

from app.domain.pos_operation import (
    DescuentoExcedeElTotalError,
    LineaPedida,
    OperacionVaciaError,
    TendersNoCuadranError,
    VueltoNegativoError,
    calcular_operacion,
    calcular_vuelto,
    importe_bruto_en_centavos,
    validar_tenders,
)


def _linea(precio: str, cantidad: int = 1, descuento: str = "0") -> LineaPedida:
    return LineaPedida(
        product_id=uuid4(),
        quantity=cantidad,
        unit_price_list=Decimal(precio),
        discount_ars=Decimal(descuento),
    )


class TestElCasoQueNoCierraConPrecioUnitario:
    """$1 de descuento sobre tres unidades de $100.

    Es el caso que obligó a mover el descuento del unitario al total de línea:
    $99,6666… no existe con dos decimales.
    """

    def test_el_total_es_exacto(self) -> None:
        op = calcular_operacion([_linea("100.00", 3)], Decimal("1.00"))
        assert op.total == Decimal("299.00")

    def test_tres_lineas_de_cien_con_un_peso_de_descuento(self) -> None:
        op = calcular_operacion(
            [_linea("100.00"), _linea("100.00"), _linea("100.00")], Decimal("1.00")
        )
        assert op.subtotal == Decimal("300.00")
        assert op.total == Decimal("299.00")
        # 34 + 33 + 33 centavos: el residuo entero va a una sola línea, y con
        # importes empatados es la primera del carrito.
        assert [ln.discount_global_share_ars for ln in op.lineas] == [
            Decimal("0.34"),
            Decimal("0.33"),
            Decimal("0.33"),
        ]

    def test_la_suma_de_las_lineas_es_el_total(self) -> None:
        op = calcular_operacion(
            [_linea("100.00"), _linea("100.00"), _linea("100.00")], Decimal("1.00")
        )
        assert sum(ln.line_total for ln in op.lineas) == op.total

    def test_ningun_unitario_de_dos_decimales_lo_habria_logrado(self) -> None:
        """Demuestra por qué el descuento NO puede vivir en `unit_price`."""
        for candidato in (Decimal("99.66"), Decimal("99.67")):
            assert candidato * 3 != Decimal("299.00")


class TestRepartoDelDescuentoGlobal:
    def test_sin_residuo_reparte_proporcional(self) -> None:
        op = calcular_operacion([_linea("100.00"), _linea("300.00")], Decimal("4.00"))
        assert [ln.discount_global_share_ars for ln in op.lineas] == [
            Decimal("1.00"),
            Decimal("3.00"),
        ]
        assert sum(ln.line_total for ln in op.lineas) == op.total

    def test_el_residuo_va_a_la_linea_de_mayor_importe(self) -> None:
        op = calcular_operacion(
            [_linea("10.00"), _linea("1000.00"), _linea("10.00")], Decimal("0.07")
        )
        partes = [ln.discount_global_share_ars for ln in op.lineas]
        assert partes[1] == max(partes)
        assert sum(partes) == Decimal("0.07")

    def test_con_empate_gana_la_primera_del_carrito(self) -> None:
        """El resultado no puede depender del orden en que la DB devolvió filas."""
        op = calcular_operacion([_linea("50.00"), _linea("50.00")], Decimal("0.01"))
        assert [ln.discount_global_share_ars for ln in op.lineas] == [
            Decimal("0.01"),
            Decimal("0.00"),
        ]

    def test_el_descuento_de_linea_y_el_global_conviven(self) -> None:
        op = calcular_operacion([_linea("100.00", 1, "10.00"), _linea("100.00")], Decimal("9.00"))
        assert op.subtotal == Decimal("190.00")
        assert op.total == Decimal("181.00")
        primera = op.lineas[0]
        assert primera.discount_ars == Decimal("10.00") + primera.discount_global_share_ars
        assert sum(ln.line_total for ln in op.lineas) == op.total

    def test_un_descuento_que_deja_la_venta_en_cero_es_valido(self) -> None:
        op = calcular_operacion([_linea("100.00")], Decimal("100.00"))
        assert op.total == Decimal("0.00")
        assert sum(ln.line_total for ln in op.lineas) == Decimal("0.00")

    @pytest.mark.parametrize("cantidad_lineas", [2, 3, 5, 7, 11])
    @pytest.mark.parametrize("descuento", ["0.01", "0.07", "1.00", "33.33", "99.99"])
    def test_el_cierre_exacto_no_depende_del_reparto(
        self, cantidad_lineas: int, descuento: str
    ) -> None:
        """El invariante, barrido: importes primos entre sí para forzar residuo."""
        precios = ["101.03", "7.11", "1999.97", "0.03", "58.29", "3.33", "12345.67"]
        lineas = [
            _linea(precios[i % len(precios)], cantidad=(i % 4) + 1)
            for i in range(cantidad_lineas)
        ]
        op = calcular_operacion(lineas, Decimal(descuento))
        assert sum(ln.line_total for ln in op.lineas) == op.total
        assert op.total == op.subtotal - op.discount_global_ars


class TestNingunaLineaNegativa:
    """El residuo entero a UNA línea podía pasarla de su propio importe."""

    def test_el_caso_del_review(self) -> None:
        op = calcular_operacion(
            [_linea("34.00"), _linea("33.00"), _linea("33.00")], Decimal("99.99")
        )
        assert all(ln.line_total >= 0 for ln in op.lineas)
        assert sum(ln.line_total for ln in op.lineas) == op.total == Decimal("0.01")

    @pytest.mark.parametrize("cantidad", [2, 3, 4, 7])
    def test_descuentos_casi_totales_nunca_dejan_una_linea_negativa(self, cantidad: int) -> None:
        """Barrido: cada centavo de descuento desde 0 hasta el subtotal entero."""
        precios = ["34.00", "33.00", "33.00", "0.07", "1.01", "12.34", "0.50"]
        lineas = [_linea(precios[i]) for i in range(cantidad)]
        subtotal = sum(Decimal(precios[i]) for i in range(cantidad))
        centavos = int(subtotal * 100)
        for d in list(range(0, 50)) + list(range(max(0, centavos - 400), centavos + 1)):
            op = calcular_operacion(lineas, Decimal(d) / 100)
            assert all(ln.line_total >= 0 for ln in op.lineas), d
            assert sum(ln.line_total for ln in op.lineas) == op.total


class TestRechazos:
    def test_carrito_vacio(self) -> None:
        with pytest.raises(OperacionVaciaError):
            calcular_operacion([], Decimal("0"))

    def test_descuento_global_mayor_que_la_venta(self) -> None:
        with pytest.raises(DescuentoExcedeElTotalError):
            calcular_operacion([_linea("100.00")], Decimal("100.01"))

    def test_descuento_de_linea_mayor_que_la_linea(self) -> None:
        with pytest.raises(DescuentoExcedeElTotalError):
            calcular_operacion([_linea("100.00", 1, "100.01")])

    def test_descuento_global_sobre_un_carrito_que_ya_vale_cero(self) -> None:
        """Las líneas quedaron en cero por descuentos de línea.

        No necesita un rechazo propio: cualquier descuento global supera a cero,
        y decir eso es exacto.
        """
        with pytest.raises(DescuentoExcedeElTotalError):
            calcular_operacion([_linea("10.00", 1, "10.00")], Decimal("1.00"))

    def test_cantidad_cero(self) -> None:
        with pytest.raises(ValueError):
            calcular_operacion([_linea("100.00", 0)])


class TestUnidadesBase:
    """Contrato: el precio es por UNIDAD DE VENTA; la cantidad, en unidades base."""

    def _gramos(self, precio_kg: str, gramos: int) -> LineaPedida:
        return LineaPedida(
            product_id=uuid4(),
            quantity=gramos,
            unit_price_list=Decimal(precio_kg),
            base_units_per_sale_unit=1000,
        )

    def test_setecientos_cincuenta_gramos_de_yerba_a_3200_el_kilo(self) -> None:
        op = calcular_operacion([self._gramos("3200.00", 750)])
        assert op.total == Decimal("2400.00")

    def test_un_precio_por_kilo_que_no_entra_en_dos_decimales_por_gramo(self) -> None:
        """$1.234,56/kg son $1,23456/g: se redondea el IMPORTE de la línea, una vez."""
        op = calcular_operacion([self._gramos("1234.56", 750)])
        assert op.total == Decimal("925.92")

    def test_sin_el_factor_seria_mil_veces_mas(self) -> None:
        sin_factor = calcular_operacion([_linea("1000.00", 750)])
        con_factor = calcular_operacion([self._gramos("1000.00", 750)])
        assert sin_factor.total == con_factor.total * 1000

    def test_la_cantidad_en_unidades_base_no_se_convierte(self) -> None:
        op = calcular_operacion([self._gramos("3200.00", 750)])
        assert op.lineas[0].quantity == 750

    def test_redondeo_half_up_por_linea(self) -> None:
        # $0,01 el kilo × 500 g = $0,005 → $0,01.
        assert importe_bruto_en_centavos(Decimal("0.01"), 500, 1000) == 1
        assert importe_bruto_en_centavos(Decimal("0.01"), 499, 1000) == 0


class TestTenders:
    def test_pago_simple_cuadra(self) -> None:
        validar_tenders(Decimal("299.00"), [Decimal("299.00")])

    def test_pago_mixto_cuadra(self) -> None:
        validar_tenders(Decimal("6499.00"), [Decimal("5000.00"), Decimal("1499.00")])

    def test_un_centavo_de_menos_se_rechaza(self) -> None:
        with pytest.raises(TendersNoCuadranError):
            validar_tenders(Decimal("6499.00"), [Decimal("5000.00"), Decimal("1498.99")])

    def test_sin_pagos(self) -> None:
        with pytest.raises(TendersNoCuadranError):
            validar_tenders(Decimal("100.00"), [])

    def test_pago_en_cero(self) -> None:
        with pytest.raises(TendersNoCuadranError):
            validar_tenders(Decimal("100.00"), [Decimal("100.00"), Decimal("0.00")])


class TestVuelto:
    def test_el_vuelto_no_es_la_venta(self) -> None:
        assert calcular_vuelto(Decimal("10000.00"), Decimal("8500.00")) == Decimal("1500.00")

    def test_sin_efectivo_entregado_no_hay_vuelto(self) -> None:
        assert calcular_vuelto(None, Decimal("0.00")) == Decimal("0.00")

    def test_justo_no_da_vuelto(self) -> None:
        assert calcular_vuelto(Decimal("8500.00"), Decimal("8500.00")) == Decimal("0.00")

    def test_entregar_menos_que_la_parte_en_efectivo_se_rechaza(self) -> None:
        with pytest.raises(VueltoNegativoError):
            calcular_vuelto(Decimal("8000.00"), Decimal("8500.00"))
