"""soft delete auditable para gastos y productos

Revision ID: 20260511_0001
Revises: 20260510_0001
Create Date: 2026-05-11
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260511_0001"
down_revision = "20260510_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_repair_items_action", "data_repair_items", type_="check")
    op.create_check_constraint(
        "ck_repair_items_action",
        "data_repair_items",
        "action IN ("
        "'VOID_SALE','CREATE_PRODUCT','UPDATE_PRODUCT','UPDATE_SALE','REVIEW_SALE'"
        ")",
    )

    op.add_column(
        "expense_entries",
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "expense_entries",
        sa.Column("void_reason", sa.String(30), nullable=True),
    )
    op.add_column(
        "expense_entries",
        sa.Column("voided_by_repair_run_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_expense_entries_void_reason",
        "expense_entries",
        "void_reason IS NULL OR void_reason IN ("
        "'REPAIR_MISCLASSIFIED_IMPORT', 'USER_CANCELLED', 'DUPLICATE', 'MANUAL_ADMIN_VOID')",
    )
    op.create_check_constraint(
        "ck_expense_entries_void_consistency",
        "expense_entries",
        "voided_at IS NULL OR void_reason IS NOT NULL",
    )
    op.create_index(
        "ix_expense_entries_tenant_voided_at",
        "expense_entries",
        ["tenant_id", "voided_at"],
    )

    op.add_column(
        "products",
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "products",
        sa.Column("deactivation_reason", sa.String(30), nullable=True),
    )
    op.create_check_constraint(
        "ck_products_deactivation_reason",
        "products",
        "deactivation_reason IS NULL OR deactivation_reason IN ("
        "'USER_CANCELLED', 'DUPLICATE', 'MANUAL_ADMIN_VOID')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_repair_items_action", "data_repair_items", type_="check")
    op.create_check_constraint(
        "ck_repair_items_action",
        "data_repair_items",
        "action IN ('VOID_SALE','CREATE_PRODUCT','UPDATE_PRODUCT')",
    )
    op.drop_constraint("ck_products_deactivation_reason", "products", type_="check")
    op.drop_column("products", "deactivation_reason")
    op.drop_column("products", "deactivated_at")
    op.drop_index("ix_expense_entries_tenant_voided_at", table_name="expense_entries")
    op.drop_constraint("ck_expense_entries_void_consistency", "expense_entries", type_="check")
    op.drop_constraint("ck_expense_entries_void_reason", "expense_entries", type_="check")
    op.drop_column("expense_entries", "voided_by_repair_run_id")
    op.drop_column("expense_entries", "void_reason")
    op.drop_column("expense_entries", "voided_at")
