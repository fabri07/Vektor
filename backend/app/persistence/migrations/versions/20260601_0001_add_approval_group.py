"""Stage 3: approval groups en pending_actions

Revision ID: 20260601_0001
Revises: 20260512_0001
Create Date: 2026-06-01
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260601_0001"
down_revision = "20260512_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pending_actions",
        sa.Column("approval_group_id", sa.String(36), nullable=True),
    )
    op.add_column(
        "pending_actions",
        sa.Column(
            "group_execution_status",
            sa.String(20),
            nullable=True,
        ),
    )
    op.add_column(
        "pending_actions",
        sa.Column("group_position", sa.Integer, nullable=True),
    )
    op.create_index(
        "ix_pending_actions_tenant_approval_group",
        "pending_actions",
        ["tenant_id", "approval_group_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_pending_actions_tenant_approval_group",
        table_name="pending_actions",
    )
    op.drop_column("pending_actions", "group_position")
    op.drop_column("pending_actions", "group_execution_status")
    op.drop_column("pending_actions", "approval_group_id")
