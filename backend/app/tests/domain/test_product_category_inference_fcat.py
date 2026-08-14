"""F-CAT — inferir la categoría de un producto desde su nombre, sólo con evidencia.

El Paso 0 sobre una cuenta real devolvió **0 de 398 productos con categoría**, y
el archivo no traía columna de categoría: la única fuente posible es el nombre.
Pero inferir desde un nombre es adivinar, así que la regla es la misma que
gobierna las fechas (F6-A2) y las filas sin monto (F-H4): entre elegir mal y no
elegir, Véktor conserva el dato y pregunta.

**Medido sobre los nombres reales de esa cuenta antes de fijar la regla:** la
tabla de alias está escrita en plural ("alfombras", "cortinas") y los productos
vienen en singular ("alfombra shaggy"), así que casi nada resolvía — y lo poco
que resolvía lo hacía por la palabra equivocada: `alfombra felpuda exterior` daba
JARDIN porque matcheaba "exterior" y no "alfombra". Plegar el plural arregla las
dos cosas a la vez: aparece "alfombra" y, al aparecer, el caso pasa a tener DOS
categorías posibles y se abstiene.
"""

from __future__ import annotations

import pytest

from app.domain.expense_categories import (
    EXPENSE_CATEGORY_LABELS_ES,
    normalize_expense_category,
)
from app.domain.product_categories import (
    infer_product_category_from_name,
    normalize_product_category,
)
from app.domain.verticals import Vertical

_DECO = Vertical.DECORACION_HOGAR


class TestInferenciaConEvidencia:
    @pytest.mark.parametrize(
        ("nombre", "esperado"),
        [
            ("almohadones lienzo 30 x 50", "TEXTILES"),
            ("cortina black out", "TEXTILES"),
            ("alfombra shaggy", "TEXTILES"),
            ("vela aromatica", "AROMAS"),
            ("difusor de ambiente", "AROMAS"),
            ("mesa ratona", "MUEBLES"),
        ],
    )
    def test_una_sola_categoria_posible_se_infiere(
        self, nombre: str, esperado: str
    ) -> None:
        assert infer_product_category_from_name(nombre, _DECO) == esperado

    @pytest.mark.parametrize(
        "nombre",
        [
            # TEXTILES (alfombra) + JARDIN (exterior): un felpudo de exterior es
            # de las dos, y eso lo decide una persona.
            "alfombra felpuda exterior",
            # ILUMINACION (lampara) + MUEBLES (mesa).
            "lampara de mesa",
        ],
    )
    def test_dos_categorias_posibles_no_se_infiere(self, nombre: str) -> None:
        assert infer_product_category_from_name(nombre, _DECO) is None

    @pytest.mark.parametrize(
        "nombre",
        [
            "porta perfume recargable",
            "agarradera tusor",
            "planner semanal",
            "organizador de viaje",
            "",
        ],
    )
    def test_sin_evidencia_no_se_infiere(self, nombre: str) -> None:
        assert infer_product_category_from_name(nombre, _DECO) is None

    def test_nunca_devuelve_other(self) -> None:
        """«Otros» es una categoría REAL del catálogo, no un tacho.

        Si lo que no se pudo inferir cayera ahí, el producto quedaría clasificado
        sin que nadie lo haya clasificado — y encima invisible en el filtro «Sin
        categoría», que es donde el usuario va a buscar lo que le falta completar.
        """
        assert infer_product_category_from_name("otros varios", _DECO) is None
        assert infer_product_category_from_name("articulo otros", _DECO) is None

    def test_el_nombre_vacio_o_none_no_rompe(self) -> None:
        assert infer_product_category_from_name(None, _DECO) is None


class TestPlegadoDePlural:
    def test_el_alias_en_plural_reconoce_el_singular(self) -> None:
        """El alias es "alfombras"; el producto real se llama "alfombra"."""
        assert infer_product_category_from_name("alfombra", _DECO) == "TEXTILES"
        assert infer_product_category_from_name("alfombras", _DECO) == "TEXTILES"

    def test_no_pliega_palabras_cortas(self) -> None:
        """Sin el piso de 4 caracteres, "mes" se convierte en "me" y "gas" en
        "ga" — los dos son alias reales de gastos, y plegarlos los rompería."""
        # El camino declarado (normalize) no usa el plegado, pero el piso protege
        # a cualquiera que use `codes_present` sobre vocabulario de gastos.
        code, _ = normalize_expense_category("gas")
        assert code in EXPENSE_CATEGORY_LABELS_ES


class TestElCaminoDeclaradoNoCambia:
    """`normalize_product_category` es lo que corre cuando el usuario MAPEÓ una
    columna de categoría. Ahí no se adivina nada y su semántica queda intacta:
    desempata por alias más largo y cae a OTHER conservando el texto."""

    def test_una_categoria_declarada_sigue_resolviendo(self) -> None:
        assert normalize_product_category("Textiles", _DECO)[0] == "TEXTILES"
        assert normalize_product_category("textil", _DECO)[0] == "TEXTILES"

    def test_lo_desconocido_declarado_sigue_cayendo_a_other_con_su_texto(self) -> None:
        code, label = normalize_product_category("Rubro inventado", _DECO)
        assert code == "OTHER"
        assert label == "Rubro inventado"
