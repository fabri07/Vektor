"""Carga MarginBenchmark desde los JSONs de heurísticas. Fuente única de verdad."""

from __future__ import annotations

import json
from pathlib import Path

from app.heuristics.verticals import MarginBenchmark

_HEURISTICS_DIR = Path(__file__).resolve().parents[3] / "app" / "application" / "data" / "heuristics"

# Alias para vertical codes históricos o alternativos
_VERTICAL_ALIASES: dict[str, str] = {
    "kiosco": "kiosco_almacen",
    "deco": "decoracion_hogar",
    "deco_hogar": "decoracion_hogar",
    "cleaning": "limpieza",
}

# Fallbacks hardcodeados — solo si el JSON no existe
_FALLBACKS: dict[str, MarginBenchmark] = {
    "kiosco_almacen": MarginBenchmark(critical_below=0.10, warning_below=0.18, healthy_min=0.18, healthy_max=0.28),
    "decoracion_hogar": MarginBenchmark(critical_below=0.15, warning_below=0.30, healthy_min=0.30, healthy_max=0.45),
    "limpieza": MarginBenchmark(critical_below=0.10, warning_below=0.20, healthy_min=0.20, healthy_max=0.35),
}


def load_margin_benchmark(vertical_code: str) -> MarginBenchmark:
    """Carga MarginBenchmark del JSON del vertical. Fallback a hardcode si no existe.

    Normaliza aliases (ej. 'kiosco' → 'kiosco_almacen') para compatibilidad con
    códigos históricos o mal escritos.
    """
    normalized = _VERTICAL_ALIASES.get(vertical_code, vertical_code)
    json_path = _HEURISTICS_DIR / f"{normalized}.json"

    if json_path.exists():
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
            m = data["margin"]
            return MarginBenchmark(
                critical_below=float(m["critical_below"]),
                warning_below=float(m["warning_below"]),
                healthy_min=float(m["healthy_min"]),
                healthy_max=float(m["healthy_max"]),
            )
        except (KeyError, ValueError, json.JSONDecodeError):
            pass  # JSON malformado → caer al fallback

    # Fallback: buscar en hardcodes por código normalizado o el original
    fallback = _FALLBACKS.get(normalized) or _FALLBACKS.get(vertical_code)
    if fallback:
        return fallback

    # Default final: kiosco_almacen (rubro más común en Véktor)
    return _FALLBACKS["kiosco_almacen"]
