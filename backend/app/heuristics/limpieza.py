"""Heuristic rules for limpieza (cleaning supplies) vertical."""

from decimal import Decimal

from app.heuristics.base import BaseHeuristicRuleSet, DimensionThresholds, VerticalRules


class LimpiezaHeuristicRuleSet(BaseHeuristicRuleSet):
    """
    Cleaning product distributors/retailers. B2B/B2C mix with recurring clients.
    Key risks: supplier concentration, payment terms mismatch, thin margins.
    """

    @property
    def vertical(self) -> str:
        return "limpieza"

    def get_rules(self) -> VerticalRules:
        return VerticalRules(
            vertical="limpieza",
            liquidity_thresholds=DimensionThresholds(
                critical_below=Decimal("0.7"),
                warning_below=Decimal("1.1"),
                good_above=Decimal("1.8"),
                excellent_above=Decimal("3.0"),
            ),
            profitability_thresholds=DimensionThresholds(
                critical_below=Decimal("0.08"),
                warning_below=Decimal("0.15"),
                good_above=Decimal("0.25"),
                excellent_above=Decimal("0.40"),
            ),
            cost_control_thresholds=DimensionThresholds(
                critical_below=Decimal("0.58"),
                warning_below=Decimal("0.72"),
                good_above=Decimal("0.83"),
                excellent_above=Decimal("0.91"),
            ),
            sales_momentum_thresholds=DimensionThresholds(
                critical_below=Decimal("-0.15"),
                warning_below=Decimal("-0.05"),
                good_above=Decimal("0.03"),
                excellent_above=Decimal("0.12"),
            ),
            debt_coverage_thresholds=DimensionThresholds(
                critical_below=Decimal("0.6"),
                warning_below=Decimal("1.0"),
                good_above=Decimal("1.5"),
                excellent_above=Decimal("2.5"),
            ),
        )
