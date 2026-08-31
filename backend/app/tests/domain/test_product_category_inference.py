"""Bloque 3B — inferencia pura de categoría desde nombre + especificaciones."""

from __future__ import annotations

import pytest

from app.domain.product_category_inference import infer_category
from app.domain.verticals import Vertical


@pytest.mark.parametrize(
    ("name", "expected_code"),
    [
        ("Silla de living tapizada", "MUEBLES"),
        ("Lámpara de pie con pantalla", "ILUMINACION"),
        ("Vela aromática de soja", "AROMAS"),
        ("Maceta de cerámica para jardín", "JARDIN"),
        ("Cuadro decorativo enmarcado", "DECO"),
        ("Taza de cerámica para café", "BAZAR"),
        ("Cortina blackout 2 paños", "TEXTILES"),
    ],
)
def test_categorias_representativas_de_decoracion_hogar(name: str, expected_code: str) -> None:
    suggestion = infer_category(Vertical.DECORACION_HOGAR, name)
    assert suggestion.code == expected_code
    assert suggestion.confidence == "high"
    assert suggestion.rule is not None and suggestion.rule.startswith("name:")


def test_especificacion_complementa_un_nombre_ambiguo() -> None:
    """El nombre solo no alcanza (genérico); las especificaciones sí."""
    suggestion = infer_category(
        Vertical.DECORACION_HOGAR,
        name="Combo x3 unidades",
        specifications="Vela aromática de soja, esencia de lavanda",
    )
    assert suggestion.code == "AROMAS"
    assert suggestion.confidence == "medium"
    assert suggestion.rule is not None and suggestion.rule.startswith("specifications:")


def test_nombre_ambiguo_con_varias_categorias_desempata_por_especificacion() -> None:
    """"Set de mesa" matchea MUEBLES (mesa) — con specs que agregan "vajilla"
    la evidencia de especificaciones no coincide con ninguna candidata del
    nombre, así que NO desempata (mesa ya es una única candidata: alta)."""
    suggestion = infer_category(
        Vertical.DECORACION_HOGAR,
        name="Set de mesa",
        specifications="Incluye vajilla de porcelana",
    )
    # "mesa" es la única keyword que matchea el nombre → alta confianza directa.
    assert suggestion.code == "MUEBLES"
    assert suggestion.confidence == "high"


def test_baja_confianza_no_categoriza() -> None:
    suggestion = infer_category(Vertical.DECORACION_HOGAR, name="Producto genérico X123")
    assert suggestion.code is None
    assert suggestion.confidence == "low"


def test_baja_confianza_sin_nombre_ni_especificaciones() -> None:
    suggestion = infer_category(Vertical.DECORACION_HOGAR, name=None, specifications=None)
    assert suggestion.code is None
    assert suggestion.confidence == "low"


def test_aislamiento_entre_verticales() -> None:
    """Un vertical sin catálogo de inferencia propio nunca hereda el de otro."""
    suggestion = infer_category(Vertical.LIBRERIA_PAPELERIA, name="Silla de living tapizada")
    assert suggestion.code is None
    assert suggestion.confidence == "low"


def test_nunca_inventa_un_codigo_fuera_del_catalogo() -> None:
    from app.domain.product_category_inference import CATEGORY_KEYWORDS

    catalogo = CATEGORY_KEYWORDS[Vertical.DECORACION_HOGAR]
    for name in ("Silla", "Lámpara", "Vela", "Maceta", "Cuadro", "Taza", "Cortina"):
        suggestion = infer_category(Vertical.DECORACION_HOGAR, name)
        if suggestion.code is not None:
            assert suggestion.code in catalogo
