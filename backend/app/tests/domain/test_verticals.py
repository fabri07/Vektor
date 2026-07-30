"""Test compuerta de `app/domain/verticals.py`.

Parametrizado sobre `Vertical`: para cada vertical operativo, asegura que
existen sus heurísticas completas, la procedencia declarada de cada bloque, sus
campos de vertical, su label, su catálogo de categorías de producto y su tabla
de aliases. Vuelve imposible agregar un rubro a medias.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.domain.product_categories import _ALIASES, PRODUCT_CATEGORY_LABELS
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
from app.heuristics.insight_templates import margin_range_pct
from app.heuristics.verticals.loader import load_margin_benchmark

_BACKEND_ROOT = Path(__file__).resolve().parents[3]
_HEURISTICS_DIR = _BACKEND_ROOT / "app" / "application" / "data" / "heuristics"
_VERTICAL_FIELDS_DIR = (
    _BACKEND_ROOT / "app" / "application" / "data" / "vertical_fields"
)

# Cantidad de claves-hoja de CONFIGURACIÓN NUMÉRICA que debe tener cada JSON de
# heurísticas: business_type (1) + cash_health (3) + margin (6) + inventory (3) +
# supplier (3) + seasonality (1) = 17. Un JSON al que le falte una clave-hoja
# pasaba desapercibido antes de este test.
#
# Las `source` de cada bloque se cuentan aparte (ver el test siguiente): son
# metadato de procedencia y su valor legítimo puede ser `null`.
_HEURISTICS_LEAF_KEYS = 17
_SOURCE_KEY = "source"
_SOURCE_FIELDS = frozenset({"institucion", "referencia", "revisado_en"})
#: Bloques del JSON que declaran procedencia propia.
_BLOQUES_CON_FUENTE = ("cash_health", "margin", "inventory", "supplier")


def _count_leaves(data: object) -> int:
    if isinstance(data, dict):
        return sum(
            _count_leaves(value) for key, value in data.items() if key != _SOURCE_KEY
        )
    return 1


class TestCatalogoCompleto:
    """Cada vertical de `Vertical` tiene toda su infraestructura de datos."""

    @pytest.mark.parametrize("vertical", list(Vertical))
    def test_tiene_heuristicas_con_todas_sus_claves_hoja(
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
    def test_cada_bloque_declara_su_procedencia(self, vertical: Vertical) -> None:
        """Los cuatro bloques llevan `source`, aunque el valor sea `null`.

        Que la clave sea obligatoria es el punto: agregar un rubro obliga a
        pronunciarse bloque por bloque sobre de dónde salieron los números. Si
        fuera opcional, el olvido se leería igual que "sin fuente" y nadie lo
        notaría — salvo que el `.get()` implícito lo tratara como respaldado,
        que es peor.

        Y es POR BLOQUE porque un informe sectorial típico documenta márgenes y
        rotación pero no días de cobertura de caja: una sola declaración por
        rubro haría que el sistema afirme que la caja está respaldada solo
        porque el margen lo está.
        """
        path = _HEURISTICS_DIR / f"{vertical.value}.json"
        data = json.loads(path.read_text(encoding="utf-8"))

        for bloque in _BLOQUES_CON_FUENTE:
            assert _SOURCE_KEY in data[bloque], (
                f"{vertical.value}: falta `{bloque}.{_SOURCE_KEY}` en {path.name} "
                "(usá `null` si ese bloque todavía no tiene fuente sectorial)"
            )
            source = data[bloque][_SOURCE_KEY]
            if source is None:
                continue
            assert set(source) == _SOURCE_FIELDS, (
                f"{vertical.value}/{bloque}: `{_SOURCE_KEY}` debe tener exactamente "
                f"{sorted(_SOURCE_FIELDS)}, tiene {sorted(source)}"
            )
            assert all(str(v).strip() for v in source.values()), (
                f"{vertical.value}/{bloque}: `{_SOURCE_KEY}` con campos vacíos "
                "no es una fuente"
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
    def test_el_rango_narrado_es_el_que_usa_el_score(self, vertical: Vertical) -> None:
        """El texto del insight y el umbral que puntúa salen del MISMO lugar.

        Antes `insight_templates` mantenía una tabla paralela escrita a mano:
        recalibrar un rubro dejaba al insight afirmando un rango que el score ya
        no usaba, y nada lo detectaba porque las dos fuentes nunca se comparaban.
        """
        benchmark = load_margin_benchmark(vertical)
        assert margin_range_pct(vertical) == (
            round(benchmark.healthy_min * 100),
            round(benchmark.healthy_max * 100),
        )

    @pytest.mark.parametrize("vertical", list(Vertical))
    def test_tiene_catalogo_de_categorias_de_producto(
        self, vertical: Vertical
    ) -> None:
        assert vertical in PRODUCT_CATEGORY_LABELS
        assert PRODUCT_CATEGORY_LABELS[vertical]

    @pytest.mark.parametrize("vertical", list(Vertical))
    def test_tiene_aliases_de_categoria(self, vertical: Vertical) -> None:
        """`_ALIASES` se accede con `.get(vertical, {})`: un vertical sin entrada
        no revienta — normaliza TODO a `OTHER` en silencio, que es peor."""
        assert vertical in _ALIASES, (
            f"{vertical.value}: falta su tabla de aliases en "
            "`app/domain/product_categories._ALIASES`"
        )
        assert _ALIASES[vertical]

    def test_operational_verticals_coincide_con_el_enum(self) -> None:
        assert {v.value for v in Vertical} == OPERATIONAL_VERTICALS


class TestParseVertical:
    """`parse_vertical` no aliasea nada: falla ruidosamente ante cualquier
    código que no sea exactamente uno de los valores canónicos."""

    @pytest.mark.parametrize("vertical", list(Vertical))
    def test_valores_canonicos_parsean(self, vertical: Vertical) -> None:
        assert parse_vertical(vertical.value) is vertical

    @pytest.mark.parametrize(
        "raw",
        [
            "kiosco",  # código corto legado — NO es un alias
            "almacen",  # ídem
            "decoracion",  # ídem
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
