"""ORM models: operation_fingerprints y business_memory."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import PGJSONB, Base, TimestampMixin


class OperationFingerprint(Base):
    """Deduplicación de operaciones por hash SHA-256."""

    __tablename__ = "operation_fingerprints"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    fingerprint: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    action_type: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    executed_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    )

    __table_args__ = (
        sa.UniqueConstraint(
            "tenant_id", "fingerprint", name="uq_operation_fingerprints_tenant_fp"
        ),
    )


class BusinessMemory(Base):
    """Estado financiero resumido del negocio. Una fila por tenant."""

    __tablename__ = "business_memory"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        primary_key=True,
    )
    last_sale_amount: Mapped[Decimal | None] = mapped_column(
        sa.Numeric(12, 2), nullable=True
    )
    last_sale_at: Mapped[datetime | None] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    sales_today: Mapped[Decimal] = mapped_column(
        sa.Numeric(12, 2), nullable=False, default=Decimal("0")
    )
    sales_this_week: Mapped[Decimal] = mapped_column(
        sa.Numeric(12, 2), nullable=False, default=Decimal("0")
    )
    sales_this_month: Mapped[Decimal] = mapped_column(
        sa.Numeric(12, 2), nullable=False, default=Decimal("0")
    )
    expenses_today: Mapped[Decimal] = mapped_column(
        sa.Numeric(12, 2), nullable=False, default=Decimal("0")
    )
    products_in_stockout: Mapped[int] = mapped_column(
        sa.Integer(), nullable=False, default=0
    )
    recent_actions: Mapped[list[Any]] = mapped_column(
        PGJSONB, nullable=True, default=list
    )
    llm_context_summary: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    llm_context_updated_at: Mapped[datetime | None] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
        onupdate=sa.text("CURRENT_TIMESTAMP"),
    )
