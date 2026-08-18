"""F-ID: prefijo/formato de código Véktor, puro.

`product_prefix_for` resuelve por categoría curada o cae a GEN sin lanzar
nunca. `format_code` nunca trunca el correlativo. El test de cobertura es la
compuerta de CI real: agregar una categoría al catálogo canónico sin curar su
prefijo tiene que romper la suite, no caer a GEN en silencio.
"""

from __future__ import annotations

from app.domain.entity_code import (
    CUSTOMER_PREFIX,
    FALLBACK_PRODUCT_PREFIX,
    PRODUCT_CATEGORY_PREFIXES,
    SUPPLIER_PREFIX,
    format_code,
    product_prefix_for,
)
from app.domain.product_categories import PRODUCT_CATEGORY_LABELS
from app.domain.verticals import Vertical


def test_product_prefix_for_categoria_curada() -> None:
    assert product_prefix_for(Vertical.DECORACION_HOGAR, "TEXTILES") == "TEX"


def test_product_prefix_for_sin_categoria_cae_a_gen() -> None:
    assert product_prefix_for(Vertical.DECORACION_HOGAR, None) == FALLBACK_PRODUCT_PREFIX
    assert product_prefix_for(None, None) == FALLBACK_PRODUCT_PREFIX


def test_product_prefix_for_categoria_other_cae_a_gen() -> None:
    assert product_prefix_for(Vertical.KIOSCO_ALMACEN, "OTHER") == FALLBACK_PRODUCT_PREFIX


def test_product_prefix_for_categoria_custom_del_tenant_cae_a_gen() -> None:
    assert (
        product_prefix_for(Vertical.KIOSCO_ALMACEN, "CUSTOM_LO_QUE_SEA")
        == FALLBACK_PRODUCT_PREFIX
    )


def test_cobertura_total_del_catalogo_curado_por_vertical() -> None:
    """La compuerta de CI: toda categoría real (menos OTHER) tiene prefijo
    curado en TODAS las 6 verticales del catálogo canónico."""
    for vertical, labels in PRODUCT_CATEGORY_LABELS.items():
        categorias_reales = set(labels) - {"OTHER"}
        prefijos_curados = set(PRODUCT_CATEGORY_PREFIXES.get(vertical, {}))
        faltantes = categorias_reales - prefijos_curados
        assert not faltantes, (
            f"{vertical}: categorías sin prefijo curado: {faltantes}"
        )


def test_prefijos_unicos_dentro_de_cada_vertical() -> None:
    for vertical, prefijos in PRODUCT_CATEGORY_PREFIXES.items():
        valores = list(prefijos.values())
        assert len(valores) == len(set(valores)), (
            f"{vertical}: prefijos repetidos entre categorías: {prefijos}"
        )


def test_customer_supplier_prefix_planos() -> None:
    assert CUSTOMER_PREFIX == "CLI"
    assert SUPPLIER_PREFIX == "PRV"


def test_format_code_basico() -> None:
    assert format_code("TEX", 1) == "TEX-0001"
    assert format_code("CLI", 42) == "CLI-0042"


def test_format_code_nunca_trunca() -> None:
    assert format_code("GEN", 12345) == "GEN-12345"
