"""La confianza del score contempla la calidad de la vara, no solo la de los datos.

Antes `confidence_level` copiaba tal cual la completitud de los datos del tenant.
Un negocio con ventas, costos, caja, productos y proveedores cargados salía
`HIGH` aunque el umbral contra el que se lo medía no tuviera ningún fundamento
declarado. El sistema fallaba ruidosamente si faltaba configuración, pero mentía
en silencio cuando la configuración existía y era débil.
"""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from app.domain.verticals import Vertical
from app.heuristics.health_engine import calculate_health_score
from app.heuristics.verticals import (
    BENCHMARK_CONFIDENCE,
    BenchmarkProvenance,
    MarginBenchmark,
    weakest_confidence,
)
from app.heuristics.verticals.loader import load_vertical_heuristics
from app.state.business_state_service import BusinessState

_HEURISTICS_DIR = (
    Path(__file__).resolve().parents[2] / "application" / "data" / "heuristics"
)


def _state(confidence_level: str = "HIGH", completeness: float = 90.0) -> BusinessState:
    return BusinessState(
        snapshot_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        vertical_code=Vertical.KIOSCO_ALMACEN.value,
        data_completeness_score=completeness,
        confidence_level=confidence_level,
        monthly_sales_est=Decimal("100000"),
        monthly_inventory_cost_est=Decimal("60000"),
        monthly_fixed_expenses_est=Decimal("20000"),
        cash_on_hand_est=Decimal("40000"),
        product_count=6,
        supplier_count=3,
        products=[],
        main_concern=None,
        cash_source="onboarding",
    )


def _benchmark(provenance: BenchmarkProvenance) -> MarginBenchmark:
    return MarginBenchmark(
        critical_below=0.05,
        warning_below=0.10,
        healthy_min=0.10,
        healthy_max=0.30,
        provenance=provenance,
    )


# ── weakest_confidence ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("niveles", "esperado"),
    [
        (("HIGH", "HIGH"), "HIGH"),
        (("HIGH", "MEDIUM"), "MEDIUM"),
        (("MEDIUM", "HIGH"), "MEDIUM"),
        (("HIGH", "LOW"), "LOW"),
        (("LOW", "MEDIUM"), "LOW"),
    ],
)
def test_weakest_confidence_toma_el_eslabon_mas_debil(
    niveles: tuple[str, ...], esperado: str
) -> None:
    assert weakest_confidence(*niveles) == esperado


# ── Confianza efectiva del score ──────────────────────────────────────────────


def test_datos_impecables_contra_benchmark_provisional_no_dan_high() -> None:
    """El caso que motivó todo esto."""
    resultado = calculate_health_score(
        _state(confidence_level="HIGH"),
        benchmark=_benchmark(BenchmarkProvenance.STATIC_PROVISIONAL),
    )

    assert resultado.data_confidence == "HIGH"
    assert resultado.benchmark_confidence == "MEDIUM"
    assert resultado.confidence_level == "MEDIUM"
    assert resultado.benchmark_provenance is BenchmarkProvenance.STATIC_PROVISIONAL


def test_benchmark_con_fuente_no_levanta_la_confianza_de_los_datos() -> None:
    """El mínimo corta para los dos lados: una vara sólida no arregla datos flojos."""
    resultado = calculate_health_score(
        _state(confidence_level="LOW", completeness=30.0),
        benchmark=_benchmark(BenchmarkProvenance.STATIC_SOURCED),
    )

    assert resultado.benchmark_confidence == "HIGH"
    assert resultado.confidence_level == "LOW"


def test_todo_solido_si_da_high() -> None:
    """Contrapeso: sin esto, devolver siempre MEDIUM pasaría los tests de arriba."""
    resultado = calculate_health_score(
        _state(confidence_level="HIGH"),
        benchmark=_benchmark(BenchmarkProvenance.STATIC_SOURCED),
    )

    assert resultado.confidence_level == "HIGH"


def test_el_objetivo_declarado_por_el_dueno_aporta_confianza_alta() -> None:
    """Un override no tiene respaldo sectorial, pero es el número que EL declara."""
    resultado = calculate_health_score(
        _state(confidence_level="HIGH"),
        benchmark=_benchmark(BenchmarkProvenance.TENANT_OVERRIDE),
    )

    assert resultado.confidence_level == "HIGH"
    assert resultado.benchmark_provenance is BenchmarkProvenance.TENANT_OVERRIDE


def test_toda_procedencia_tiene_confianza_asignada() -> None:
    """Un miembro nuevo del enum sin confianza reventaría con KeyError en runtime."""
    for provenance in BenchmarkProvenance:
        assert provenance in BENCHMARK_CONFIDENCE
        assert BENCHMARK_CONFIDENCE[provenance] in ("LOW", "MEDIUM", "HIGH")


# ── Procedencia de los JSON ───────────────────────────────────────────────────


@pytest.mark.parametrize("vertical", list(Vertical))
def test_la_procedencia_del_json_coincide_con_su_fuente_declarada(
    vertical: Vertical,
) -> None:
    """Con fuente → sourced; sin fuente → provisional. Sin zona gris."""
    data = json.loads(
        (_HEURISTICS_DIR / f"{vertical.value}.json").read_text(encoding="utf-8")
    )
    config = load_vertical_heuristics(vertical)

    if data["benchmark_source"] is None:
        assert config.benchmark_source is None
        assert config.margin.provenance is BenchmarkProvenance.STATIC_PROVISIONAL
    else:
        assert config.benchmark_source is not None
        assert config.benchmark_source.institucion == data["benchmark_source"]["institucion"]
        assert config.margin.provenance is BenchmarkProvenance.STATIC_SOURCED
