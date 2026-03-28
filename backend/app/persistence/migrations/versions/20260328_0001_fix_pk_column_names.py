"""Fix PK column names to match UUIDPrimaryKeyMixin (rename *_id → id)

All ORM models using UUIDPrimaryKeyMixin expect a column named ``id``.
The initial migration created these tables with domain-prefixed names
(e.g. ``insight_id``, ``sale_id``).  This migration renames those columns.

PostgreSQL automatically updates foreign-key constraints that reference
a renamed column, so no FK rebuild is needed.

Revision ID: 20260328_0001
Revises: 20260324_0001
Create Date: 2026-03-28
"""

from alembic import op

revision = "20260328_0001"
down_revision = "20260324_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("business_snapshots", "snapshot_id", new_column_name="id")
    op.alter_column("heuristic_rule_sets", "ruleset_id", new_column_name="id")
    op.alter_column("products", "product_id", new_column_name="id")
    op.alter_column("sales_entries", "sale_id", new_column_name="id")
    op.alter_column("expense_entries", "expense_id", new_column_name="id")
    op.alter_column("health_score_snapshots", "health_score_snapshot_id", new_column_name="id")
    op.alter_column("decision_audit_log", "audit_id", new_column_name="id")
    op.alter_column("insights", "insight_id", new_column_name="id")
    op.alter_column("action_suggestions", "action_suggestion_id", new_column_name="id")
    op.alter_column("notifications", "notification_id", new_column_name="id")


def downgrade() -> None:
    op.alter_column("notifications", "id", new_column_name="notification_id")
    op.alter_column("action_suggestions", "id", new_column_name="action_suggestion_id")
    op.alter_column("insights", "id", new_column_name="insight_id")
    op.alter_column("decision_audit_log", "id", new_column_name="audit_id")
    op.alter_column("health_score_snapshots", "id", new_column_name="health_score_snapshot_id")
    op.alter_column("expense_entries", "id", new_column_name="expense_id")
    op.alter_column("sales_entries", "id", new_column_name="sale_id")
    op.alter_column("products", "id", new_column_name="product_id")
    op.alter_column("heuristic_rule_sets", "id", new_column_name="ruleset_id")
    op.alter_column("business_snapshots", "id", new_column_name="snapshot_id")
