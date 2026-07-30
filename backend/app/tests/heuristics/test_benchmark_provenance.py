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
from dataclasses import replace
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

    if data["margin"]["source"] is None:
        assert config.margin.source is None
        assert config.margin.provenance is BenchmarkProvenance.STATIC_PROVISIONAL
    else:
        assert config.margin.source is not None
        assert config.margin.source.institucion == data["margin"]["source"]["institucion"]
        assert config.margin.provenance is BenchmarkProvenance.STATIC_SOURCED


@pytest.mark.parametrize("vertical", list(Vertical))
def test_la_fuente_es_por_bloque_y_no_se_contagia(vertical: Vertical) -> None:
    """Cada bloque afirma solo lo suyo.

    El caso que motivó separarlo: los informes sectoriales documentan márgenes y
    rotación pero no días de cobertura de caja. Con una sola fuente por rubro, el
    sistema afirmaría que la caja está respaldada solo porque el margen lo está.
    """
    config = load_vertical_heuristics(vertical)
    data = json.loads(
        (_HEURISTICS_DIR / f"{vertical.value}.json").read_text(encoding="utf-8")
    )

    for bloque, benchmark in (
        ("cash_health", config.cash_health),
        ("margin", config.margin),
        ("inventory", config.inventory),
        ("supplier", config.supplier),
    ):
        declarada = data[bloque]["source"]
        if declarada is None:
            assert benchmark.source is None, f"{vertical.value}/{bloque} inventó una fuente"
        else:
            assert benchmark.source is not None
            assert benchmark.source.institucion == declarada["institucion"]


# ── min_healthy_suppliers ─────────────────────────────────────────────────────


#: Rubros que YA existían cuando `min_healthy_suppliers` se hizo explícito. El
#: test de abajo se limita a estos a propósito: los rubros nuevos declaran su
#: mínimo real, que en algunos casos contradice la vieja deducción (una
#: verdulería es muy sensible al quiebre Y compra en dos puestos). Parametrizar
#: sobre `Vertical` entero convertiría este test de no-regresión en un candado
#: contra la corrección que motivó todo el cambio.
_RUBROS_PREVIOS_AL_CAMPO = (
    Vertical.KIOSCO_ALMACEN,
    Vertical.LIMPIEZA,
    Vertical.DECORACION_HOGAR,
)


@pytest.mark.parametrize("vertical", _RUBROS_PREVIOS_AL_CAMPO)
@pytest.mark.parametrize("cantidad", range(1, 11))
def test_el_minimo_de_proveedores_no_cambia_el_score_de_los_rubros_existentes(
    vertical: Vertical, cantidad: int
) -> None:
    """`min_healthy_suppliers` reproduce EXACTO lo que se deducía antes.

    El mínimo sano salía de `stockout_sensitivity` (alta/muy_alta → 4, resto →
    3). Hacerlo explícito es un refactor, no una recalibración: los valores que
    llevan los tres rubros existentes son los que esa deducción daba, así que
    ningún tenant se mueve un punto. Si alguien toca uno de esos tres JSON
    creyendo que es cosmético, este test lo frena.
    """
    from app.heuristics.health_engine import _score_supplier

    config = load_vertical_heuristics(vertical)
    deducido = 4 if config.supplier.stockout_sensitivity.lower() in {"alta", "muy_alta"} else 3
    esperado = _score_supplier(
        cantidad,
        replace(config, supplier=replace(config.supplier, min_healthy_suppliers=deducido)),
    )

    assert _score_supplier(cantidad, config) == esperado
