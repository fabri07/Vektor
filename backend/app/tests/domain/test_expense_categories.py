"""Tests del catálogo canónico de categorías de gasto y su normalizador."""

import re

import pytest

from app.domain.expense_categories import (
    EXPENSE_CATEGORIES_PATTERN,
    EXPENSE_CATEGORY_CODES,
    EXPENSE_CATEGORY_LABELS_ES,
    normalize_expense_category,
)


class TestCatalog:
    def test_every_code_has_label(self) -> None:
        assert set(EXPENSE_CATEGORY_CODES) == set(EXPENSE_CATEGORY_LABELS_ES)

    def test_pattern_accepts_all_codes(self) -> None:
        for code in EXPENSE_CATEGORY_CODES:
            assert re.match(EXPENSE_CATEGORIES_PATTERN, code)

    def test_pattern_rejects_raw_text(self) -> None:
        for bad in ("Bancarios", "compra_proveedor", "importado", "rent", ""):
            assert re.match(EXPENSE_CATEGORIES_PATTERN, bad) is None


class TestNormalizeExactAliases:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # Las 11 categorías del CSV real del usuario
            ("Bancarios", "BANK_FEES"),
            ("Seguros", "INSURANCE"),
            ("Servicios", "UTILITIES"),
            ("Impuestos", "TAXES"),
            ("Mantenimiento", "MAINTENANCE"),
            ("Marketing", "MARKETING"),
            ("Logística", "LOGISTICS"),
            ("Insumos", "SUPPLIES"),
            ("Profesionales", "PROFESSIONAL_SERVICES"),
            ("Sueldos", "PAYROLL"),
            ("Alquiler", "RENT"),
            # Legacy backward-compat
            ("compra_proveedor", "INVENTORY"),
            ("importado", "OTHER"),
            ("salida_caja", "OTHER"),
            # Tildes y mayúsculas
            ("LOGÍSTICA", "LOGISTICS"),
            ("Categoría con tildes: Reparación", "MAINTENANCE"),
        ],
    )
    def test_alias(self, raw: str, expected: str) -> None:
        code, label = normalize_expense_category(raw)
        assert code == expected
        assert label is None

    def test_canonical_codes_passthrough(self) -> None:
        for code in EXPENSE_CATEGORY_CODES:
            assert normalize_expense_category(code) == (code, None)
            # también en lower (payloads viejos de agentes)
            assert normalize_expense_category(code.lower()) == (code, None)


class TestNormalizeFuzzy:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Seguro comercio", "INSURANCE"),
            ("Impuestos varios", "TAXES"),
            ("Comision bancaria", "BANK_FEES"),
            ("mantenimento", "MAINTENANCE"),  # typo
        ],
    )
    def test_fuzzy_match(self, raw: str, expected: str) -> None:
        code, label = normalize_expense_category(raw)
        assert code == expected
        assert label is None


class TestNormalizeFallback:
    def test_none_and_empty(self) -> None:
        assert normalize_expense_category(None) == ("OTHER", None)
        assert normalize_expense_category("") == ("OTHER", None)
        assert normalize_expense_category("   ") == ("OTHER", None)

    def test_unknown_preserves_label(self) -> None:
        code, label = normalize_expense_category("Donaciones al club del barrio")
        assert code == "OTHER"
        assert label == "Donaciones al club del barrio"

    def test_label_truncated_to_50(self) -> None:
        long = "x" * 80
        code, label = normalize_expense_category(long)
        assert code == "OTHER"
        assert label is not None and len(label) == 50
