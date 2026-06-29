"""stats_enrichment — glue entre los handlers consultivos y stats_engine.

Convierte una serie/distribución ya cargada por el agente en un sub-dict `analisis`
listo para el narrador. NUNCA llama LLM (igual que stats_engine).

Dos formas:
  - enrich_timeseries: dominios con serie temporal real (ventas, gastos, caja).
  - enrich_distribution: agregados por entidad (stock, proveedores, clientes,
    marketing) — usa concentración, NO cuartiles ni proyección (el orden de las
    entidades es arbitrario, una pendiente no tendría sentido).

Guardas de muestra heredadas verbatim de stats_engine (no-invention rule).
"""
from __future__ import annotations

from typing import Any

from app.application.agents.shared import stats_engine

# Proyección solo con señal real: mínimo de días activos y cobertura del período.
_MIN_PROJECT_ACTIVE = 7
_MIN_ACTIVE_RATIO = 0.5


def enrich_timeseries(series: list[float], *, days_ahead: int = 15) -> dict[str, Any]:
    """Estadística + proyección + anomalías sobre una serie diaria (con zero-fill).

    - estadistica: describe_sales sobre la serie completa (incluye días cerrados →
      promedio por día calendario). dias_activos/dias_totales dan contexto.
    - anomalias: detect_anomalies sobre días ACTIVOS (sin ceros) → evita falsos
      "caida" por domingos/feriados.
    - proyeccion: project_sales sobre días activos, SOLO si hay >= 7 días activos y
      la actividad cubre >= 50% del período; si no, insufficient_data (los ceros
      estructurales sesgarían la tendencia).
    """
    dias_totales = len(series)
    activos = [v for v in series if v != 0.0]
    dias_activos = len(activos)

    estadistica = stats_engine.describe_sales(series)
    anomalias = stats_engine.detect_anomalies(activos)

    ratio = (dias_activos / dias_totales) if dias_totales else 0.0
    if dias_activos >= _MIN_PROJECT_ACTIVE and ratio >= _MIN_ACTIVE_RATIO:
        proyeccion = stats_engine.project_sales(activos, days_ahead=days_ahead)
    else:
        proyeccion = {
            "status": "insufficient_data",
            "n": dias_activos,
            "min_required": _MIN_PROJECT_ACTIVE,
        }

    return {
        "estadistica": estadistica,
        "proyeccion": proyeccion,
        "anomalias": anomalias,
        "dias_activos": dias_activos,
        "dias_totales": dias_totales,
    }


def enrich_distribution(values: list[float]) -> dict[str, Any]:
    """Concentración (Pareto) sobre montos por entidad. Sin proyección ni cuartiles."""
    return {"concentracion": stats_engine.concentration(values)}
