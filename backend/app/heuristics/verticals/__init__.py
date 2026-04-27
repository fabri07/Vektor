"""Vertical-specific heuristic config for the Health Engine."""

from dataclasses import dataclass


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
    """

    critical_below: float   # margin below this → CRITICAL zone
    warning_below: float    # margin below this → WARNING zone
    healthy_min: float      # margin at or above this → healthy
    healthy_max: float      # margin at or above this → excellent


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
    business_type: str
    cash_health: CashHealthBenchmark
    margin: MarginBenchmark
    inventory: InventoryBenchmark
    supplier: SupplierBenchmark
    seasonality: str
