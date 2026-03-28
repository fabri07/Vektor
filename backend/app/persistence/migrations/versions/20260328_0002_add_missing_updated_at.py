"""Add missing updated_at to tables that use TimestampMixin

The initial migration omitted updated_at on several tables.
TimestampMixin requires both created_at and updated_at.

Revision ID: 20260328_0002
Revises: 20260328_0001
Create Date: 2026-03-28
"""

import sqlalchemy as sa
from alembic import op

revision = "20260328_0002"
down_revision = "20260328_0001"
branch_labels = None
depends_on = None

_TABLES = [
    "uploaded_files",
    "action_suggestions",
    "insights",
    "sales_entries",
    "expense_entries",
    "notifications",
    "heuristic_rule_sets",
]


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )


def downgrade() -> None:
    for table in reversed(_TABLES):
        op.drop_column(table, "updated_at")
