"""add custom fields infrastructure (vertical definitions + tenant overrides + undo log)

Revision ID: 20260508_0001
Revises: 20260503_0002
Create Date: 2026-05-08
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260508_0001"
down_revision = "20260503_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Columnas custom_fields en tablas core ──────────────────────────────
    op.add_column(
        "sales_entries",
        sa.Column(
            "custom_fields",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "expense_entries",
        sa.Column(
            "custom_fields",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "products",
        sa.Column(
            "custom_fields",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "business_profiles",
        sa.Column(
            "custom_fields",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    # ── 2. vertical_field_definitions ─────────────────────────────────────────
    op.create_table(
        "vertical_field_definitions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("vertical_code", sa.String(50), nullable=False),
        sa.Column("entity_type", sa.String(30), nullable=False),
        sa.Column("field_key", sa.String(80), nullable=False),
        sa.Column("label", sa.String(200), nullable=False),
        sa.Column("data_type", sa.String(20), nullable=False),
        sa.Column("enum_options", postgresql.JSONB(), nullable=True),
        sa.Column("is_required", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("context_weight", sa.Float(), nullable=False, server_default="0"),
        sa.Column("affects_scoring", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "entity_type IN ('sale', 'expense', 'product', 'inventory')",
            name="ck_vfd_entity_type",
        ),
        sa.CheckConstraint(
            "data_type IN ('text', 'number', 'date', 'enum', 'boolean')",
            name="ck_vfd_data_type",
        ),
        sa.UniqueConstraint("vertical_code", "field_key", "entity_type", name="uq_vfd_natural_key"),
    )
    op.create_index(
        "ix_vfd_vertical_entity",
        "vertical_field_definitions",
        ["vertical_code", "entity_type"],
    )

    # ── 3. tenant_custom_field_definitions ────────────────────────────────────
    op.create_table(
        "tenant_custom_field_definitions",
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
        sa.Column(
            "vertical_field_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("vertical_field_definitions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("entity_type", sa.String(30), nullable=False),
        sa.Column("field_key", sa.String(80), nullable=False),
        sa.Column("override_label", sa.String(200), nullable=True),
        sa.Column("override_required", sa.Boolean(), nullable=True),
        sa.Column("override_enum_options", postgresql.JSONB(), nullable=True),
        sa.Column("data_type", sa.String(20), nullable=True),  # NULL for base fields (data_type lives in vertical_field_definitions)
        sa.CheckConstraint(
            "data_type IS NULL OR data_type IN ('text', 'number', 'date', 'enum', 'boolean')",
            name="ck_tcfd_data_type",
        ),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("is_base_field", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "entity_type IN ('sale', 'expense', 'product', 'inventory')",
            name="ck_tcfd_entity_type",
        ),
        sa.UniqueConstraint(
            "tenant_id", "field_key", "entity_type", name="uq_tcfd_tenant_field_entity"
        ),
    )
    op.create_index(
        "ix_tcfd_tenant_entity",
        "tenant_custom_field_definitions",
        ["tenant_id", "entity_type"],
    )

    # ── 4. tenant_field_change_log ─────────────────────────────────────────────
    op.create_table(
        "tenant_field_change_log",
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
        sa.Column("field_key", sa.String(80), nullable=False),
        sa.Column("entity_type", sa.String(30), nullable=False),
        sa.Column("action", sa.String(30), nullable=False),
        sa.Column("previous_state", postgresql.JSONB(), nullable=True),
        sa.Column("new_state", postgresql.JSONB(), nullable=False),
        sa.Column(
            "changed_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.user_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "changed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "action IN ('enabled', 'disabled', 'modified', 'created', 'undo')",
            name="ck_tfcl_action",
        ),
    )
    op.create_index(
        "ix_tfcl_tenant_changed_at",
        "tenant_field_change_log",
        ["tenant_id", "changed_at"],
    )
    op.create_index(
        "ix_tfcl_tenant_field",
        "tenant_field_change_log",
        ["tenant_id", "field_key", "entity_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_tfcl_tenant_field", table_name="tenant_field_change_log")
    op.drop_index("ix_tfcl_tenant_changed_at", table_name="tenant_field_change_log")
    op.drop_table("tenant_field_change_log")

    op.drop_index("ix_tcfd_tenant_entity", table_name="tenant_custom_field_definitions")
    op.drop_table("tenant_custom_field_definitions")

    op.drop_index("ix_vfd_vertical_entity", table_name="vertical_field_definitions")
    op.drop_table("vertical_field_definitions")

    op.drop_column("business_profiles", "custom_fields")
    op.drop_column("products", "custom_fields")
    op.drop_column("expense_entries", "custom_fields")
    op.drop_column("sales_entries", "custom_fields")
