"""expense_type OPEX/COGS en expense_entries (discriminador contable)

Revision ID: 20260710_0001
Revises: 20260705_0002
Create Date: 2026-07-10

Contexto
--------
Migración ADDITIVE. Agrega `expense_type VARCHAR(10) NOT NULL DEFAULT 'OPEX'`
a `expense_entries`: distingue gasto operativo (OPEX) de compra de mercadería
(COGS), que hasta ahora eran indistinguibles en la misma tabla. Check constraint
de valores + índice (tenant_id, expense_type) para filtros/segmentación.

En Postgres moderno (Neon) agregar columna con server_default es metadata-only,
sin rewrite de tabla. El backfill de filas históricas (compra_proveedor /
gastos con product_id) se hace por script operativo aparte, no acá.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260710_0001"
down_revision = "20260705_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "expense_entries",
        sa.Column(
            "expense_type",
            sa.String(10),
            nullable=False,
            server_default="OPEX",
        ),
    )
    op.create_check_constraint(
        "ck_expense_entries_expense_type",
        "expense_entries",
        "expense_type IN ('OPEX', 'COGS')",
    )
    op.create_index(
        "ix_expense_entries_tenant_expense_type",
        "expense_entries",
        ["tenant_id", "expense_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_expense_entries_tenant_expense_type", table_name="expense_entries")
    op.drop_constraint(
        "ck_expense_entries_expense_type", "expense_entries", type_="check"
    )
    op.drop_column("expense_entries", "expense_type")
