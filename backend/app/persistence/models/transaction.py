"""ORM models: sales_entries, expense_entries, products."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import PGJSONB, Base, TimestampMixin, UUIDPrimaryKeyMixin


class SaleEntry(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "sales_entries"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("products.id", ondelete="SET NULL"),
        nullable=True,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    transaction_date: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    payment_method: Mapped[str] = mapped_column(String(30), nullable=False, default="cash")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    provenance: Mapped[str] = mapped_column(String(10), nullable=False, default="REAL")
    custom_fields: Mapped[dict[str, Any]] = mapped_column(
        PGJSONB, nullable=False, server_default="'{}'::jsonb", default=dict
    )
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    void_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    voided_by_repair_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint("provenance IN ('REAL', 'DEMO')", name="ck_sales_entries_provenance"),
        CheckConstraint(
            "void_reason IS NULL OR void_reason IN ("
            "'REPAIR_MISCLASSIFIED_IMPORT','USER_CANCELLED','DUPLICATE','MANUAL_ADMIN_VOID')",
            name="ck_sales_entries_void_reason",
        ),
        CheckConstraint(
            "voided_at IS NULL OR void_reason IS NOT NULL",
            name="ck_sales_entries_void_consistency",
        ),
        Index("ix_sales_entries_tenant_provenance", "tenant_id", "provenance"),
        Index("ix_sales_entries_tenant_voided_at", "tenant_id", "voided_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<SaleEntry tenant={self.tenant_id} amount={self.amount} date={self.transaction_date}>"  # noqa: E501
        )


class ExpenseEntry(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "expense_entries"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # FASE 3 (B1): vínculo opcional al producto del catálogo (compras de
    # mercadería/insumos). NULL si no se resolvió o es ambiguo. SET NULL al
    # borrar el producto: el gasto histórico se conserva.
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("products.id", ondelete="SET NULL"),
        nullable=True,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    # OPEX = gasto operativo; COGS = compra de mercadería (entra al stock).
    expense_type: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default="OPEX", default="OPEX"
    )
    transaction_date: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    is_recurring: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    payment_method: Mapped[str] = mapped_column(String(30), nullable=False, default="transfer")
    supplier_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    provenance: Mapped[str] = mapped_column(String(10), nullable=False, default="REAL")
    custom_fields: Mapped[dict[str, Any]] = mapped_column(
        PGJSONB, nullable=False, server_default="'{}'::jsonb", default=dict
    )
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    void_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)
    voided_by_repair_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    __table_args__ = (
        CheckConstraint("provenance IN ('REAL', 'DEMO')", name="ck_expense_entries_provenance"),
        CheckConstraint(
            "expense_type IN ('OPEX', 'COGS')", name="ck_expense_entries_expense_type"
        ),
        Index("ix_expense_entries_tenant_expense_type", "tenant_id", "expense_type"),
        CheckConstraint(
            "void_reason IS NULL OR void_reason IN ("
            "'REPAIR_MISCLASSIFIED_IMPORT','USER_CANCELLED','DUPLICATE','MANUAL_ADMIN_VOID')",
            name="ck_expense_entries_void_reason",
        ),
        CheckConstraint(
            "voided_at IS NULL OR void_reason IS NOT NULL",
            name="ck_expense_entries_void_consistency",
        ),
        Index("ix_expense_entries_tenant_provenance", "tenant_id", "provenance"),
        Index("ix_expense_entries_tenant_voided_at", "tenant_id", "voided_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<ExpenseEntry tenant={self.tenant_id}"
            f" amount={self.amount} category={self.category!r}>"
        )
