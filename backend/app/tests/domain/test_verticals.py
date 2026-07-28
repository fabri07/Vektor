"""Test compuerta de `app/domain/verticals.py`.

Parametrizado sobre `Vertical`: para cada vertical operativo, asegura que
existen sus heurísticas, sus campos de vertical, su label, su rango de margen
y su catálogo de categorías de producto. Este test vuelve imposible agregar un
4º vertical a medias — y va a proteger todo lo que Task 2 y Task 3 construyen
encima del enum.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.domain.product_categories import PRODUCT_CATEGORY_LABELS
from app.domain.verticals import (
    OPERATIONAL_VERTICALS,
    VERTICAL_LABELS,
    RequestedVertical,
    UnknownVerticalError,
    Vertical,
    heuristic_profile_version,
    parse_vertical,
    try_parse_vertical,
)
from app.heuristics.insight_templates import _MARGIN_RANGES

_BACKEND_ROOT = Path(__file__).resolve().parents[3]
_HEURISTICS_DIR = _BACKEND_ROOT / "app" / "application" / "data" / "heuristics"
_VERTICAL_FIELDS_DIR = (
    _BACKEND_ROOT / "app" / "application" / "data" / "vertical_fields"
)

# Cantidad de claves-hoja que debe tener cada JSON de heurísticas: business_type
# (1) + cash_health (3) + margin (6) + inventory (3) + supplier (2) +
# seasonality (1) = 16. Un JSON al que le falte una clave-hoja pasaba
# desapercibido antes de este test.
_HEURISTICS_LEAF_KEYS = 16


def _count_leaves(data: object) -> int:
    if isinstance(data, dict):
        return sum(_count_leaves(value) for value in data.values())
    return 1


class TestCatalogoCompleto:
    """Cada vertical de `Vertical` tiene toda su infraestructura de datos."""

    @pytest.mark.parametrize("vertical", list(Vertical))
    def test_tiene_heuristicas_con_las_16_claves_hoja(
        self, vertical: Vertical
    ) -> None:
        path = _HEURISTICS_DIR / f"{vertical.value}.json"
        assert path.exists(), f"Falta el archivo de heurísticas: {path}"
        data = json.loads(path.read_text(encoding="utf-8"))
        leaves = _count_leaves(data)
        assert leaves == _HEURISTICS_LEAF_KEYS, (
            f"{vertical.value}: se esperaban {_HEURISTICS_LEAF_KEYS} "
            f"claves-hoja en {path.name}, se encontraron {leaves}"
        )

    @pytest.mark.parametrize("vertical", list(Vertical))
    def test_tiene_definiciones_de_campos_de_vertical(
        self, vertical: Vertical
    ) -> None:
        path = _VERTICAL_FIELDS_DIR / f"{vertical.value}.json"
        assert path.exists(), f"Falta el archivo de campos de vertical: {path}"

    @pytest.mark.parametrize("vertical", list(Vertical))
    def test_tiene_label(self, vertical: Vertical) -> None:
        assert vertical in VERTICAL_LABELS
        assert VERTICAL_LABELS[vertical].strip()

    @pytest.mark.parametrize("vertical", list(Vertical))
    def test_tiene_rango_de_margen(self, vertical: Vertical) -> None:
        assert vertical in _MARGIN_RANGES

    @pytest.mark.parametrize("vertical", list(Vertical))
    def test_tiene_catalogo_de_categorias_de_producto(
        self, vertical: Vertical
    ) -> None:
        assert vertical in PRODUCT_CATEGORY_LABELS
        assert PRODUCT_CATEGORY_LABELS[vertical]

    def test_operational_verticals_coincide_con_el_enum(self) -> None:
        assert {v.value for v in Vertical} == OPERATIONAL_VERTICALS


class TestParseVertical:
    """`parse_vertical` no aliasea nada: falla ruidosamente ante cualquier
    código que no sea exactamente uno de los tres valores canónicos."""

    @pytest.mark.parametrize("vertical", list(Vertical))
    def test_valores_canonicos_parsean(self, vertical: Vertical) -> None:
        assert parse_vertical(vertical.value) is vertical

    @pytest.mark.parametrize(
        "raw",
        [
            "kiosco",  # código corto legado — NO es un alias
            "otros",  # valor de RequestedVertical, no de Vertical
            "",
            "  ",
            None,
            "KIOSCO_ALMACEN",  # case-sensitive
            "Kiosco_Almacen",
            "kiosco almacen",
            "inexistente",
        ],
    )
    def test_valores_invalidos_levantan(self, raw: str | None) -> None:
        with pytest.raises(UnknownVerticalError):
            parse_vertical(raw)

    def test_mensaje_nombra_el_valor_recibido_y_los_validos(self) -> None:
        with pytest.raises(UnknownVerticalError) as exc_info:
            parse_vertical("kiosco")
        message = str(exc_info.value)
        assert "kiosco" in message
        for vertical in Vertical:
            assert vertical.value in message


class TestTryParseVertical:
    """`try_parse_vertical` devuelve `None` en vez de levantar."""

    @pytest.mark.parametrize("vertical", list(Vertical))
    def test_valores_canonicos_parsean(self, vertical: Vertical) -> None:
        assert try_parse_vertical(vertical.value) is vertical

    @pytest.mark.parametrize("raw", ["kiosco", "otros", "", None, "INVALIDO"])
    def test_valores_invalidos_devuelven_none(self, raw: str | None) -> None:
        assert try_parse_vertical(raw) is None


class TestHeuristicProfileVersion:
    @pytest.mark.parametrize("vertical", list(Vertical))
    def test_formato(self, vertical: Vertical) -> None:
        assert heuristic_profile_version(vertical) == f"{vertical.value}:v1"


class TestRequestedVertical:
    """`RequestedVertical` es el superconjunto usado en el formulario de
    solicitud: los 3 verticales operativos + `otros`."""

    def test_incluye_los_verticales_operativos_mas_otros(self) -> None:
        assert {v.value for v in RequestedVertical} == OPERATIONAL_VERTICALS | {
            "otros"
        }
