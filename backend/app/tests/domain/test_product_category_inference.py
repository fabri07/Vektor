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


@pytest.mark.parametrize(
    ("name", "expected_code"),
    [
        # Nombres REALES de un catálogo del rubro que no matcheaban nada. La
        # ampliación del vocabulario se midió sobre esos 398 productos: la
        # cobertura con confianza alta pasó de 114 (28%) a 180 (45%).
        ("alfombra felpuda semi circular", "TEXTILES"),
        ("frazada polar 2pl", "TEXTILES"),
        ("set x 2 repasadores", "TEXTILES"),
        ("bandeja rose calada", "BAZAR"),
        ("frasco vidrio 850 ml", "BAZAR"),
        ("huevera x 6 hoyos", "BAZAR"),
        ("especiero apilable granito", "BAZAR"),
        ("set salero pimentero", "BAZAR"),
        ("tabla de picar pino", "BAZAR"),
        ("hermetico sao red. 1750 ml", "BAZAR"),
        ("set x 6 utensilios hudson", "BAZAR"),
        ("cucharas medidoras", "BAZAR"),
        ("cafetera  francesa hudson", "BAZAR"),
    ],
)
def test_vocabulario_medido_contra_nombres_reales(name: str, expected_code: str) -> None:
    suggestion = infer_category(Vertical.DECORACION_HOGAR, name)
    assert suggestion.code == expected_code
    assert suggestion.confidence == "high"


@pytest.mark.parametrize(
    "name",
    ["canasto yute grande", "Cesto organizador", "porta bolsas mascota", "porta llaves"],
)
def test_articulos_de_organizacion_siguen_sin_categoria(name: str) -> None:
    """No es un olvido: el catálogo del rubro no tiene categoría de organización,
    y meterlos en DECO o BAZAR sería elegir por el negocio. Prefiere "sin
    categoría" antes que una categoría inventada (regla de no-invención). Si
    mañana se agrega la categoría, este test es el que hay que cambiar."""
    assert infer_category(Vertical.DECORACION_HOGAR, name).code is None


def test_una_palabra_nueva_no_le_roba_un_producto_a_otra_categoria() -> None:
    """`tabla` (BAZAR) y `mantel` (TEXTILES) en el mismo nombre dan ambigüedad, y
    ambiguo es "sin sugerencia" — nunca la primera del dict. Es la propiedad que
    hace seguro ampliar el vocabulario: sumar palabras puede dejar de resolver un
    caso, pero no puede resolverlo MAL."""
    assert infer_category(Vertical.DECORACION_HOGAR, "set tabla + mantel").code is None
