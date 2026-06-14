"""source_upload_id en sales_entries y expense_entries (trazabilidad de import)

Revision ID: 20260717_0001
Revises: 20260716_0001
Create Date: 2026-07-17

Contexto
--------
Migración ADDITIVE (B1 — preventivo de idempotencia). Agrega
`source_upload_id UUID NULL` con FK a `uploaded_files.id` (ON DELETE SET NULL)
en `sales_entries` y `expense_entries`. Permite:

  1. trazar de qué archivo nació cada fila importada (auditoría / soporte); y
  2. dar señal de origen-import al detector de duplicados de DataRepairService
     (dos filas activas con el mismo source_upload_id y contenido idéntico son
     candidatas a dedup; transacciones manuales idénticas no la tienen).

Nullable: las filas históricas y las cargas manuales quedan en NULL. En Postgres
agregar columna nullable es metadata-only (sin rewrite). FK con SET NULL: borrar
el archivo no borra las transacciones.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "20260717_0001"
down_revision = "20260716_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("sales_entries", "expense_entries"):
        op.add_column(
            table,
            sa.Column("source_upload_id", UUID(as_uuid=True), nullable=True),
        )
        op.create_foreign_key(
            f"fk_{table}_source_upload_id",
            table,
            "uploaded_files",
            ["source_upload_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_index(
            f"ix_{table}_source_upload_id",
            table,
            ["source_upload_id"],
        )


def downgrade() -> None:
    for table in ("sales_entries", "expense_entries"):
        op.drop_index(f"ix_{table}_source_upload_id", table_name=table)
        op.drop_constraint(
            f"fk_{table}_source_upload_id", table, type_="foreignkey"
        )
        op.drop_column(table, "source_upload_id")
