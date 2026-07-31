"""Tests unitarios de las funciones puras de scripts/detect_cost_as_price_imports.py.

Ninguna depende de DB: `_parece_costo` opera sobre un header y
`_mapeo_confirmado_manda_costo_a_precio` sobre el `detail` ya deserializado de un
`pipeline_events`. Se carga el módulo por ruta de archivo (`scripts/` no es un
paquete) — mismo patrón que `test_detect_misvoided_purchases.py`.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"


def _load_module() -> Any:
    if str(_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "detect_cost_as_price_imports", _SCRIPTS_DIR / "detect_cost_as_price_imports.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_mod = _load_module()


class TestPareceCosto:
    def test_reconoce_las_variantes_de_costo(self) -> None:
        for header in (
            "Precio de compra",
            "Precio compra",
            "Costo",
            "Costo unitario",
            "costo_compra",
            "Precio unitario",
        ):
            assert _mod._parece_costo(header), header

    def test_no_confunde_el_precio_de_venta_con_un_costo(self) -> None:
        """El falso positivo que arruinaría el reporte: marcar como sospechoso un
        producto cuyo precio de venta se cargó bien."""
        for header in (
            "Precio de venta",
            "Precio de venta final",
            "Precio unitario de venta",
            "Precio de lista",
            "Nombre",
            "Stock",
        ):
            assert not _mod._parece_costo(header), header

    def test_un_header_que_menciona_compra_y_venta_cuenta_como_costo(self) -> None:
        # "precio de compra para la venta" es ambiguo, pero mencionar compra pesa:
        # preferimos revisarlo de más a dejarlo pasar.
        assert _mod._parece_costo("Precio de compra para venta")


class TestMapeoConfirmado:
    def test_detecta_columna_de_costo_mapeada_a_precio_de_venta(self) -> None:
        """Evidencia nivel 1: el mapeo con el que se importó ESE archivo."""
        detail = {
            "mappings": {
                "flat": {},
                "context": {
                    "sheet:precios y stock": {
                        "Productos": "name",
                        "Precio de compra": "sale_price_ars",
                    }
                },
            }
        }
        assert _mod._mapeo_confirmado_manda_costo_a_precio(detail) == ["Precio de compra"]

    def test_un_mapeo_correcto_no_genera_hallazgo(self) -> None:
        detail = {
            "mappings": {
                "flat": {},
                "context": {
                    "sheet:precios y stock": {
                        "Precio de compra": "unit_cost_ars",
                        "Precio de lista": "list_price_ars",
                        "Precio de venta final": "sale_price_ars",
                    }
                },
            }
        }
        assert _mod._mapeo_confirmado_manda_costo_a_precio(detail) == []

    def test_tambien_mira_el_mapeo_plano(self) -> None:
        detail = {"mappings": {"flat": {"Costo": "sale_price_ars"}, "context": {}}}
        assert _mod._mapeo_confirmado_manda_costo_a_precio(detail) == ["Costo"]

    def test_un_confirm_sin_snapshot_no_rompe(self) -> None:
        """Los imports anteriores al fix de traza no tienen `mappings`: no hay
        evidencia de nivel 1 y se cae a los niveles 2/3, sin explotar."""
        assert _mod._mapeo_confirmado_manda_costo_a_precio({}) == []
        assert _mod._mapeo_confirmado_manda_costo_a_precio({"mappings": None}) == []
        assert _mod._mapeo_confirmado_manda_costo_a_precio({"imported_counts": {}}) == []


class TestHeadersDelSummary:
    def test_junta_headers_planos_y_por_hoja(self) -> None:
        summary = {
            "headers": ["a"],
            "mapping_contexts": [
                {"headers": ["b", "c"]},
                {"headers": None},
                "no-es-dict",
            ],
        }
        assert _mod._headers_del_summary(summary) == ["a", "b", "c"]

    def test_summary_vacio_no_rompe(self) -> None:
        assert _mod._headers_del_summary({}) == []
