"""Unit tests for HealthScore domain entity."""

import uuid
from datetime import datetime
from decimal import Decimal

import pytest

from app.domain.health_score import (
    DimensionScore,
    HealthScore,
    ScoreDimension,
    ScoreLevel,
)


class TestDimensionScore:
    def test_weighted_value(self) -> None:
        ds = DimensionScore(
            dimension=ScoreDimension.LIQUIDITY,
            value=Decimal("80"),
            weight=Decimal("0.25"),
            explanation="test",
        )
        assert ds.weighted_value == Decimal("20.00")

    def test_invalid_value_raises(self) -> None:
        with pytest.raises(ValueError):
            DimensionScore(
                dimension=ScoreDimension.LIQUIDITY,
                value=Decimal("101"),
                weight=Decimal("0.25"),
                explanation="test",
            )

    def test_invalid_weight_raises(self) -> None:
        with pytest.raises(ValueError):
            DimensionScore(
                dimension=ScoreDimension.LIQUIDITY,
                value=Decimal("80"),
                weight=Decimal("1.5"),
                explanation="test",
            )


class TestHealthScore:
    def _make_dimensions(self, score: float) -> list[DimensionScore]:
        weight = Decimal("0.20")
        return [
            DimensionScore(
                dimension=dim,
                value=Decimal(str(score)),
                weight=weight,
                explanation="test",
            )
            for dim in ScoreDimension
        ]

    def test_level_critical(self) -> None:
        score = HealthScore.from_dimensions(
            tenant_id=uuid.uuid4(),
            dimensions=self._make_dimensions(20.0),
            snapshot_date=datetime.utcnow(),
            triggered_by="test",
        )
        assert score.level == ScoreLevel.CRITICAL
        assert score.is_critical

    def test_level_excellent(self) -> None:
        score = HealthScore.from_dimensions(
            tenant_id=uuid.uuid4(),
            dimensions=self._make_dimensions(95.0),
            snapshot_date=datetime.utcnow(),
            triggered_by="test",
        )
        assert score.level == ScoreLevel.EXCELLENT
        assert not score.needs_attention

    def test_needs_attention_warning(self) -> None:
        score = HealthScore.from_dimensions(
            tenant_id=uuid.uuid4(),
            dimensions=self._make_dimensions(50.0),
            snapshot_date=datetime.utcnow(),
            triggered_by="test",
        )
        assert score.level == ScoreLevel.WARNING
        assert score.needs_attention

    def test_total_score_is_sum_of_weighted(self) -> None:
        dims = self._make_dimensions(80.0)
        score = HealthScore.from_dimensions(
            tenant_id=uuid.uuid4(),
            dimensions=dims,
            snapshot_date=datetime.utcnow(),
            triggered_by="test",
        )
        expected = sum(d.weighted_value for d in dims)
        assert score.total_score == expected
