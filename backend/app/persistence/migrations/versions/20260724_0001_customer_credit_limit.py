"""Agrega credit_limit nullable a customers.

Revision ID: 20260724_0001
Revises: 20260723_0001
Create Date: 2026-07-24

Contexto
--------
Migración ADDITIVE — Fase 2 (vínculo cobro→cliente). Agrega a ``customers``:

  ``credit_limit`` (Numeric(14,2), nullable) — límite de crédito del cliente.
  NULL = sin límite configurado. El endpoint GET /customers/{id}/balance
  calcula ``over_limit = credit_limit is not None and balance > credit_limit``.

``downgrade`` hace ``drop_column`` (columna vacía — sin datos de producción
críticos todavía; reversible sin pérdida relevante).
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260724_0001"
down_revision = "20260723_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "customers",
        sa.Column("credit_limit", sa.Numeric(14, 2), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("customers", "credit_limit")
