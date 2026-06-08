"""FASE 3 (B1): product_id en expense_entries (vínculo al catálogo)

Revision ID: 20260705_0001
Revises: 20260701_0001
Create Date: 2026-07-05

Contexto
--------
Migración ADDITIVE. Agrega `product_id UUID NULL` a `expense_entries` con FK a
`products.id` ON DELETE SET NULL, simétrica a la que ya tiene `sales_entries`.
Permite vincular compras de mercadería/insumos importadas al producto del
catálogo. NULL = no resuelto o ambiguo; SET NULL al borrar el producto conserva
el gasto histórico. Índice para consultas "gastos por producto".
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260705_0001"
down_revision = "20260701_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "expense_entries",
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_expense_entries_product_id",
        "expense_entries",
        "products",
        ["product_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_expense_entries_product_id",
        "expense_entries",
        ["product_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_expense_entries_product_id", table_name="expense_entries")
    op.drop_constraint("fk_expense_entries_product_id", "expense_entries", type_="foreignkey")
    op.drop_column("expense_entries", "product_id")
