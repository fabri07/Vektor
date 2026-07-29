"""Heuristic rules for decoracion_hogar vertical."""

from decimal import Decimal

from app.domain.verticals import Vertical
from app.heuristics.base import BaseHeuristicRuleSet, DimensionThresholds, VerticalRules


class DecoracionHogarHeuristicRuleSet(BaseHeuristicRuleSet):
    """
    Home decoration businesses have seasonal demand peaks and higher ticket sizes.
    Key risks: inventory build-up, seasonal cash troughs, credit sales.
    """

    @property
    def vertical(self) -> Vertical:
        return Vertical.DECORACION_HOGAR

    def get_rules(self) -> VerticalRules:
        return VerticalRules(
            vertical=Vertical.DECORACION_HOGAR,
            liquidity_thresholds=DimensionThresholds(
                critical_below=Decimal("0.8"),
                warning_below=Decimal("1.2"),
                good_above=Decimal("2.0"),
                excellent_above=Decimal("3.5"),
            ),
            profitability_thresholds=DimensionThresholds(
                critical_below=Decimal("0.10"),
                warning_below=Decimal("0.20"),
                good_above=Decimal("0.35"),
                excellent_above=Decimal("0.50"),
            ),
            cost_control_thresholds=DimensionThresholds(
                critical_below=Decimal("0.55"),
                warning_below=Decimal("0.70"),
                good_above=Decimal("0.82"),
                excellent_above=Decimal("0.90"),
            ),
            sales_momentum_thresholds=DimensionThresholds(
                critical_below=Decimal("-0.30"),
                warning_below=Decimal("-0.10"),
                good_above=Decimal("0.05"),
                excellent_above=Decimal("0.20"),
            ),
            debt_coverage_thresholds=DimensionThresholds(
                critical_below=Decimal("0.8"),
                warning_below=Decimal("1.2"),
                good_above=Decimal("2.0"),
                excellent_above=Decimal("3.0"),
            ),
        )
