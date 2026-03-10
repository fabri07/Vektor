"""ORM models: business_profiles, business_snapshots, insights, action_suggestions, momentum_profiles."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text, Boolean
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.persistence.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class BusinessProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "business_profiles"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,       # one profile per tenant
        index=True,
    )
    legal_name: Mapped[str] = mapped_column(String(300), nullable=False)
    trade_name: Mapped[str] = mapped_column(String(300), nullable=False)
    cuit: Mapped[str] = mapped_column(String(20), nullable=False)
    vertical: Mapped[str] = mapped_column(String(50), nullable=False)
    size: Mapped[str] = mapped_column(String(20), nullable=False, default="micro")
    province: Mapped[str] = mapped_column(String(100), nullable=False)
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    employee_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    monthly_rent: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)

    def __repr__(self) -> str:
        return f"<BusinessProfile tenant={self.tenant_id} name={self.trade_name!r}>"


class BusinessSnapshot(UUIDPrimaryKeyMixin, Base):
    """
    Point-in-time snapshot of a tenant's business state.
    Calculated by the Business State Layer before the Health Engine runs.
    """

    __tablename__ = "business_snapshots"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    snapshot_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    # Financial aggregates for the period
    total_revenue: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    total_expenses: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    gross_profit: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    avg_daily_sales: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    transaction_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Raw data used for score computation (flexible)
    raw_data: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return f"<BusinessSnapshot tenant={self.tenant_id} date={self.snapshot_date}>"


class MomentumProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Sales trend and momentum metrics."""

    __tablename__ = "momentum_profiles"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    week_over_week_growth: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    month_over_month_growth: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    avg_ticket: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    best_day_of_week: Mapped[str | None] = mapped_column(String(20), nullable=True)
    sales_trend: Mapped[str] = mapped_column(String(20), nullable=False, default="stable")
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return f"<MomentumProfile tenant={self.tenant_id}>"


class Insight(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """AI/rule-generated insights for a tenant."""

    __tablename__ = "insights"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)  # e.g. "cash_flow"
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="info")
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("business_snapshots.id", ondelete="SET NULL"),
        nullable=True,
    )

    def __repr__(self) -> str:
        return f"<Insight tenant={self.tenant_id} category={self.category!r}>"


class ActionSuggestion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Actionable suggestions generated from insights / health score."""

    __tablename__ = "action_suggestions"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    insight_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("insights.id", ondelete="SET NULL"),
        nullable=True,
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    priority: Mapped[str] = mapped_column(String(20), nullable=False, default="medium")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<ActionSuggestion tenant={self.tenant_id} status={self.status!r}>"
