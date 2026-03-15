"""ORM models: business_profiles, business_snapshots, insights, action_suggestions, momentum_profiles.

Column names match the migration schema exactly.
BusinessProfile uses onboarding-friendly nullable fields for datos del negocio.
MomentumProfile uses tenant_id as primary key (1-to-1 with tenants).
"""  # noqa: E501

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import PGJSONB, Base, TimestampMixin, UUIDPrimaryKeyMixin


class BusinessProfile(TimestampMixin, Base):
    """
    Business profile created at registration with vertical_code.
    All onboarding fields (legal_name, cuit, etc.) are filled during onboarding — NOT at register.
    """

    __tablename__ = "business_profiles"

    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    vertical_code: Mapped[str] = mapped_column(Text, nullable=False)
    data_mode: Mapped[str] = mapped_column(Text, nullable=False, default="M0")
    data_confidence: Mapped[str] = mapped_column(Text, nullable=False, default="LOW")

    # Financial estimates — nullable, filled during onboarding
    monthly_sales_estimate_ars: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2), nullable=True
    )
    monthly_inventory_spend_estimate_ars: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2), nullable=True
    )
    monthly_fixed_expenses_estimate_ars: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2), nullable=True
    )
    cash_on_hand_estimate_ars: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2), nullable=True
    )
    supplier_count_estimate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    product_count_estimate: Mapped[int | None] = mapped_column(Integer, nullable=True)

    onboarding_completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    heuristic_profile_version: Mapped[str] = mapped_column(Text, nullable=False, default="v1")

    # ── Weekly report scheduling (schema F3-04) ───────────────────────────────
    # 0=Monday … 6=Sunday. v1: Celery Beat runs for all tenants with these defaults.
    # TODO: implementar scheduler por tenant usando weekly_report_day
    # y weekly_report_hour de business_profiles
    weekly_report_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    weekly_report_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)

    def __repr__(self) -> str:
        return f"<BusinessProfile tenant={self.tenant_id} vertical={self.vertical_code!r}>"


class BusinessSnapshot(UUIDPrimaryKeyMixin, Base):
    """
    Point-in-time snapshot of a tenant's business state.
    Calculated by the Business State Layer before the Health Engine runs.
    """

    __tablename__ = "business_snapshots"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    snapshot_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    snapshot_version: Mapped[str] = mapped_column(Text, nullable=False, default="v1")
    raw_inputs_json: Mapped[dict[str, Any] | None] = mapped_column(PGJSONB, nullable=True)
    data_completeness_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    data_mode: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence_level: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return f"<BusinessSnapshot tenant={self.tenant_id} date={self.snapshot_date}>"


class MomentumProfile(Base):
    """
    Momentum and streak metrics. tenant_id is the primary key (1-to-1 with tenant).
    No separate UUID — the tenant IS the key.
    """

    __tablename__ = "momentum_profiles"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        primary_key=True,
    )
    best_score_ever: Mapped[int | None] = mapped_column(Integer, nullable=True)
    best_score_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    active_goal_json: Mapped[dict[str, Any] | None] = mapped_column(PGJSONB, nullable=True)
    milestones_json: Mapped[list[Any]] = mapped_column(PGJSONB, nullable=False, default=list)
    estimated_value_protected_ars: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2), nullable=True
    )
    improving_streak_weeks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return f"<MomentumProfile tenant={self.tenant_id}>"


class Insight(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """AI/rule-generated insights for a tenant."""

    __tablename__ = "insights"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    insight_type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    severity_code: Mapped[str] = mapped_column(Text, nullable=False, default="MEDIUM")
    heuristic_version: Mapped[str] = mapped_column(Text, nullable=False, default="v1")

    def __repr__(self) -> str:
        return f"<Insight tenant={self.tenant_id} type={self.insight_type!r}>"


class ActionSuggestion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Actionable suggestions generated from insights / health score."""

    __tablename__ = "action_suggestions"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    insight_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("insights.id", ondelete="SET NULL"),
        nullable=True,
    )
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    risk_level: Mapped[str] = mapped_column(Text, nullable=False, default="LOW")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="SUGGESTED")

    def __repr__(self) -> str:
        return f"<ActionSuggestion tenant={self.tenant_id} status={self.status!r}>"
