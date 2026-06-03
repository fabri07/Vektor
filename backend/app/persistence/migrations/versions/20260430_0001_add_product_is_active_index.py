"""add composite index on products(tenant_id, is_active)

Revision ID: 20260430_0001
Revises: 20260429_0001
Create Date: 2026-04-30

Speeds up /insights/breakdown which filters by both tenant_id and is_active
in COUNT, low_stock and no_rotation queries.
"""

from alembic import op

revision = "20260430_0001"
down_revision = "20260429_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_products_tenant_is_active",
        "products",
        ["tenant_id", "is_active"],
    )


def downgrade() -> None:
    op.drop_index("ix_products_tenant_is_active", table_name="products")
