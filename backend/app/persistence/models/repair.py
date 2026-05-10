"""ORM models for data repair audit: data_repair_runs, data_repair_items."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import PGJSONB, Base, UUIDPrimaryKeyMixin


class DataRepairRun(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "data_repair_runs"

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    repair_type: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="PENDING")
    dry_run: Mapped[bool] = mapped_column(nullable=False, default=True)
    source_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("data_repair_runs.id", ondelete="SET NULL"),
        nullable=True,
    )

    candidates_found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sales_detected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sales_voided: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    products_detected: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    products_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    products_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    products_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    details_json: Mapped[dict[str, Any] | None] = mapped_column(PGJSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','RUNNING','COMPLETED','FAILED','APPROVED','APPLIED')",
            name="ck_repair_runs_status",
        ),
    )

    def __repr__(self) -> str:
        return f"<DataRepairRun id={self.id} type={self.repair_type!r} dry_run={self.dry_run} status={self.status!r}>"


class DataRepairItem(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "data_repair_items"

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("data_repair_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    source_file_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    sale_entry_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    product_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    before_json: Mapped[dict[str, Any] | None] = mapped_column(PGJSONB, nullable=True)
    after_json: Mapped[dict[str, Any] | None] = mapped_column(PGJSONB, nullable=True)
    confidence: Mapped[str] = mapped_column(String(10), nullable=False, default="MEDIUM")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        CheckConstraint(
            "action IN ('VOID_SALE','CREATE_PRODUCT','UPDATE_PRODUCT')",
            name="ck_repair_items_action",
        ),
    )

    def __repr__(self) -> str:
        return f"<DataRepairItem run={self.run_id} action={self.action!r} tenant={self.tenant_id}>"
