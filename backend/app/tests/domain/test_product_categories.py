"""Tests de los catálogos de categorías de producto por vertical."""

import pytest

from app.domain.expense_categories import classify_expense_with_vertical
from app.domain.product_categories import (
    PRODUCT_CATEGORY_LABELS,
    normalize_product_category,
    product_category_catalog,
)
from app.domain.verticals import UnknownVerticalError, Vertical, parse_vertical

KIOSCO = Vertical.KIOSCO_ALMACEN


class TestCatalogs:
    def test_every_vertical_has_other(self) -> None:
        for vertical, labels in PRODUCT_CATEGORY_LABELS.items():
            assert "OTHER" in labels, vertical

    def test_catalog_shape(self) -> None:
        catalog = product_category_catalog(KIOSCO)
        assert {"code": "BEBIDAS", "label": "Bebidas"} in catalog

    def test_every_vertical_has_a_catalog(self) -> None:
        for vertical in Vertical:
            assert product_category_catalog(vertical)

    @pytest.mark.parametrize("raw", ["ferreteria", "kiosco", None, ""])
    def test_unknown_or_legacy_vertical_raises(self, raw: str | None) -> None:
        """Ya no hay fallback ni alias: el vertical se parsea en el borde y un
        código desconocido (incluido el corto legado ``kiosco``) levanta."""
        with pytest.raises(UnknownVerticalError):
            product_category_catalog(parse_vertical(raw))


class TestNormalize:
    def test_kiosco_aliases(self) -> None:
        assert normalize_product_category("Gaseosas", KIOSCO) == ("BEBIDAS", None)
        assert normalize_product_category("golosinas", KIOSCO) == (
            "GOLOSINAS",
            None,
        )
        assert normalize_product_category("Lácteos", KIOSCO) == ("LACTEOS", None)

    def test_label_passthrough(self) -> None:
        # El label canónico es alias de sí mismo.
        assert normalize_product_category("Bebidas", KIOSCO) == ("BEBIDAS", None)
        assert normalize_product_category("BEBIDAS", KIOSCO) == ("BEBIDAS", None)

    def test_limpieza_vertical(self) -> None:
        assert normalize_product_category("Lavandina", Vertical.LIMPIEZA) == ("QUIMICOS", None)
        assert normalize_product_category("Bolsas de residuo", Vertical.LIMPIEZA) == (
            "BOLSAS",
            None,
        )

    def test_deco_vertical(self) -> None:
        assert normalize_product_category("Velas aromáticas", Vertical.DECORACION_HOGAR) == (
            "AROMAS",
            None,
        )
        assert normalize_product_category("Almohadones", Vertical.DECORACION_HOGAR) == (
            "TEXTILES",
            None,
        )

    def test_unknown_preserves_label(self) -> None:
        code, label = normalize_product_category("Repuestos de bicicleta", KIOSCO)
        assert code == "OTHER"
        assert label == "Repuestos de bicicleta"

    def test_same_text_different_vertical(self) -> None:
        # "Limpieza" es categoría de producto en kiosco; en el vertical limpieza
        # no existe como código → cae a OTHER con label.
        assert normalize_product_category("Limpieza", KIOSCO) == (
            "LIMPIEZA",
            None,
        )

    def test_kiosco_diarios_revistas(self) -> None:
        assert normalize_product_category("Diarios", KIOSCO) == (
            "DIARIOS_REVISTAS",
            None,
        )
        assert normalize_product_category("La Nación", KIOSCO) == (
            "DIARIOS_REVISTAS",
            None,
        )
        assert normalize_product_category("Clarín", KIOSCO) == (
            "DIARIOS_REVISTAS",
            None,
        )
        assert normalize_product_category("Revistas", KIOSCO) == (
            "DIARIOS_REVISTAS",
            None,
        )

    def test_kiosco_regaleria_accesorios(self) -> None:
        assert normalize_product_category("Auriculares", KIOSCO) == (
            "REGALERIA",
            None,
        )
        assert normalize_product_category("Pilas", KIOSCO) == (
            "REGALERIA",
            None,
        )
        assert normalize_product_category("Encendedores", KIOSCO) == (
            "REGALERIA",
            None,
        )


class TestExpenseCrossClassification:
    """Cruce con ``classify_expense_with_vertical``: mercadería vendible del
    vertical detectada en un texto de gasto → INVENTORY/COGS."""

    def test_diarios_classifies_as_inventory(self) -> None:
        code, label, is_merch = classify_expense_with_vertical(
            "Diarios La Nación", KIOSCO
        )
        assert code == "INVENTORY"
        assert label == "Diarios La Nación"
        assert is_merch is True

    def test_auriculares_classifies_as_inventory(self) -> None:
        code, _label, is_merch = classify_expense_with_vertical(
            "Auriculares", KIOSCO
        )
        assert code == "INVENTORY"
        assert is_merch is True

    def test_operativo_stays_opex(self) -> None:
        # Insumo operativo real → NO debe volverse mercadería.
        code, _label, is_merch = classify_expense_with_vertical(
            "bolsas uso interno", KIOSCO
        )
        assert code != "INVENTORY"
        assert is_merch is False
