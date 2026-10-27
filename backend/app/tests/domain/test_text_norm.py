"""Tests de los normalizadores canónicos de identidad de producto (Fase 2, T1).

Cubre los 4 helpers por campo agregados a ``text_norm.py``:
``normalize_barcode``, ``normalize_sku``, ``normalize_product_name``,
``normalize_brand``. Cada uno delega en ``normalize_text`` salvo
``normalize_barcode`` (solo dígitos).
"""

import pytest

from app.domain.text_norm import (
    normalize_barcode,
    normalize_brand,
    normalize_product_name,
    normalize_sku,
)


class TestNormalizeBarcode:
    def test_strips_non_digits(self) -> None:
        assert normalize_barcode("779-089 500") == "779089500"

    def test_keeps_only_digits_with_letters(self) -> None:
        assert normalize_barcode("EAN:7790895000123") == "7790895000123"

    @pytest.mark.parametrize("raw", [None, "", "   ", "abc-def"])
    def test_none_or_no_digits_returns_none(self, raw: str | None) -> None:
        assert normalize_barcode(raw) is None

    @pytest.mark.parametrize(
        "raw",
        [
            "producto-123",  # 3 dígitos embebidos: no es barcode
            "ABC123",  # 3 dígitos
            "1234567",  # 7 dígitos: por debajo del mínimo (8)
            "123456789012345",  # 15 dígitos: por encima del máximo (14)
        ],
    )
    def test_implausible_length_returns_none(self, raw: str) -> None:
        # Gate de longitud (8–14): evita colisiones falsas por dígitos embebidos.
        assert normalize_barcode(raw) is None

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("12345678", "12345678"),  # EAN-8
            ("012345678905", "012345678905"),  # UPC-A (12)
            ("7790895000123", "7790895000123"),  # EAN-13
            ("07790895000123", "07790895000123"),  # GTIN-14
        ],
    )
    def test_plausible_barcode_lengths_kept(self, raw: str, expected: str) -> None:
        assert normalize_barcode(raw) == expected


class TestNormalizeSku:
    def test_casefold_and_accents(self) -> None:
        assert normalize_sku("CC 500") == "cc 500"
        assert normalize_sku("Café-500") == "cafe-500"

    @pytest.mark.parametrize("raw", [None, ""])
    def test_none_or_empty_returns_none(self, raw: str | None) -> None:
        assert normalize_sku(raw) is None


class TestNormalizeProductName:
    def test_dash_underscore_collapse_to_same_key(self) -> None:
        assert (
            normalize_product_name("Coca-Cola")
            == normalize_product_name("Coca Cola")
            == normalize_product_name("coca_cola")
            == "coca cola"
        )

    def test_accents_removed(self) -> None:
        assert normalize_product_name("Café") == "cafe"

    def test_empty_or_none_returns_empty_string(self) -> None:
        assert normalize_product_name("") == ""
        assert normalize_product_name(None) == ""


class TestNormalizeBrand:
    def test_casefold_and_accents(self) -> None:
        assert normalize_brand("Café Martínez") == "cafe martinez"

    @pytest.mark.parametrize("raw", [None, ""])
    def test_none_or_empty_returns_none(self, raw: str | None) -> None:
        assert normalize_brand(raw) is None


class TestValoresNoTexto:
    """I5-1: una celda numérica de xlsx puede llegar como número nativo.

    Un código de barras, un SKU, un nombre o una marca cargados como número
    siguen siendo texto: los normalizadores no revientan con un ``int``/``float``,
    un ``float`` entero no arrastra el ``.0`` y un ``0`` es ``"0"``, no vacío.
    """

    def test_barcode_nativo(self) -> None:
        assert normalize_barcode(7790001234567) == "7790001234567"
        assert normalize_barcode(7790001234567.0) == "7790001234567"

    def test_sku_nativo(self) -> None:
        assert normalize_sku(555) == "555"
        assert normalize_sku(555.0) == "555"
        assert normalize_sku(0) == "0"

    def test_nombre_y_marca_nativos(self) -> None:
        assert normalize_product_name(1001) == "1001"
        assert normalize_product_name(0) == "0"
        assert normalize_brand(99) == "99"

    def test_none_sigue_siendo_ausente(self) -> None:
        assert normalize_barcode(None) is None
        assert normalize_sku(None) is None
        assert normalize_product_name(None) == ""
        assert normalize_brand(None) is None
