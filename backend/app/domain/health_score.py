"""
HealthScore domain entity and scoring value objects.

The health score is the core output of the Health Engine.
It is only recalculated when underlying data changes (not on every request).
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4


class ScoreLevel(StrEnum):
    CRITICAL = "critical"       # 0–39
    WARNING = "warning"         # 40–59
    FAIR = "fair"               # 60–74
    GOOD = "good"               # 75–89
    EXCELLENT = "excellent"     # 90–100


class ScoreDimension(StrEnum):
    LIQUIDITY = "liquidity"
    PROFITABILITY = "profitability"
    COST_CONTROL = "cost_control"
    SALES_MOMENTUM = "sales_momentum"
    DEBT_COVERAGE = "debt_coverage"


@dataclass(frozen=True)
class DimensionScore:
    """Score for a single financial dimension."""

    dimension: ScoreDimension
    value: Decimal           # 0.00 – 100.00
    weight: Decimal          # 0.00 – 1.00; all dimensions must sum to 1
    explanation: str

    def __post_init__(self) -> None:
        if not (Decimal("0") <= self.value <= Decimal("100")):
            raise ValueError(f"Score value must be between 0 and 100, got {self.value}")
        if not (Decimal("0") <= self.weight <= Decimal("1")):
            raise ValueError(f"Weight must be between 0 and 1, got {self.weight}")

    @property
    def weighted_value(self) -> Decimal:
        return self.value * self.weight


@dataclass
class HealthScore:
    """
    Composite financial health score for a tenant.

    Invariant: total_score == sum(d.weighted_value for d in dimensions)
    """

    tenant_id: UUID
    total_score: Decimal
    level: ScoreLevel
    dimensions: list[DimensionScore]
    snapshot_date: datetime
    triggered_by: str           # e.g. "sale_entry_created", "expense_updated"
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=datetime.utcnow)

    @classmethod
    def from_dimensions(
        cls,
        tenant_id: UUID,
        dimensions: list[DimensionScore],
        snapshot_date: datetime,
        triggered_by: str,
    ) -> "HealthScore":
        total = sum((d.weighted_value for d in dimensions), Decimal("0"))
        return cls(
            tenant_id=tenant_id,
            total_score=total,
            level=cls._classify(total),
            dimensions=dimensions,
            snapshot_date=snapshot_date,
            triggered_by=triggered_by,
        )

    @staticmethod
    def _classify(score: Decimal) -> ScoreLevel:
        if score < 40:
            return ScoreLevel.CRITICAL
        if score < 60:
            return ScoreLevel.WARNING
        if score < 75:
            return ScoreLevel.FAIR
        if score < 90:
            return ScoreLevel.GOOD
        return ScoreLevel.EXCELLENT

    @property
    def is_critical(self) -> bool:
        return self.level == ScoreLevel.CRITICAL

    @property
    def needs_attention(self) -> bool:
        return self.level in (ScoreLevel.CRITICAL, ScoreLevel.WARNING)
