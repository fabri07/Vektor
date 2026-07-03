"""Origen y baja en inventory_movements (source_type/upload/row_ref/row_hash/voided_at).

Revision ID: 20260727_0001
Revises: 20260726_0001
Create Date: 2026-07-27

Contexto
--------
Migración ADDITIVE (columnas nullable) — habilita revertir/deduplicar movimientos de
inventario. Hoy `inventory_movements` es insert-only sin vínculo a su origen
(`source_event_id` es el literal genérico "import"), así que ni el void de un gasto ni
la relectura de un archivo pueden deshacer el movimiento correspondiente → los reread
inflaban el stock (movimientos duplicados). Se agregan:

- ``source_type``       — semántica del origen (purchase_import, catalog_initial_stock,
                          manual_adjustment, receipt, reconciliation).
- ``source_upload_id``  — FK al UploadedFile que lo originó (reversa del reread por
                          archivo). ON DELETE SET NULL conserva el movimiento histórico.
- ``source_row_ref``    — fila humana/debug.
- ``source_row_hash``   — identidad lógica estable (idempotencia; no depende del orden
                          del Excel).
- ``voided_at``         — soft-delete (dedup/reread). Un movimiento voidado no cuenta.

``downgrade`` dropea los índices y las columnas (sin datos previos → reversible).
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260727_0001"
down_revision = "20260726_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "inventory_movements",
        sa.Column("source_type", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "inventory_movements",
        sa.Column("source_upload_id", UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "inventory_movements",
        sa.Column("source_row_ref", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "inventory_movements",
        sa.Column("source_row_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "inventory_movements",
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_inventory_movements_source_upload",
        "inventory_movements",
        "uploaded_files",
        ["source_upload_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_inventory_movements_source_upload_id",
        "inventory_movements",
        ["source_upload_id"],
    )
    op.create_index(
        "ix_inventory_movements_source_row_hash",
        "inventory_movements",
        ["source_row_hash"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_inventory_movements_source_row_hash", table_name="inventory_movements"
    )
    op.drop_index(
        "ix_inventory_movements_source_upload_id", table_name="inventory_movements"
    )
    op.drop_constraint(
        "fk_inventory_movements_source_upload",
        "inventory_movements",
        type_="foreignkey",
    )
    op.drop_column("inventory_movements", "voided_at")
    op.drop_column("inventory_movements", "source_row_hash")
    op.drop_column("inventory_movements", "source_row_ref")
    op.drop_column("inventory_movements", "source_upload_id")
    op.drop_column("inventory_movements", "source_type")
