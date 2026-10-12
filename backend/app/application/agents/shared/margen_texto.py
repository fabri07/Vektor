"""Cómo se le dice el margen al LLM (M·2b-2). Sólo formatea: no calcula.

El número sale de FactsService (``facts_provider.margenes_del_tenant``). Acá se
asegura que un margen ESTIMADO llegue dicho como estimado y uno AUSENTE como
«sin medir» con su motivo, nunca como 0.
"""

from __future__ import annotations

from typing import Any

from app.application.services.facts_service import BusinessFact


def describir_margen_bruto(f: BusinessFact) -> str:
    """«32.5% (confirmado; …)», «32.5% ESTIMADO (…)» o «sin medir (…)»."""
    d = f.detail or {}
    if f.value is None:
        return f"sin medir ({d.get('motivo') or 'sin ventas en el período'})"
    cobertura = (
        f"cobertura de costo confirmada {d.get('cobertura_confirmada_pct')}%, "
        f"con estimaciones {d.get('cobertura_total_pct')}%"
    )
    if d.get("estado") == "estimado":
        return f"{f.value}% ESTIMADO ({d.get('motivo')}; {cobertura})"
    return f"{f.value}% ({cobertura})"


def datos_margen_bruto(f: BusinessFact) -> dict[str, Any]:
    """Los campos del margen bruto para un ``structured_data`` JSON-serializable."""
    d = f.detail or {}
    return {
        "margen_bruto_pct": f.value,
        "margen_bruto_estado": d.get("estado"),
        "margen_bruto_motivo": d.get("motivo"),
        "cobertura_costo_confirmada_pct": d.get("cobertura_confirmada_pct"),
        "cobertura_costo_total_pct": d.get("cobertura_total_pct"),
    }
