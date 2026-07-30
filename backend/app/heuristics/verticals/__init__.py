"""Vertical-specific heuristic config for the Health Engine."""

from dataclasses import dataclass
from enum import StrEnum

from app.domain.verticals import Vertical


class BenchmarkProvenance(StrEnum):
    """De dónde salió la vara contra la que se mide un negocio.

    Véktor ya fallaba ruidosamente ante configuración FALTANTE (un vertical
    desconocido levanta, un JSON incompleto levanta). Lo que no tenía era forma
    de distinguir configuración PRESENTE pero mal fundada — y un umbral con
    fundamento débil no se ve distinto de uno sólido cuando el score sale.
    """

    #: Umbral del JSON del vertical con fuente sectorial declarada (cámara + año).
    STATIC_SOURCED = "static_sourced"
    #: Umbral del JSON sin fuente documentada. Sirve para operar, no para afirmar.
    STATIC_PROVISIONAL = "static_provisional"
    #: El objetivo de margen que declaró el dueño en /settings. Es SU número.
    TENANT_OVERRIDE = "tenant_override"
    #: Percentiles de la muestra cross-tenant. Hoy NO puntúa: ver
    #: `ObservedMarginDistribution` en el repositorio de analytics.
    DATA_DRIVEN = "data_driven"


#: Confianza que aporta cada procedencia. Un benchmark provisional no puede
#: sostener un `HIGH`: el negocio puede tener datos impecables y aun así estar
#: siendo medido contra un umbral que nadie fundamentó.
BENCHMARK_CONFIDENCE: dict[BenchmarkProvenance, str] = {
    BenchmarkProvenance.STATIC_SOURCED: "HIGH",
    BenchmarkProvenance.TENANT_OVERRIDE: "HIGH",
    BenchmarkProvenance.STATIC_PROVISIONAL: "MEDIUM",
    # Inalcanzable hoy (el data-driven no puntúa). Si vuelve, vuelve en LOW hasta
    # que la muestra cuente negocios distintos y no eventos de recálculo.
    BenchmarkProvenance.DATA_DRIVEN: "LOW",
}

#: Orden de las confianzas, de menor a mayor. `LOW` es el que gatea la regla de
#: no-invención (empty state en vez de análisis).
_CONFIDENCE_ORDER: dict[str, int] = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


def weakest_confidence(*niveles: str) -> str:
    """El más bajo de varios niveles de confianza.

    La confianza efectiva de un score es la del eslabón más débil: datos
    completos medidos contra una vara sin fundamento no son un diagnóstico
    confiable, y una vara impecable sobre datos incompletos tampoco.
    """
    return min(niveles, key=lambda n: _CONFIDENCE_ORDER.get(n, 0))


@dataclass(frozen=True)
class BenchmarkSource:
    """Referencia sectorial de la que salieron los umbrales de un vertical.

    Su ausencia (`None` en el JSON) no es un detalle de documentación: es lo que
    marca al benchmark como provisional y le baja la confianza al score.
    """

    institucion: str
    referencia: str
    revisado_en: str


@dataclass(frozen=True)
class MarginBenchmark:
    """
    Margin thresholds for a business vertical.

    The estimated net margin is mapped to a 0-100 score using five bands:
        [below critical]             → 0-14
        [critical_below, warning_below) → 15-39
        [warning_below, healthy_min)    → 40-69   (may be zero-width)
        [healthy_min, healthy_max)      → 70-89
        [healthy_max, above]            → 90-100

    `provenance` no tiene default A PROPÓSITO: obliga a cada constructor a
    declarar de dónde sacó los números. Un benchmark sin procedencia no compila.
    """

    critical_below: float  # margin below this → CRITICAL zone
    warning_below: float  # margin below this → WARNING zone
    healthy_min: float  # margin at or above this → healthy
    healthy_max: float  # margin at or above this → excellent
    provenance: BenchmarkProvenance

    @property
    def confidence(self) -> str:
        """Confianza que aporta este benchmark — HIGH | MEDIUM | LOW."""
        return BENCHMARK_CONFIDENCE[self.provenance]


@dataclass(frozen=True)
class CashHealthBenchmark:
    healthy_days_min: float
    warning_days_min: float
    critical_days_below: float


@dataclass(frozen=True)
class InventoryBenchmark:
    rotation_days_min: float
    rotation_days_max: float
    overstock_tolerance: str


@dataclass(frozen=True)
class SupplierBenchmark:
    reorder_frequency: str
    stockout_sensitivity: str


@dataclass(frozen=True)
class VerticalHeuristicConfig:
    business_type: Vertical
    #: Referencia sectorial de los umbrales, o None si el rubro no la declara.
    benchmark_source: BenchmarkSource | None
    cash_health: CashHealthBenchmark
    margin: MarginBenchmark
    inventory: InventoryBenchmark
    supplier: SupplierBenchmark
    seasonality: str
