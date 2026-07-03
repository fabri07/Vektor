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

Idempotente: cada paso chequea el estado actual antes de aplicarse (vía
``sa.inspect``). Necesario porque más de un servicio de Railway puede correr
``alembic upgrade head`` en paralelo contra la misma DB en un mismo deploy
(cada uno con su propio ``preDeployCommand``) — sin esto, el segundo en
llegar revienta con "column already exists" aunque la migración ya haya
quedado aplicada por el primero.
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260727_0001"
down_revision = "20260726_0001"
branch_labels = None
depends_on = None

_TABLE = "inventory_movements"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {c["name"] for c in inspector.get_columns(_TABLE)}
    existing_fks = {fk["name"] for fk in inspector.get_foreign_keys(_TABLE)}
    existing_indexes = {ix["name"] for ix in inspector.get_indexes(_TABLE)}

    def _add_column(name: str, col_type: sa.types.TypeEngine) -> None:
        if name not in existing_columns:
            op.add_column(_TABLE, sa.Column(name, col_type, nullable=True))

    _add_column("source_type", sa.String(length=40))
    _add_column("source_upload_id", UUID(as_uuid=True))
    _add_column("source_row_ref", sa.String(length=100))
    _add_column("source_row_hash", sa.String(length=64))
    _add_column("voided_at", sa.DateTime(timezone=True))

    if "fk_inventory_movements_source_upload" not in existing_fks:
        op.create_foreign_key(
            "fk_inventory_movements_source_upload",
            _TABLE,
            "uploaded_files",
            ["source_upload_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if "ix_inventory_movements_source_upload_id" not in existing_indexes:
        op.create_index(
            "ix_inventory_movements_source_upload_id",
            _TABLE,
            ["source_upload_id"],
        )
    if "ix_inventory_movements_source_row_hash" not in existing_indexes:
        op.create_index(
            "ix_inventory_movements_source_row_hash",
            _TABLE,
            ["source_row_hash"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {c["name"] for c in inspector.get_columns(_TABLE)}
    existing_fks = {fk["name"] for fk in inspector.get_foreign_keys(_TABLE)}
    existing_indexes = {ix["name"] for ix in inspector.get_indexes(_TABLE)}

    if "ix_inventory_movements_source_row_hash" in existing_indexes:
        op.drop_index("ix_inventory_movements_source_row_hash", table_name=_TABLE)
    if "ix_inventory_movements_source_upload_id" in existing_indexes:
        op.drop_index("ix_inventory_movements_source_upload_id", table_name=_TABLE)
    if "fk_inventory_movements_source_upload" in existing_fks:
        op.drop_constraint(
            "fk_inventory_movements_source_upload", _TABLE, type_="foreignkey"
        )
    for col in (
        "voided_at",
        "source_row_hash",
        "source_row_ref",
        "source_upload_id",
        "source_type",
    ):
        if col in existing_columns:
            op.drop_column(_TABLE, col)
