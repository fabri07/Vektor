"""Add user_activity_events table for observability.

Revision ID: 20260324_0001
Revises: 20260318_0001
Create Date: 2026-03-24 10:00:00
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260324_0001"
down_revision: Union[str, None] = "20260318_0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_activity_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.user_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_user_activity_events_tenant_id",
        "user_activity_events",
        ["tenant_id"],
    )
    op.create_index(
        "ix_user_activity_events_user_id",
        "user_activity_events",
        ["user_id"],
    )
    op.create_index(
        "ix_user_activity_events_event_type",
        "user_activity_events",
        ["event_type"],
    )
    op.create_index(
        "ix_user_activity_events_created_at",
        "user_activity_events",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_user_activity_events_created_at", table_name="user_activity_events")
    op.drop_index("ix_user_activity_events_event_type", table_name="user_activity_events")
    op.drop_index("ix_user_activity_events_user_id", table_name="user_activity_events")
    op.drop_index("ix_user_activity_events_tenant_id", table_name="user_activity_events")
    op.drop_table("user_activity_events")
