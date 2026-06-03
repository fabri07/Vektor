"""add token tracking to audit log

Revision ID: 20260429_0001
Revises: 20260427_0001
Create Date: 2026-04-29

"""

import sqlalchemy as sa

from alembic import op

revision = "20260429_0001"
down_revision = "20260427_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "decision_audit_log",
        sa.Column("tokens_input", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "decision_audit_log",
        sa.Column("tokens_output", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "decision_audit_log",
        sa.Column("tokens_total", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_index(
        "ix_decision_audit_log_tenant_created",
        "decision_audit_log",
        ["tenant_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_decision_audit_log_tenant_created", table_name="decision_audit_log")
    op.drop_column("decision_audit_log", "tokens_total")
    op.drop_column("decision_audit_log", "tokens_output")
    op.drop_column("decision_audit_log", "tokens_input")
