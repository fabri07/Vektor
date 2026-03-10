"""Initial schema v1.1 for Vektor.

Revision ID: 20260310_0001
Revises:
Create Date: 2026-03-10 21:05:00

"""

from __future__ import annotations

import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "20260310_0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("legal_name", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False, server_default=sa.text("'ARS'")),
        sa.Column(
            "pricing_reference_mode",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'MEP'"),
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'ACTIVE'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id"),
    )

    op.create_table(
        "users",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column("role_code", sa.Text(), nullable=False, server_default=sa.text("'OWNER'")),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
        sa.UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
    )

    op.create_table(
        "subscriptions",
        sa.Column("subscription_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_code", sa.Text(), nullable=False, server_default=sa.text("'FREE'")),
        sa.Column("plan_price_usd_reference", sa.Numeric(8, 2), nullable=True),
        sa.Column("plan_price_ars", sa.Numeric(14, 2), nullable=True),
        sa.Column(
            "billing_index_reference",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'MEP'"),
        ),
        sa.Column("seats_included", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'ACTIVE'")),
        sa.Column("current_period_start", sa.Date(), nullable=True),
        sa.Column("current_period_end", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("subscription_id"),
        sa.UniqueConstraint("tenant_id", name="uq_subscriptions_tenant_id"),
    )

    op.create_table(
        "business_profiles",
        sa.Column("profile_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("vertical_code", sa.Text(), nullable=False),
        sa.Column("data_mode", sa.Text(), nullable=False, server_default=sa.text("'M0'")),
        sa.Column("data_confidence", sa.Text(), nullable=False, server_default=sa.text("'LOW'")),
        sa.Column("monthly_sales_estimate_ars", sa.Numeric(14, 2), nullable=True),
        sa.Column("monthly_inventory_spend_estimate_ars", sa.Numeric(14, 2), nullable=True),
        sa.Column("monthly_fixed_expenses_estimate_ars", sa.Numeric(14, 2), nullable=True),
        sa.Column("cash_on_hand_estimate_ars", sa.Numeric(14, 2), nullable=True),
        sa.Column("supplier_count_estimate", sa.Integer(), nullable=True),
        sa.Column("product_count_estimate", sa.Integer(), nullable=True),
        sa.Column(
            "onboarding_completed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
        sa.Column(
            "heuristic_profile_version",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'v1'"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("profile_id"),
        sa.UniqueConstraint("tenant_id", name="uq_business_profiles_tenant_id"),
    )

    op.create_table(
        "heuristic_rule_sets",
        sa.Column("ruleset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_code", sa.Text(), nullable=False),
        sa.Column("vertical_code", sa.Text(), nullable=False),
        sa.Column("rules_json", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("ruleset_id"),
        sa.UniqueConstraint("version_code", name="uq_heuristic_rule_sets_version_code"),
    )

    op.create_table(
        "business_snapshots",
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_date", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "snapshot_version",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'v1'"),
        ),
        sa.Column("raw_inputs_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("data_completeness_score", sa.Numeric(5, 2), nullable=True),
        sa.Column("data_mode", sa.Text(), nullable=True),
        sa.Column("confidence_level", sa.Text(), nullable=True),
        sa.Column("heuristic_ruleset_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["heuristic_ruleset_id"],
            ["heuristic_rule_sets.ruleset_id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("snapshot_id"),
    )

    op.create_table(
        "products",
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("unit_cost_ars", sa.Numeric(14, 2), nullable=True),
        sa.Column("sale_price_ars", sa.Numeric(14, 2), nullable=True),
        sa.Column("stock_units", sa.Integer(), nullable=True),
        sa.Column("low_stock_threshold_units", sa.Integer(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")),
        sa.Column("source_type", sa.Text(), nullable=False, server_default=sa.text("'MANUAL'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("product_id"),
    )

    op.create_table(
        "sales_entries",
        sa.Column("sale_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("sale_date", sa.Date(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("unit_price_ars", sa.Numeric(14, 2), nullable=True),
        sa.Column("gross_amount_ars", sa.Numeric(14, 2), nullable=True),
        sa.Column(
            "sale_source_type",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'MANUAL'"),
        ),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["product_id"], ["products.product_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("sale_id"),
    )

    op.create_table(
        "expense_entries",
        sa.Column("expense_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("category_code", sa.Text(), nullable=False),
        sa.Column("expense_date", sa.Date(), nullable=False),
        sa.Column("amount_ars", sa.Numeric(14, 2), nullable=False),
        sa.Column("source_type", sa.Text(), nullable=False, server_default=sa.text("'MANUAL'")),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("expense_id"),
    )

    op.create_table(
        "health_score_snapshots",
        sa.Column("health_score_snapshot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_snapshot_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("score_total", sa.Integer(), nullable=True),
        sa.Column("score_cash", sa.Integer(), nullable=True),
        sa.Column("score_margin", sa.Integer(), nullable=True),
        sa.Column("score_stock", sa.Integer(), nullable=True),
        sa.Column("score_supplier", sa.Integer(), nullable=True),
        sa.Column("score_confidence", sa.Text(), nullable=True),
        sa.Column("score_version", sa.Text(), nullable=False, server_default=sa.text("'v1a'")),
        sa.Column("heuristic_version", sa.Text(), nullable=False, server_default=sa.text("'v1'")),
        sa.Column("primary_risk_code", sa.Text(), nullable=True),
        sa.Column("score_inputs_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_snapshot_id"],
            ["business_snapshots.snapshot_id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("health_score_snapshot_id"),
    )

    op.create_table(
        "decision_audit_log",
        sa.Column("audit_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision_type", sa.Text(), nullable=False),
        sa.Column("source_snapshot_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_ruleset_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("input_state_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("output_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("confidence_level", sa.Text(), nullable=True),
        sa.Column(
            "displayed_to_user",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
        sa.Column(
            "user_acknowledged",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_ruleset_id"],
            ["heuristic_rule_sets.ruleset_id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_snapshot_id"],
            ["business_snapshots.snapshot_id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("audit_id"),
    )

    op.create_table(
        "momentum_profiles",
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("best_score_ever", sa.Integer(), nullable=True),
        sa.Column("best_score_date", sa.Date(), nullable=True),
        sa.Column("active_goal_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "milestones_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("estimated_value_protected_ars", sa.Numeric(14, 2), nullable=True),
        sa.Column(
            "improving_streak_weeks",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id"),
    )

    op.create_table(
        "weekly_score_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("week_start_date", sa.Date(), nullable=False),
        sa.Column("health_score_total", sa.Integer(), nullable=True),
        sa.Column("score_cash", sa.Integer(), nullable=True),
        sa.Column("score_margin", sa.Integer(), nullable=True),
        sa.Column("score_stock", sa.Integer(), nullable=True),
        sa.Column("score_supplier", sa.Integer(), nullable=True),
        sa.Column("delta_vs_previous_week", sa.Integer(), nullable=True),
        sa.Column("trend_label", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "insights",
        sa.Column("insight_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("insight_type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "severity_code",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'MEDIUM'"),
        ),
        sa.Column(
            "heuristic_version",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'v1'"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("insight_id"),
    )

    op.create_table(
        "action_suggestions",
        sa.Column("action_suggestion_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("insight_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action_type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("risk_level", sa.Text(), nullable=False, server_default=sa.text("'LOW'")),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'SUGGESTED'"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["insight_id"], ["insights.insight_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("action_suggestion_id"),
    )

    op.create_table(
        "uploaded_files",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("file_type", sa.Text(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("processing_status", sa.Text(), nullable=False),
        sa.Column("parsed_summary_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "notifications",
        sa.Column("notification_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("channel_code", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column(
            "delivery_status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'PENDING'"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("notification_id"),
    )

    op.create_table(
        "user_activity_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "stock_snapshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("snapshot_date", sa.Date(), nullable=False),
        sa.Column("total_sku_count", sa.Integer(), nullable=True),
        sa.Column("stock_value_est", sa.Numeric(14, 2), nullable=True),
        sa.Column("critical_stock_value_est", sa.Numeric(14, 2), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )

    # Indexes for tenant_id FK access patterns.
    op.create_index("ix_users_tenant_id", "users", ["tenant_id"], unique=False)
    op.create_index("ix_business_snapshots_tenant_id", "business_snapshots", ["tenant_id"], unique=False)
    op.create_index("ix_products_tenant_id", "products", ["tenant_id"], unique=False)
    op.create_index("ix_expense_entries_tenant_id", "expense_entries", ["tenant_id"], unique=False)
    op.create_index("ix_insights_tenant_id", "insights", ["tenant_id"], unique=False)
    op.create_index("ix_action_suggestions_tenant_id", "action_suggestions", ["tenant_id"], unique=False)
    op.create_index("ix_uploaded_files_tenant_id", "uploaded_files", ["tenant_id"], unique=False)
    op.create_index("ix_notifications_tenant_id", "notifications", ["tenant_id"], unique=False)
    op.create_index("ix_user_activity_events_tenant_id", "user_activity_events", ["tenant_id"], unique=False)
    op.create_index("ix_stock_snapshots_tenant_id", "stock_snapshots", ["tenant_id"], unique=False)

    # Required composite indexes.
    op.execute(
        sa.text(
            "CREATE INDEX ix_health_score_snapshots_tenant_created_at_desc "
            "ON health_score_snapshots (tenant_id, created_at DESC)"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX ix_weekly_score_history_tenant_week_start_desc "
            "ON weekly_score_history (tenant_id, week_start_date DESC)"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX ix_decision_audit_log_tenant_created_at_desc "
            "ON decision_audit_log (tenant_id, created_at DESC)"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX ix_sales_entries_tenant_sale_date_desc "
            "ON sales_entries (tenant_id, sale_date DESC)"
        )
    )

    # Initial active ruleset seed.
    base_rules = {
        "weights": {
            "cash": 0.30,
            "margin": 0.30,
            "stock": 0.25,
            "supplier": 0.15,
        }
    }
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            INSERT INTO heuristic_rule_sets (
                ruleset_id,
                version_code,
                vertical_code,
                rules_json,
                description,
                is_active,
                created_at,
                activated_at
            ) VALUES (
                '0f1c8f4d-8a5f-48a8-9e0b-7e449b7a1001',
                'v1.0',
                'ALL',
                CAST(:rules_json AS jsonb),
                'Reglas base iniciales para todos los verticales.',
                TRUE,
                NOW(),
                NOW()
            )
            """
        ),
        {"rules_json": json.dumps(base_rules)},
    )


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS ix_sales_entries_tenant_sale_date_desc"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_decision_audit_log_tenant_created_at_desc"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_weekly_score_history_tenant_week_start_desc"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_health_score_snapshots_tenant_created_at_desc"))

    op.drop_index("ix_stock_snapshots_tenant_id", table_name="stock_snapshots")
    op.drop_index("ix_user_activity_events_tenant_id", table_name="user_activity_events")
    op.drop_index("ix_notifications_tenant_id", table_name="notifications")
    op.drop_index("ix_uploaded_files_tenant_id", table_name="uploaded_files")
    op.drop_index("ix_action_suggestions_tenant_id", table_name="action_suggestions")
    op.drop_index("ix_insights_tenant_id", table_name="insights")
    op.drop_index("ix_expense_entries_tenant_id", table_name="expense_entries")
    op.drop_index("ix_products_tenant_id", table_name="products")
    op.drop_index("ix_business_snapshots_tenant_id", table_name="business_snapshots")
    op.drop_index("ix_users_tenant_id", table_name="users")

    op.drop_table("stock_snapshots")
    op.drop_table("user_activity_events")
    op.drop_table("notifications")
    op.drop_table("uploaded_files")
    op.drop_table("action_suggestions")
    op.drop_table("insights")
    op.drop_table("weekly_score_history")
    op.drop_table("momentum_profiles")
    op.drop_table("decision_audit_log")
    op.drop_table("health_score_snapshots")
    op.drop_table("expense_entries")
    op.drop_table("sales_entries")
    op.drop_table("products")
    op.drop_table("business_snapshots")
    op.drop_table("heuristic_rule_sets")
    op.drop_table("business_profiles")
    op.drop_table("subscriptions")
    op.drop_table("users")
    op.drop_table("tenants")
