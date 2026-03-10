"""
Abstract base for vertical-specific heuristic rule sets.

Each vertical (kiosco, decoracion_hogar, limpieza) implements its own
thresholds and weights for the five financial dimensions.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal


@dataclass
class DimensionThresholds:
    """Thresholds that map a raw metric to a 0–100 score."""

    critical_below: Decimal   # score → CRITICAL if metric < this
    warning_below: Decimal    # score → WARNING if metric < this
    good_above: Decimal       # score → GOOD if metric >= this
    excellent_above: Decimal  # score → EXCELLENT if metric >= this


@dataclass
class VerticalRules:
    """Complete rule configuration for a business vertical."""

    vertical: str
    liquidity_thresholds: DimensionThresholds
    profitability_thresholds: DimensionThresholds
    cost_control_thresholds: DimensionThresholds
    sales_momentum_thresholds: DimensionThresholds
    debt_coverage_thresholds: DimensionThresholds


class BaseHeuristicRuleSet(ABC):
    """Interface that every vertical heuristic must implement."""

    @property
    @abstractmethod
    def vertical(self) -> str: ...

    @abstractmethod
    def get_rules(self) -> VerticalRules: ...

    def score_from_metric(
        self, value: Decimal, thresholds: DimensionThresholds
    ) -> Decimal:
        """
        Map a raw financial metric to a 0–100 score using linear interpolation
        between the defined threshold points.
        """
        if value < thresholds.critical_below:
            return Decimal("0")
        if value >= thresholds.excellent_above:
            return Decimal("100")
        if value >= thresholds.good_above:
            # Interpolate between good and excellent
            span = thresholds.excellent_above - thresholds.good_above
            pos = value - thresholds.good_above
            return Decimal("75") + (pos / span * Decimal("25"))
        if value >= thresholds.warning_below:
            span = thresholds.good_above - thresholds.warning_below
            pos = value - thresholds.warning_below
            return Decimal("50") + (pos / span * Decimal("25"))
        # Between critical and warning
        span = thresholds.warning_below - thresholds.critical_below
        pos = value - thresholds.critical_below
        return pos / span * Decimal("50")
