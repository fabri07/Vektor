"""ORM model: cash_closes — cierre de caja diario (arqueo). Sprint 20."""

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Date, ForeignKey, Index, Numeric, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import PGJSONB, Base, TimestampMixin, UUIDPrimaryKeyMixin


class CashClose(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Arqueo de caja de un día: esperado (calculado) vs contado (ingresado).

    `expected_total_ars` se recomputa server-side al crear (ventas por método con
    neteo de efectivo). `difference_ars = counted - expected`. Un solo cierre por
    (tenant, fecha) — UniqueConstraint.
    """

    __tablename__ = "cash_closes"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    close_date: Mapped[date] = mapped_column(Date, nullable=False)
    expected_total_ars: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    counted_total_ars: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    difference_ars: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    # breakdown_by_method: {método: {"expected": float, "counted": float|None}}
    breakdown_by_method: Mapped[dict[str, Any]] = mapped_column(
        PGJSONB, nullable=False, default=dict
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    closed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "close_date", name="uq_cash_closes_tenant_date"),
        Index("ix_cash_closes_tenant_date", "tenant_id", "close_date"),
    )

    def __repr__(self) -> str:
        return (
            f"<CashClose tenant={self.tenant_id} date={self.close_date} "
            f"diff={self.difference_ars}>"
        )
