"""Tests de los catálogos de categorías de producto por vertical."""

from app.domain.expense_categories import classify_expense_with_vertical
from app.domain.product_categories import (
    PRODUCT_CATEGORY_LABELS,
    normalize_product_category,
    product_category_catalog,
)


class TestCatalogs:
    def test_every_vertical_has_other(self) -> None:
        for vertical, labels in PRODUCT_CATEGORY_LABELS.items():
            assert "OTHER" in labels, vertical

    def test_catalog_shape(self) -> None:
        catalog = product_category_catalog("kiosco_almacen")
        assert {"code": "BEBIDAS", "label": "Bebidas"} in catalog

    def test_unknown_vertical_falls_back_to_kiosco(self) -> None:
        assert product_category_catalog("ferreteria") == product_category_catalog(
            "kiosco_almacen"
        )
        assert product_category_catalog(None) == product_category_catalog("kiosco_almacen")

    def test_vertical_alias_kiosco(self) -> None:
        assert product_category_catalog("kiosco") == product_category_catalog(
            "kiosco_almacen"
        )


class TestNormalize:
    def test_kiosco_aliases(self) -> None:
        assert normalize_product_category("Gaseosas", "kiosco_almacen") == ("BEBIDAS", None)
        assert normalize_product_category("golosinas", "kiosco_almacen") == (
            "GOLOSINAS",
            None,
        )
        assert normalize_product_category("Lácteos", "kiosco_almacen") == ("LACTEOS", None)

    def test_label_passthrough(self) -> None:
        # El label canónico es alias de sí mismo.
        assert normalize_product_category("Bebidas", "kiosco_almacen") == ("BEBIDAS", None)
        assert normalize_product_category("BEBIDAS", "kiosco_almacen") == ("BEBIDAS", None)

    def test_limpieza_vertical(self) -> None:
        assert normalize_product_category("Lavandina", "limpieza") == ("QUIMICOS", None)
        assert normalize_product_category("Bolsas de residuo", "limpieza") == (
            "BOLSAS",
            None,
        )

    def test_deco_vertical(self) -> None:
        assert normalize_product_category("Velas aromáticas", "decoracion_hogar") == (
            "AROMAS",
            None,
        )
        assert normalize_product_category("Almohadones", "decoracion_hogar") == (
            "TEXTILES",
            None,
        )

    def test_unknown_preserves_label(self) -> None:
        code, label = normalize_product_category("Repuestos de bicicleta", "kiosco_almacen")
        assert code == "OTHER"
        assert label == "Repuestos de bicicleta"

    def test_same_text_different_vertical(self) -> None:
        # "Limpieza" es categoría de producto en kiosco; en el vertical limpieza
        # no existe como código → cae a OTHER con label.
        assert normalize_product_category("Limpieza", "kiosco_almacen") == (
            "LIMPIEZA",
            None,
        )

    def test_kiosco_diarios_revistas(self) -> None:
        assert normalize_product_category("Diarios", "kiosco_almacen") == (
            "DIARIOS_REVISTAS",
            None,
        )
        assert normalize_product_category("La Nación", "kiosco_almacen") == (
            "DIARIOS_REVISTAS",
            None,
        )
        assert normalize_product_category("Clarín", "kiosco_almacen") == (
            "DIARIOS_REVISTAS",
            None,
        )
        assert normalize_product_category("Revistas", "kiosco_almacen") == (
            "DIARIOS_REVISTAS",
            None,
        )

    def test_kiosco_regaleria_accesorios(self) -> None:
        assert normalize_product_category("Auriculares", "kiosco_almacen") == (
            "REGALERIA",
            None,
        )
        assert normalize_product_category("Pilas", "kiosco_almacen") == (
            "REGALERIA",
            None,
        )
        assert normalize_product_category("Encendedores", "kiosco_almacen") == (
            "REGALERIA",
            None,
        )


class TestExpenseCrossClassification:
    """Cruce con ``classify_expense_with_vertical``: mercadería vendible del
    vertical detectada en un texto de gasto → INVENTORY/COGS."""

    def test_diarios_classifies_as_inventory(self) -> None:
        code, label, is_merch = classify_expense_with_vertical(
            "Diarios La Nación", "kiosco_almacen"
        )
        assert code == "INVENTORY"
        assert label == "Diarios La Nación"
        assert is_merch is True

    def test_auriculares_classifies_as_inventory(self) -> None:
        code, _label, is_merch = classify_expense_with_vertical(
            "Auriculares", "kiosco_almacen"
        )
        assert code == "INVENTORY"
        assert is_merch is True

    def test_operativo_stays_opex(self) -> None:
        # Insumo operativo real → NO debe volverse mercadería.
        code, _label, is_merch = classify_expense_with_vertical(
            "bolsas uso interno", "kiosco_almacen"
        )
        assert code != "INVENTORY"
        assert is_merch is False
