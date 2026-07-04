"""Fecha de negocio en inventory_movements (occurred_at).

Revision ID: 20260727_0002
Revises: 20260727_0001
Create Date: 2026-07-27

Contexto
--------
``inventory_movements.created_at`` guarda la fecha de INSERCIÓN de la fila (fecha de
carga del archivo), no la fecha real en la que ocurrió la compra/venta/ajuste en el
mundo real. Un dedup que agrupó movimientos por ``date(created_at)`` voideó compras
reales de meses distintos que habían sido cargadas el mismo día (incidente "don
pedro", 2026-07) — el fix de raíz es distinguir explícitamente la fecha de NEGOCIO de
la fecha de carga.

Se agrega ``occurred_at`` (nullable, sin backfill, sin índice, sin server_default):
tareas posteriores poblarán la columna en los writers de movimientos. Filas legacy
quedan con ``occurred_at IS NULL`` — todo lector DEBE usar
``COALESCE(occurred_at, created_at)`` para no perder movimientos históricos.

Idempotente vía ``sa.inspect`` (mismo motivo que ``20260727_0001``: más de un
servicio de Railway puede correr ``alembic upgrade head`` en paralelo contra la
misma DB en un mismo deploy).
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "20260727_0002"
down_revision = "20260727_0001"
branch_labels = None
depends_on = None

_TABLE = "inventory_movements"
_COLUMN = "occurred_at"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {c["name"] for c in inspector.get_columns(_TABLE)}

    if _COLUMN not in existing_columns:
        op.add_column(
            _TABLE,
            sa.Column(_COLUMN, sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_columns = {c["name"] for c in inspector.get_columns(_TABLE)}

    if _COLUMN in existing_columns:
        op.drop_column(_TABLE, _COLUMN)
