"""soft delete en sales_entries + tablas de reparación auditada

Revision ID: 20260510_0001
Revises: 20260508_0001
Create Date: 2026-05-10
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260510_0001"
down_revision = "20260508_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Soft delete en sales_entries ───────────────────────────────────────
    op.add_column(
        "sales_entries",
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "sales_entries",
        sa.Column("void_reason", sa.String(30), nullable=True),
    )
    op.add_column(
        "sales_entries",
        sa.Column("voided_by_repair_run_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_sales_entries_void_reason",
        "sales_entries",
        "void_reason IS NULL OR void_reason IN ("
        "'REPAIR_MISCLASSIFIED_IMPORT', 'USER_CANCELLED', 'DUPLICATE', 'MANUAL_ADMIN_VOID')",
    )
    op.create_check_constraint(
        "ck_sales_entries_void_consistency",
        "sales_entries",
        "voided_at IS NULL OR void_reason IS NOT NULL",
    )
    op.create_index(
        "ix_sales_entries_tenant_voided_at",
        "sales_entries",
        ["tenant_id", "voided_at"],
    )

    # ── 2. data_repair_runs ───────────────────────────────────────────────────
    op.create_table(
        "data_repair_runs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("repair_type", sa.String(100), nullable=False),
        sa.Column("status", sa.String(30), nullable=False, server_default="PENDING"),
        sa.Column("dry_run", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("source_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("candidates_found", sa.Integer, nullable=False, server_default="0"),
        sa.Column("sales_detected", sa.Integer, nullable=False, server_default="0"),
        sa.Column("sales_voided", sa.Integer, nullable=False, server_default="0"),
        sa.Column("products_detected", sa.Integer, nullable=False, server_default="0"),
        sa.Column("products_created", sa.Integer, nullable=False, server_default="0"),
        sa.Column("products_updated", sa.Integer, nullable=False, server_default="0"),
        sa.Column("products_skipped", sa.Integer, nullable=False, server_default="0"),
        sa.Column("details_json", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["source_run_id"],
            ["data_repair_runs.id"],
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','RUNNING','COMPLETED','FAILED','APPROVED','APPLIED')",
            name="ck_repair_runs_status",
        ),
    )

    # ── 3. data_repair_items ──────────────────────────────────────────────────
    op.create_table(
        "data_repair_items",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_file_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("sale_entry_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(50), nullable=False),
        sa.Column("before_json", postgresql.JSONB(), nullable=True),
        sa.Column("after_json", postgresql.JSONB(), nullable=True),
        sa.Column("confidence", sa.String(10), nullable=False, server_default="'MEDIUM'"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["data_repair_runs.id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "action IN ('VOID_SALE','CREATE_PRODUCT','UPDATE_PRODUCT')",
            name="ck_repair_items_action",
        ),
    )
    op.create_index("ix_repair_items_run_id", "data_repair_items", ["run_id"])
    op.create_index(
        "ix_repair_items_run_sale",
        "data_repair_items",
        ["run_id", "sale_entry_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_repair_items_run_sale", table_name="data_repair_items")
    op.drop_index("ix_repair_items_run_id", table_name="data_repair_items")
    op.drop_table("data_repair_items")
    op.drop_table("data_repair_runs")
    op.drop_index("ix_sales_entries_tenant_voided_at", table_name="sales_entries")
    op.drop_constraint("ck_sales_entries_void_consistency", "sales_entries", type_="check")
    op.drop_constraint("ck_sales_entries_void_reason", "sales_entries", type_="check")
    op.drop_column("sales_entries", "voided_by_repair_run_id")
    op.drop_column("sales_entries", "void_reason")
    op.drop_column("sales_entries", "voided_at")
