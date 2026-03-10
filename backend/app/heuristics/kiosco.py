"""Heuristic rules for kiosco vertical."""

from decimal import Decimal

from app.heuristics.base import BaseHeuristicRuleSet, DimensionThresholds, VerticalRules


class KioscoHeuristicRuleSet(BaseHeuristicRuleSet):
    """
    Kioscos operate with high transaction frequency and thin margins.
    Key risks: cash flow gaps, shrinkage, supplier payment delays.
    """

    @property
    def vertical(self) -> str:
        return "kiosco"

    def get_rules(self) -> VerticalRules:
        return VerticalRules(
            vertical="kiosco",
            liquidity_thresholds=DimensionThresholds(
                critical_below=Decimal("0.5"),
                warning_below=Decimal("1.0"),
                good_above=Decimal("2.0"),
                excellent_above=Decimal("3.0"),
            ),
            profitability_thresholds=DimensionThresholds(
                critical_below=Decimal("0.05"),   # 5% gross margin
                warning_below=Decimal("0.12"),    # 12%
                good_above=Decimal("0.20"),       # 20%
                excellent_above=Decimal("0.30"),  # 30%
            ),
            cost_control_thresholds=DimensionThresholds(
                critical_below=Decimal("0.60"),   # expenses/revenue ratio
                warning_below=Decimal("0.75"),
                good_above=Decimal("0.85"),
                excellent_above=Decimal("0.92"),
            ),
            sales_momentum_thresholds=DimensionThresholds(
                critical_below=Decimal("-0.20"),  # -20% WoW
                warning_below=Decimal("-0.05"),
                good_above=Decimal("0.05"),
                excellent_above=Decimal("0.15"),
            ),
            debt_coverage_thresholds=DimensionThresholds(
                critical_below=Decimal("0.5"),
                warning_below=Decimal("1.0"),
                good_above=Decimal("1.5"),
                excellent_above=Decimal("2.5"),
            ),
        )
