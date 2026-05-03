"""ORM model: durable chat session memory events."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import Base, UUIDPrimaryKeyMixin


CHAT_SESSION_ENTRY_TYPES = (
    "DATA_LOADED",
    "DATA_REJECTED",
    "FILE_UPLOADED",
    "INTENT_DETECTED",
    "QUERY_ANSWERED",
)


class ChatSessionLog(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "chat_session_log"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    entry_type: Mapped[str] = mapped_column(String(30), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    entity_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    pending_action_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pending_actions.id", ondelete="SET NULL"),
        nullable=True,
    )
    uploaded_file_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("uploaded_files.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        CheckConstraint(
            "entry_type IN ('DATA_LOADED', 'DATA_REJECTED', 'FILE_UPLOADED', "
            "'INTENT_DETECTED', 'QUERY_ANSWERED')",
            name="ck_chat_session_log_entry_type",
        ),
        Index("ix_chat_session_log_tenant_created", "tenant_id", "created_at"),
        Index("ix_chat_session_log_tenant_entry_type", "tenant_id", "entry_type"),
    )
