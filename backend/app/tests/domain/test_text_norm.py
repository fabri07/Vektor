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
