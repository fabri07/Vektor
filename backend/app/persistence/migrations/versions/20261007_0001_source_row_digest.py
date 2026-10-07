"""Huella de la fila original de ventas y gastos importados (B·nuevo).

Revision ID: 20261007_0001
Revises: 20261002_0001

``source_row_digest`` guarda la huella del contenido de la fila que creó el
registro (``domain/fila_original.huella_de_fila``). El importador la escribe en
el mismo INSERT y la edición manual no la toca: es el dato original. La
relectura la usa para reubicar una edición a mano cuando las filas del archivo se
corrieron de lugar.

Aditiva e idempotente (convención E8c; el lock de ``migrations/env.py``
serializa a la API y al worker). Sin índice: se lee sólo entre los registros de
un archivo, que ya se filtran por ``source_upload_id``. Sin backfill, a
propósito: lo importado antes no tiene evidencia escrita de su fila original, y
queda como hoy, bajo la compuerta de la relectura.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261007_0001"
down_revision = "20261002_0001"
branch_labels = None
depends_on = None

_TABLAS = ("sales_entries", "expense_entries")
_COLUMNA = "source_row_digest"


def _tiene_columna(tabla: str) -> bool:
    return _COLUMNA in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(tabla)}


def upgrade() -> None:
    for tabla in _TABLAS:
        if not _tiene_columna(tabla):
            op.add_column(tabla, sa.Column(_COLUMNA, sa.String(80), nullable=True))


def downgrade() -> None:
    for tabla in _TABLAS:
        if _tiene_columna(tabla):
            op.drop_column(tabla, _COLUMNA)
