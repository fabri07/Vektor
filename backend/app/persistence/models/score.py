"""ORM models: health_score_snapshots, heuristic_rule_sets, weekly_score_history."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import PGJSONB, Base, TimestampMixin, UUIDPrimaryKeyMixin


class HealthScoreSnapshot(UUIDPrimaryKeyMixin, Base):
    """
    Persisted health score snapshot.
    Only created when underlying data changes (not on every request).
    """

    __tablename__ = "health_score_snapshots"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    total_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    level: Mapped[str] = mapped_column(String(20), nullable=False)
    dimensions: Mapped[dict[str, Any]] = mapped_column(PGJSONB, nullable=False)  # DimensionScore[]
    triggered_by: Mapped[str] = mapped_column(String(100), nullable=False)
    snapshot_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return (
            f"<HealthScoreSnapshot tenant={self.tenant_id}"
            f" score={self.total_score} level={self.level!r}>"
        )


class HeuristicRuleSet(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """
    Versioned set of heuristic rules for a specific business vertical.
    Rules are serialized as JSONB to allow hot-reload without deploys.
    """

    __tablename__ = "heuristic_rule_sets"

    vertical: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    rules: Mapped[dict[str, Any]] = mapped_column(PGJSONB, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<HeuristicRuleSet vertical={self.vertical!r} v={self.version!r}>"


class WeeklyScoreHistory(UUIDPrimaryKeyMixin, Base):
    """Weekly aggregated score history for trend visualization."""

    __tablename__ = "weekly_score_history"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    week_start: Mapped[date] = mapped_column(Date, nullable=False)
    week_end: Mapped[date] = mapped_column(Date, nullable=False)
    avg_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    min_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    max_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    level: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return f"<WeeklyScoreHistory tenant={self.tenant_id} week={self.week_start}>"
