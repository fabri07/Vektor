"""Compuertas del clasificador de códigos escaneados (B4)."""

import pytest

from app.domain.internal_sku import generate_internal_sku
from app.domain.scan_code import (
    TipoDeCodigo,
    clasificar,
    digito_verificador_gs1,
    gtin_valido,
    variantes_gtin,
)

# EAN-13 argentino real de estructura: 779 + ... con verificador calculado.
EAN13 = "7790040000" + "12" + str(digito_verificador_gs1("779004000012"))
EAN8 = "9638507" + str(digito_verificador_gs1("9638507"))


class TestChecksum:
    def test_ean13_conocido(self) -> None:
        # 4006381333931 es el ejemplo canónico de GS1.
        assert gtin_valido("4006381333931")

    def test_un_digito_cambiado_no_es_gtin(self) -> None:
        assert not gtin_valido("4006381333932")

    def test_ean8_conocido(self) -> None:
        assert gtin_valido("96385074")

    def test_largo_no_gtin(self) -> None:
        assert not gtin_valido("1234567")


class TestClasificar:
    def test_ean13(self) -> None:
        c = clasificar(EAN13)
        assert c.tipo is TipoDeCodigo.GTIN
        assert c.valor == EAN13

    def test_ean13_con_checksum_invalido_es_opaco(self) -> None:
        """Una lectura corrupta no puede coincidir con el GTIN de otro producto."""
        malo = EAN13[:-1] + str((int(EAN13[-1]) + 1) % 10)
        assert clasificar(malo).tipo is TipoDeCodigo.OPAQUE

    def test_ean8(self) -> None:
        assert clasificar(EAN8).tipo is TipoDeCodigo.GTIN

    def test_sufijo_del_lector(self) -> None:
        """El wedge manda CR, LF o Tab al final: no son parte del código."""
        for sufijo in ("\r", "\n", "\r\n", "\t"):
            c = clasificar(EAN13 + sufijo)
            assert c.tipo is TipoDeCodigo.GTIN
            assert c.valor == EAN13

    def test_sku_interno_de_vektor(self) -> None:
        import uuid

        sku = generate_internal_sku(uuid.uuid4())
        c = clasificar(sku)
        assert c.tipo is TipoDeCodigo.INTERNAL_SKU
        assert c.valor == sku

    def test_sku_interno_en_minusculas(self) -> None:
        """Un lector con Bloq Mayús cambiado manda minúsculas."""
        import uuid

        sku = generate_internal_sku(uuid.uuid4())
        assert clasificar(sku.lower()).valor == sku

    def test_prefijo_de_balanza(self) -> None:
        cuerpo = "201234500150"
        codigo = cuerpo + str(digito_verificador_gs1(cuerpo))
        assert clasificar(codigo).tipo is TipoDeCodigo.SCALE

    def test_alfanumerico_con_ocho_digitos_adentro_es_opaco(self) -> None:
        """El caso del punto 8: normalize_barcode lo reduciría a un EAN-8."""
        c = clasificar(f"AB-{EAN8}")
        assert c.tipo is TipoDeCodigo.OPAQUE
        assert c.valor == f"AB-{EAN8}"

    def test_texto_vacio_es_opaco(self) -> None:
        assert clasificar("\r").tipo is TipoDeCodigo.OPAQUE


class TestAprendible:
    @pytest.mark.parametrize(
        ("codigo", "aprendible"),
        [
            (EAN13, True),
            (EAN8, True),
            ("ABC-123", False),
            ("VKT-0123456789AB", False),
        ],
    )
    def test_solo_gtin(self, codigo: str, aprendible: bool) -> None:
        assert clasificar(codigo).aprendible is aprendible

    def test_balanza_no_se_aprende(self) -> None:
        cuerpo = "201234500150"
        assert not clasificar(cuerpo + str(digito_verificador_gs1(cuerpo))).aprendible


class TestVariantes:
    def test_upc_a_y_ean13_son_el_mismo(self) -> None:
        upc = "036000291452"
        assert gtin_valido(upc)
        formas = variantes_gtin(upc)
        assert "0036000291452" in formas
        assert upc in formas

    def test_ean13_incluye_su_gtin14(self) -> None:
        assert "0" + EAN13 in variantes_gtin(EAN13)
