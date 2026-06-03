"""add agent automation rules

Revision ID: 20260426_0001
Revises: 20260424_0002
Create Date: 2026-04-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "20260426_0001"
down_revision: str | None = "20260424_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_automation_rules",
        sa.Column("id", UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("agent_name", sa.Text(), nullable=False),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column("external_system", sa.Text(), nullable=True),
        sa.Column("rule_key", sa.Text(), nullable=False),
        sa.Column("criteria", JSONB, nullable=False, server_default="{}"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("risk_level", sa.Text(), nullable=False),
        sa.Column(
            "created_from_pending_action_id",
            UUID(as_uuid=True),
            sa.ForeignKey("pending_actions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("last_executed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_agent_automation_rules_tenant_user_enabled",
        "agent_automation_rules",
        ["tenant_id", "user_id", "enabled"],
    )
    op.create_index(
        "ix_agent_automation_rules_tenant_rule_key",
        "agent_automation_rules",
        ["tenant_id", "rule_key"],
    )
    op.create_index(
        "uq_agent_automation_rules_active_rule",
        "agent_automation_rules",
        ["tenant_id", "user_id", "rule_key"],
        unique=True,
        postgresql_where=sa.text("enabled = true"),
        sqlite_where=sa.text("enabled = 1"),
    )


def downgrade() -> None:
    op.drop_index("uq_agent_automation_rules_active_rule", table_name="agent_automation_rules")
    op.drop_index("ix_agent_automation_rules_tenant_rule_key", table_name="agent_automation_rules")
    op.drop_index(
        "ix_agent_automation_rules_tenant_user_enabled",
        table_name="agent_automation_rules",
    )
    op.drop_table("agent_automation_rules")
