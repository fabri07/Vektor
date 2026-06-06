"""transaction_date DATE→DATETIME (ventas/gastos) + products.acquired_at

Revision ID: 20260625_0001
Revises: 20260620_0001
Create Date: 2026-06-25

- sales_entries.transaction_date y expense_entries.transaction_date pasan de DATE
  a TIMESTAMP para guardar fecha + hora (series de tiempo intradía). Las filas
  existentes quedan a medianoche (cast DATE→TIMESTAMP). La lógica de agregación
  diaria usa func.date(transaction_date), así que el comportamiento por día no cambia.
- products.acquired_at: fecha/hora de alta editable (NULL = no informada).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260625_0001"
down_revision = "20260620_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "sales_entries",
        "transaction_date",
        existing_type=sa.Date(),
        type_=sa.DateTime(),
        existing_nullable=False,
        postgresql_using="transaction_date::timestamp",
    )
    op.alter_column(
        "expense_entries",
        "transaction_date",
        existing_type=sa.Date(),
        type_=sa.DateTime(),
        existing_nullable=False,
        postgresql_using="transaction_date::timestamp",
    )
    op.add_column(
        "products",
        sa.Column("acquired_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("products", "acquired_at")
    op.alter_column(
        "expense_entries",
        "transaction_date",
        existing_type=sa.DateTime(),
        type_=sa.Date(),
        existing_nullable=False,
        postgresql_using="transaction_date::date",
    )
    op.alter_column(
        "sales_entries",
        "transaction_date",
        existing_type=sa.DateTime(),
        type_=sa.Date(),
        existing_nullable=False,
        postgresql_using="transaction_date::date",
    )
