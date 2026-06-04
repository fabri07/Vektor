"""add provenance flag to transactions + is_demo to tenants

Revision ID: 20260503_0001
Revises: 20260430_0002
Create Date: 2026-05-03

Adds:
  - tenants.is_demo               BOOLEAN NOT NULL DEFAULT false
  - sales_entries.provenance      VARCHAR(10) NOT NULL DEFAULT 'REAL'
  - expense_entries.provenance    VARCHAR(10) NOT NULL DEFAULT 'REAL'
  - products.provenance           VARCHAR(10) NOT NULL DEFAULT 'REAL'
  - uploaded_files.rejection_reason  VARCHAR(200) NULL

Then backfills:
  - tenants SET is_demo=true WHERE email IN (demo tenant emails)
  - sales_entries/expense_entries/products SET provenance='DEMO'
    WHERE tenant_id IN (SELECT tenant_id FROM tenants WHERE is_demo=true)
"""

import sqlalchemy as sa

from alembic import op

revision = "20260503_0001"
down_revision = "20260430_0002"
branch_labels = None
depends_on = None

_DEMO_EMAILS = (
    "demo.kiosco@vektor.app",
    "demo.limpieza@vektor.app",
    "demo.deco@vektor.app",
)


def upgrade() -> None:
    # ── tenants.is_demo ───────────────────────────────────────────────────────
    op.add_column(
        "tenants",
        sa.Column("is_demo", sa.Boolean(), nullable=False, server_default="false"),
    )

    # ── sales_entries.provenance ──────────────────────────────────────────────
    op.add_column(
        "sales_entries",
        sa.Column(
            "provenance",
            sa.String(10),
            nullable=False,
            server_default="REAL",
        ),
    )
    op.create_check_constraint(
        "ck_sales_entries_provenance",
        "sales_entries",
        "provenance IN ('REAL', 'DEMO')",
    )
    op.create_index(
        "ix_sales_entries_tenant_provenance",
        "sales_entries",
        ["tenant_id", "provenance"],
    )

    # ── expense_entries.provenance ────────────────────────────────────────────
    op.add_column(
        "expense_entries",
        sa.Column(
            "provenance",
            sa.String(10),
            nullable=False,
            server_default="REAL",
        ),
    )
    op.create_check_constraint(
        "ck_expense_entries_provenance",
        "expense_entries",
        "provenance IN ('REAL', 'DEMO')",
    )
    op.create_index(
        "ix_expense_entries_tenant_provenance",
        "expense_entries",
        ["tenant_id", "provenance"],
    )

    # ── products.provenance ───────────────────────────────────────────────────
    op.add_column(
        "products",
        sa.Column(
            "provenance",
            sa.String(10),
            nullable=False,
            server_default="REAL",
        ),
    )
    op.create_check_constraint(
        "ck_products_provenance",
        "products",
        "provenance IN ('REAL', 'DEMO')",
    )
    op.create_index(
        "ix_products_tenant_provenance",
        "products",
        ["tenant_id", "provenance"],
    )

    # ── uploaded_files.rejection_reason ──────────────────────────────────────
    op.add_column(
        "uploaded_files",
        sa.Column("rejection_reason", sa.String(100), nullable=True),
    )

    # ── Backfill: mark demo tenants ───────────────────────────────────────────
    placeholders = ", ".join(f"'{e}'" for e in _DEMO_EMAILS)
    op.execute(
        f"""
        UPDATE tenants
        SET is_demo = true
        WHERE tenant_id IN (
            SELECT tenant_id FROM users WHERE email IN ({placeholders})
        )
        """
    )

    # ── Backfill: mark demo transactions ─────────────────────────────────────
    op.execute(
        """
        UPDATE sales_entries
        SET provenance = 'DEMO'
        WHERE tenant_id IN (SELECT tenant_id FROM tenants WHERE is_demo = true)
        """
    )
    op.execute(
        """
        UPDATE expense_entries
        SET provenance = 'DEMO'
        WHERE tenant_id IN (SELECT tenant_id FROM tenants WHERE is_demo = true)
        """
    )
    op.execute(
        """
        UPDATE products
        SET provenance = 'DEMO'
        WHERE tenant_id IN (SELECT tenant_id FROM tenants WHERE is_demo = true)
        """
    )


def downgrade() -> None:
    op.drop_index("ix_products_tenant_provenance", table_name="products")
    op.drop_constraint("ck_products_provenance", "products", type_="check")
    op.drop_column("products", "provenance")

    op.drop_index("ix_expense_entries_tenant_provenance", table_name="expense_entries")
    op.drop_constraint("ck_expense_entries_provenance", "expense_entries", type_="check")
    op.drop_column("expense_entries", "provenance")

    op.drop_index("ix_sales_entries_tenant_provenance", table_name="sales_entries")
    op.drop_constraint("ck_sales_entries_provenance", "sales_entries", type_="check")
    op.drop_column("sales_entries", "provenance")

    op.drop_column("uploaded_files", "rejection_reason")
    op.drop_column("tenants", "is_demo")
