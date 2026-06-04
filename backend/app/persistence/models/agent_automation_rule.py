"""ORM model: agent automation rules.

Per-user, per-tenant consent rules that allow future matching agent actions to
execute without asking for approval again.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import PGJSONB, Base, UUIDPrimaryKeyMixin


class AgentAutomationRule(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "agent_automation_rules"
    __table_args__ = (
        Index(
            "ix_agent_automation_rules_tenant_user_enabled",
            "tenant_id",
            "user_id",
            "enabled",
        ),
        Index("ix_agent_automation_rules_tenant_rule_key", "tenant_id", "rule_key"),
        Index(
            "uq_agent_automation_rules_active_rule",
            "tenant_id",
            "user_id",
            "rule_key",
            unique=True,
            postgresql_where=text("enabled = true"),
            sqlite_where=text("enabled = 1"),
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_name: Mapped[str] = mapped_column(Text, nullable=False)
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    external_system: Mapped[str | None] = mapped_column(Text, nullable=True)
    rule_key: Mapped[str] = mapped_column(Text, nullable=False)
    criteria: Mapped[dict[str, Any]] = mapped_column(PGJSONB, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    risk_level: Mapped[str] = mapped_column(Text, nullable=False)
    created_from_pending_action_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pending_actions.id", ondelete="SET NULL"),
        nullable=True,
    )
    last_executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
