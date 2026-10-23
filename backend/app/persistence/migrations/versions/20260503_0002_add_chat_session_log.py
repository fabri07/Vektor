"""add chat session log

Revision ID: 20260503_0002
Revises: 20260503_0001
Create Date: 2026-05-03
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260503_0002"
down_revision = "20260503_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chat_session_log",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("session_id", sa.String(128), nullable=False),
        sa.Column("entry_type", sa.String(30), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.String(30), nullable=True),
        sa.Column("entity_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column(
            "pending_action_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("pending_actions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "uploaded_file_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("uploaded_files.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "entry_type IN ('DATA_LOADED', 'DATA_REJECTED', 'FILE_UPLOADED', "
            "'INTENT_DETECTED', 'QUERY_ANSWERED')",
            name="ck_chat_session_log_entry_type",
        ),
    )
    op.execute(
        "CREATE INDEX ix_chat_session_log_tenant_created "
        "ON chat_session_log (tenant_id, created_at DESC)"
    )
    op.create_index(
        "ix_chat_session_log_tenant_entry_type",
        "chat_session_log",
        ["tenant_id", "entry_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_chat_session_log_tenant_entry_type", table_name="chat_session_log")
    op.drop_index("ix_chat_session_log_tenant_created", table_name="chat_session_log")
    op.drop_table("chat_session_log")
