"""low_stock_threshold_units nullable: NULL=no configurado, 0=umbral explícito sin alerta

Revision ID: 20260512_0001
Revises: 20260511_0001
Create Date: 2026-05-12
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260512_0001"
down_revision = "20260511_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Permitir NULL en la columna
    op.alter_column(
        "products",
        "low_stock_threshold_units",
        existing_type=sa.Integer(),
        nullable=True,
        server_default=None,
    )
    # Convertir los 0 existentes a NULL (todos eran "no configurado" — era el default automático)
    op.execute(
        "UPDATE products SET low_stock_threshold_units = NULL WHERE low_stock_threshold_units = 0"
    )


def downgrade() -> None:
    # Restaurar NULLs a 0 antes de re-agregar la restricción NOT NULL
    op.execute(
        "UPDATE products SET low_stock_threshold_units = 0 WHERE low_stock_threshold_units IS NULL"
    )
    op.alter_column(
        "products",
        "low_stock_threshold_units",
        existing_type=sa.Integer(),
        nullable=False,
        server_default="0",
    )
