"""add memory tables

Revision ID: 20260421_0001
Revises: 20260406_0002
Create Date: 2026-04-21 00:00:00.000000
"""
from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260421_0001"
down_revision = "20260406_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operation_fingerprints",
        sa.Column("id", sa.UUID(), nullable=False, default=uuid.uuid4),
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column(
            "executed_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "fingerprint", name="uq_operation_fingerprints_tenant_fp"
        ),
    )
    op.create_index(
        "ix_op_fp_tenant_executed",
        "operation_fingerprints",
        ["tenant_id", "executed_at"],
    )

    op.create_table(
        "business_memory",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("last_sale_amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("last_sale_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "sales_today",
            sa.Numeric(12, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "sales_this_week",
            sa.Numeric(12, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "sales_this_month",
            sa.Numeric(12, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "expenses_today",
            sa.Numeric(12, 2),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "products_in_stockout",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "recent_actions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            server_default="'[]'::jsonb",
        ),
        sa.Column("llm_context_summary", sa.Text(), nullable=True),
        sa.Column(
            "llm_context_updated_at", sa.TIMESTAMP(timezone=True), nullable=True
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("tenant_id"),
    )


def downgrade() -> None:
    op.drop_table("business_memory")
    op.drop_index(
        "ix_op_fp_tenant_executed", table_name="operation_fingerprints"
    )
    op.drop_table("operation_fingerprints")
