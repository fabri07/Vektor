"""has_user_edits + source_row_ref en sales_entries, expense_entries y products

Revision ID: 20260720_0001
Revises: 20260719_0001
Create Date: 2026-07-20

Contexto
--------
Migración ADDITIVE (base de la "relectura de archivos" que re-importa datos
preservando ediciones manuales). Agrega a `sales_entries`, `expense_entries` y
`products`:

  1. `has_user_edits BOOLEAN NOT NULL DEFAULT FALSE` — marca si un registro de
     import fue editado a mano. La relectura NO debe pisar estos registros.
  2. `source_row_ref VARCHAR(64) NULL` — referencia a la fila de origen del
     archivo (NULL = manual / histórico / sin ref), para reconciliar cada
     registro importado con su fila al re-importar.

Nullable-safe: `has_user_edits` usa `server_default=false`, por lo que las filas
existentes quedan en FALSE sin rewrite manual. `source_row_ref` es nullable. En
Postgres agregar columna con server_default constante es metadata-only.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260720_0001"
down_revision = "20260719_0001"
branch_labels = None
depends_on = None


_TABLES = ("sales_entries", "expense_entries", "products")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "has_user_edits",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )
        op.add_column(
            table,
            sa.Column("source_row_ref", sa.String(length=64), nullable=True),
        )


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "source_row_ref")
        op.drop_column(table, "has_user_edits")
