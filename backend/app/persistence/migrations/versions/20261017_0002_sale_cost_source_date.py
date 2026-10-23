"""Fecha de la fuente del costo en ``sales_entries`` (M·3-4).

Revision ID: 20261017_0002
Revises: 20261017_0001

``cost_source_date`` = fecha de NEGOCIO de la fuente que eligió el costeo de la
venta (la compra, el stock inicial o la edición manual/de catálogo). Con ella el
margen dice qué parte de lo vendido usó un costo de hace más de 45 días. NULL =
antigüedad desconocida: costo cargado sin fecha, sin costo, o venta anterior a
M·3-4 (el backfill ``costeo_retroactivo_ventas.py --solo-fecha`` la completa
sólo si el costo de la fuente coincide con el congelado).

Aditiva e idempotente (convención E8c; el lock de ``migrations/env.py``
serializa a la API y al worker). Comprueba PRESENCIA, no forma.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261017_0002"
down_revision = "20261017_0001"
branch_labels = None
depends_on = None

_TABLA = "sales_entries"
_COLUMNA = "cost_source_date"


def _columnas() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLA)}


def upgrade() -> None:
    if _COLUMNA not in _columnas():
        op.add_column(_TABLA, sa.Column(_COLUMNA, sa.Date(), nullable=True))


def downgrade() -> None:
    if _COLUMNA in _columnas():
        op.drop_column(_TABLA, _COLUMNA)
