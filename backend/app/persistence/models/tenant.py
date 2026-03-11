"""ORM models: tenants, subscriptions.

Column names match the migration schema exactly.
"""

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Integer, Numeric, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.persistence.db.base import Base, TimestampMixin


class Tenant(TimestampMixin, Base):
    __tablename__ = "tenants"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    legal_name: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False, default="ARS")
    pricing_reference_mode: Mapped[str] = mapped_column(Text, nullable=False, default="MEP")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="ACTIVE")

    # Relationships
    subscriptions: Mapped[list["Subscription"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )
    users: Mapped[list["User"]] = relationship(  # type: ignore[name-defined]  # noqa: F821
        "User", back_populates="tenant", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Tenant id={self.tenant_id} display_name={self.display_name!r}>"


class Subscription(TimestampMixin, Base):
    __tablename__ = "subscriptions"

    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    plan_code: Mapped[str] = mapped_column(Text, nullable=False, default="FREE")
    plan_price_usd_reference: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    plan_price_ars: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    billing_index_reference: Mapped[str] = mapped_column(Text, nullable=False, default="MEP")
    seats_included: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="ACTIVE")
    current_period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    current_period_end: Mapped[date | None] = mapped_column(Date, nullable=True)

    tenant: Mapped["Tenant"] = relationship(back_populates="subscriptions")

    def __repr__(self) -> str:
        return f"<Subscription tenant={self.tenant_id} plan={self.plan_code!r}>"
