"""Carga la configuración completa de heurísticas desde JSONs.

Los JSONs en app/application/data/heuristics son la fuente de verdad para el
Health Engine. Se mantiene fallback a kiosco_almacen para no cortar scoring si
un vertical desconocido o un JSON inválido llega a producción.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.heuristics.verticals import (
    CashHealthBenchmark,
    InventoryBenchmark,
    MarginBenchmark,
    SupplierBenchmark,
    VerticalHeuristicConfig,
)
from app.observability.logger import get_logger

logger = get_logger(__name__)

_HEURISTICS_DIR = (
    Path(__file__).resolve().parents[3] / "app" / "application" / "data" / "heuristics"
)

# Alias para vertical codes históricos o alternativos
_VERTICAL_ALIASES: dict[str, str] = {
    "kiosco": "kiosco_almacen",
    "deco": "decoracion_hogar",
    "deco_hogar": "decoracion_hogar",
    "cleaning": "limpieza",
}

_DEFAULT_CONFIG = VerticalHeuristicConfig(
    business_type="kiosco_almacen",
    cash_health=CashHealthBenchmark(
        healthy_days_min=10,
        warning_days_min=7,
        critical_days_below=5,
    ),
    margin=MarginBenchmark(
        critical_below=0.10,
        warning_below=0.18,
        healthy_min=0.18,
        healthy_max=0.28,
    ),
    inventory=InventoryBenchmark(
        rotation_days_min=7,
        rotation_days_max=21,
        overstock_tolerance="baja",
    ),
    supplier=SupplierBenchmark(
        reorder_frequency="semanal",
        stockout_sensitivity="muy_alta",
    ),
    seasonality="baja",
)


def _config_from_json(data: dict) -> VerticalHeuristicConfig:
    margin = data["margin"]
    cash = data["cash_health"]
    inventory = data["inventory"]
    supplier = data["supplier"]
    return VerticalHeuristicConfig(
        business_type=str(data.get("business_type") or "kiosco_almacen"),
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
            overstock_tolerance=str(inventory.get("overstock_tolerance", "media")),
        ),
        supplier=SupplierBenchmark(
            reorder_frequency=str(supplier.get("reorder_frequency", "mensual")),
            stockout_sensitivity=str(supplier.get("stockout_sensitivity", "media")),
        ),
        seasonality=str(data.get("seasonality", "media")),
    )


def load_vertical_heuristics(vertical_code: str) -> VerticalHeuristicConfig:
    """Carga configuración completa del vertical con fallback seguro."""
    normalized = _VERTICAL_ALIASES.get(vertical_code, vertical_code)
    json_path = _HEURISTICS_DIR / f"{normalized}.json"

    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            return _config_from_json(data)
        except (KeyError, ValueError, json.JSONDecodeError):
            pass  # JSON malformado → caer al fallback

    logger.warning(
        "heuristics.vertical.fallback",
        requested=vertical_code,
        using="kiosco_almacen",
    )

    default_path = _HEURISTICS_DIR / "kiosco_almacen.json"
    if default_path.exists():
        try:
            return _config_from_json(json.loads(default_path.read_text(encoding="utf-8")))
        except (KeyError, ValueError, json.JSONDecodeError):
            pass

    return _DEFAULT_CONFIG


def load_margin_benchmark(vertical_code: str) -> MarginBenchmark:
    """Compatibilidad para callers que todavía solo necesitan margen."""
    return load_vertical_heuristics(vertical_code).margin
