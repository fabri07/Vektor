"""Vertical-specific margin benchmarks for the Health Engine."""

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
