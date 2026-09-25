"""Compuertas de la presentación de unidades base (B3).

El riesgo que cubren: `stock_units = 5000` para 5 kg es correcto en la base y
catastrófico en una pantalla si alguien lo lee como "5000 kilos".
"""

from decimal import Decimal

import pytest

from app.domain.sale_unit import etiqueta_de_unidad, formatear_cantidad, valor_de_stock


class TestEtiqueta:
    @pytest.mark.parametrize(
        ("unidad", "factor", "esperado"),
        [
            ("unit", 1, "un."),
            ("gram", 1, "g"),
            ("gram", 1000, "kg"),
            ("milliliter", 1, "ml"),
            ("milliliter", 1000, "L"),
        ],
    )
    def test_las_combinaciones_conocidas(self, unidad: str, factor: int, esperado: str) -> None:
        assert etiqueta_de_unidad(unidad, factor) == esperado

    def test_un_factor_sin_nombre_cae_a_la_unidad_base(self) -> None:
        """"Medio kilo" no es una unidad: no se le inventa un nombre."""
        assert etiqueta_de_unidad("gram", 500) == "g"


class TestFormato:
    def test_setecientos_cincuenta_gramos_son_tres_cuartos_de_kilo(self) -> None:
        assert formatear_cantidad(750, "gram", 1000) == "0,750 kg"

    def test_cinco_kilos(self) -> None:
        assert formatear_cantidad(5000, "gram", 1000) == "5,000 kg"

    def test_unidades_sueltas_no_llevan_decimales(self) -> None:
        assert formatear_cantidad(12, "unit", 1) == "12 un."

    def test_separador_de_miles_argentino(self) -> None:
        # 1.234.567 g = 1.234,567 kg
        assert formatear_cantidad(1234567, "gram", 1000) == "1.234,567 kg"

    def test_muchas_unidades_llevan_punto_de_miles(self) -> None:
        assert formatear_cantidad(12500, "unit", 1) == "12.500 un."

    def test_litros(self) -> None:
        assert formatear_cantidad(1500, "milliliter", 1000) == "1,500 L"

    def test_un_factor_sin_nombre_se_muestra_en_unidad_base(self) -> None:
        assert formatear_cantidad(750, "gram", 500) == "750 g"

    def test_stock_en_cero(self) -> None:
        assert formatear_cantidad(0, "gram", 1000) == "0,000 kg"


class TestValorDeStock:
    def test_cinco_kilos_a_800_el_kilo(self) -> None:
        assert valor_de_stock(5000, 1000, Decimal("800.00")) == Decimal("4000.00")

    def test_por_unidad_es_la_cuenta_de_siempre(self) -> None:
        assert valor_de_stock(12, 1, Decimal("150.00")) == Decimal("1800.00")
