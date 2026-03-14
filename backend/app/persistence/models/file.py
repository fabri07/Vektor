"""ORM model: uploaded_files."""

import uuid
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import PGJSONB, Base, TimestampMixin, UUIDPrimaryKeyMixin

# Valid processing_status values
PROCESSING_STATUS_PENDING = "PENDING"
PROCESSING_STATUS_PROCESSING = "PROCESSING"
PROCESSING_STATUS_NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"
PROCESSING_STATUS_DONE = "DONE"
PROCESSING_STATUS_FAILED = "FAILED"


class UploadedFile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "uploaded_files"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
    )
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    s3_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    purpose: Mapped[str] = mapped_column(String(50), nullable=False)  # file_hint: ventas|gastos|stock|general
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="uploaded")

    # ── Ingestion pipeline fields ─────────────────────────────────────────────
    processing_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=PROCESSING_STATUS_PENDING
    )
    parsed_summary_json: Mapped[Any] = mapped_column(PGJSONB, nullable=True, default=None)

    def __repr__(self) -> str:
        return (
            f"<UploadedFile tenant={self.tenant_id} name={self.original_filename!r} "
            f"status={self.processing_status!r}>"
        )
