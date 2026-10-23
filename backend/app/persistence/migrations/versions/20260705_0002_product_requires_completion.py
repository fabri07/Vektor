"""FASE 3 (B2): requires_completion en products (auto-creados incompletos)

Revision ID: 20260705_0002
Revises: 20260705_0001
Create Date: 2026-07-05

Contexto
--------
Migración ADDITIVE. Agrega `requires_completion BOOLEAN NOT NULL DEFAULT false` a
`products`. Marca los productos auto-creados por un import a los que les faltan
datos clave (precio de venta o costo). El usuario los completa después; la UI los
señala con un badge. Los productos existentes quedan en `false` (default).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260705_0002"
down_revision = "20260705_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column(
            "requires_completion",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("products", "requires_completion")
