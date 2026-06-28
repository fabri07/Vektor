"""purchase_orders + catalog_url/api_url en suppliers.

Revision ID: 20260724_0002
Revises: 20260724_0001
Create Date: 2026-07-24

Contexto
--------
Migración ADDITIVE. F3a (Fase 3 Véktor v4):

  1. Agrega ``catalog_url`` (Text, nullable) y ``api_url`` (Text, nullable) a ``suppliers``.
     Permite ligar proveedores a su catálogo web o API de precios.

  2. Crea tabla ``purchase_orders``: borradores de pedidos a proveedor generados
     por AgentSupplier desde el stock en quiebre. ``status="draft"`` no compromete
     dinero — el usuario edita/confirma manualmente.

Columnas de ``purchase_orders``:
  - ``id``          UUID PK
  - ``tenant_id``   UUID NOT NULL FK → tenants (multi-tenant; índice)
  - ``supplier_id`` UUID nullable FK → suppliers (puede no estar resuelto)
  - ``status``      String(20) NOT NULL default 'draft'
  - ``total``       Numeric(14,2) NOT NULL default 0
  - ``items``       JSONB NOT NULL default '[]' — lista de PurchaseOrderItem
  - ``notes``       Text nullable
  - ``created_at``  timestamptz default now()
  - ``updated_at``  timestamptz default now()

``downgrade``: drop_table + drop_column ×2.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "20260724_0002"
down_revision = "20260724_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Campos de catálogo en suppliers
    op.add_column("suppliers", sa.Column("catalog_url", sa.Text(), nullable=True))
    op.add_column("suppliers", sa.Column("api_url", sa.Text(), nullable=True))

    # 2. Tabla purchase_orders
    op.create_table(
        "purchase_orders",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", UUID(as_uuid=True), nullable=False),
        sa.Column("supplier_id", UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("total", sa.Numeric(14, 2), nullable=False, server_default="0"),
        sa.Column(
            "items",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("notes", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["tenant_id"],
            ["tenants.tenant_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["supplier_id"],
            ["suppliers.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_purchase_orders_tenant_id", "purchase_orders", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_purchase_orders_tenant_id", table_name="purchase_orders")
    op.drop_table("purchase_orders")
    op.drop_column("suppliers", "api_url")
    op.drop_column("suppliers", "catalog_url")
