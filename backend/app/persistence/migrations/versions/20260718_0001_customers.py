"""customers + sales_entries.customer_id (entidad clientes — Fase 2)

Revision ID: 20260718_0001
Revises: 20260717_0002
Create Date: 2026-07-18

Contexto
--------
Migración ADDITIVE y reversible. Crea la entidad `customers` (clientes del
tenant) y vincula opcionalmente las ventas a un cliente:

  - Tabla `customers`: catálogo de clientes por tenant. `tenant_id` FK CASCADE
    a `tenants.tenant_id`. Soft-delete vía `deactivated_at` (NULL = activo).
    `custom_fields` JSONB para campos definidos por vertical/tenant.
  - `sales_entries.customer_id UUID NULL` con FK a `customers.id`
    (ON DELETE SET NULL): borrar un cliente no borra sus ventas históricas, solo
    desvincula. Nullable: las ventas históricas y las que no informan cliente
    quedan en NULL.

Todo additive/nullable: no rompe filas existentes ni requiere backfill. En
Postgres agregar columna nullable es metadata-only (sin rewrite de tabla).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "20260718_0001"
down_revision = "20260717_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "customers",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("phone", sa.String(50), nullable=True),
        sa.Column("telegram_username", sa.String(100), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "custom_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_customers_tenant_id", "customers", ["tenant_id"])

    op.add_column(
        "sales_entries",
        sa.Column("customer_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_sales_entries_customer_id",
        "sales_entries",
        "customers",
        ["customer_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_sales_entries_customer_id",
        "sales_entries",
        ["customer_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_sales_entries_customer_id", table_name="sales_entries")
    op.drop_constraint("fk_sales_entries_customer_id", "sales_entries", type_="foreignkey")
    op.drop_column("sales_entries", "customer_id")
    op.drop_index("ix_customers_tenant_id", table_name="customers")
    op.drop_table("customers")
