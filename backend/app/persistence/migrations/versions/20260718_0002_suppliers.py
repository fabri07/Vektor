"""suppliers + expense_entries.supplier_id (entidad proveedores — Fase 3)

Revision ID: 20260718_0002
Revises: 20260718_0001
Create Date: 2026-07-18

Contexto
--------
Migración ADDITIVE y reversible. Crea la entidad `suppliers` (proveedores del
tenant) y vincula opcionalmente los gastos a un proveedor. Es ESPEJO de
`20260718_0001` (clientes):

  - Tabla `suppliers`: catálogo de proveedores por tenant. `tenant_id` FK CASCADE
    a `tenants.tenant_id`. Soft-delete vía `deactivated_at` (NULL = activo).
    `custom_fields` JSONB para campos definidos por vertical/tenant.
  - `expense_entries.supplier_id UUID NULL` con FK a `suppliers.id`
    (ON DELETE SET NULL): borrar un proveedor no borra sus gastos históricos, solo
    desvincula. Nullable: los gastos históricos y los que no informan proveedor
    quedan en NULL. Se mantiene `expense_entries.supplier_name` (texto libre).

Todo additive/nullable: no rompe filas existentes ni requiere backfill. En
Postgres agregar columna nullable es metadata-only (sin rewrite de tabla).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "20260718_0002"
down_revision = "20260718_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "suppliers",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("phone", sa.String(50), nullable=True),
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
    op.create_index("ix_suppliers_tenant_id", "suppliers", ["tenant_id"])

    op.add_column(
        "expense_entries",
        sa.Column("supplier_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_expense_entries_supplier_id",
        "expense_entries",
        "suppliers",
        ["supplier_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_expense_entries_supplier_id",
        "expense_entries",
        ["supplier_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_expense_entries_supplier_id", table_name="expense_entries")
    op.drop_constraint("fk_expense_entries_supplier_id", "expense_entries", type_="foreignkey")
    op.drop_column("expense_entries", "supplier_id")
    op.drop_index("ix_suppliers_tenant_id", table_name="suppliers")
    op.drop_table("suppliers")
