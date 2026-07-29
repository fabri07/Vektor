"""Carga la configuración completa de heurísticas desde JSONs.

Los JSONs en app/application/data/heuristics son la fuente de verdad para el
Health Engine. NO hay fallback: un vertical desconocido lo rechaza
``parse_vertical`` (el tipo del parámetro ya es ``Vertical``) y un JSON faltante
o corrupto levanta. Scorear un negocio con los benchmarks de otro rubro es peor
que no scorearlo.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.domain.verticals import Vertical, parse_vertical
from app.heuristics.verticals import (
    CashHealthBenchmark,
    InventoryBenchmark,
    MarginBenchmark,
    SupplierBenchmark,
    VerticalHeuristicConfig,
)

_HEURISTICS_DIR = (
    Path(__file__).resolve().parents[3] / "app" / "application" / "data" / "heuristics"
)


def _config_from_json(data: dict[str, Any]) -> VerticalHeuristicConfig:
    margin = data["margin"]
    cash = data["cash_health"]
    inventory = data["inventory"]
    supplier = data["supplier"]
    return VerticalHeuristicConfig(
        business_type=parse_vertical(data["business_type"]),
        cash_health=CashHealthBenchmark(
            healthy_days_min=float(cash["healthy_days_min"]),
            warning_days_min=float(cash["warning_days_min"]),
            critical_days_below=float(cash["critical_days_below"]),
        ),
        margin=MarginBenchmark(
            critical_below=float(margin["critical_below"]),
            warning_below=float(margin["warning_below"]),
            healthy_min=float(margin["healthy_min"]),
            healthy_max=float(margin["healthy_max"]),
        ),
        inventory=InventoryBenchmark(
            rotation_days_min=float(inventory["rotation_days_min"]),
            rotation_days_max=float(inventory["rotation_days_max"]),
            overstock_tolerance=str(inventory["overstock_tolerance"]),
        ),
        supplier=SupplierBenchmark(
            reorder_frequency=str(supplier["reorder_frequency"]),
            stockout_sensitivity=str(supplier["stockout_sensitivity"]),
        ),
        seasonality=str(data["seasonality"]),
    )


@lru_cache(maxsize=len(Vertical))
def load_vertical_heuristics(vertical: Vertical) -> VerticalHeuristicConfig:
    """Configuración completa del vertical desde su JSON canónico.

    Los tres archivos son inmutables en runtime, por eso se cachean. Un JSON
    faltante (``FileNotFoundError``), inválido (``json.JSONDecodeError``) o
    incompleto (``KeyError``) propaga: es un bug de deploy, no un caso a tapar.
    """
    json_path = _HEURISTICS_DIR / f"{vertical.value}.json"
    data = json.loads(json_path.read_text(encoding="utf-8"))
    return _config_from_json(data)


def load_margin_benchmark(vertical: Vertical) -> MarginBenchmark:
    """Compatibilidad para callers que todavía solo necesitan margen."""
    return load_vertical_heuristics(vertical).margin
