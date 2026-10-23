"""Precio manual en la caja (cierre de B5).

Revision ID: 20261002_0001
Revises: 20261001_0001

Un cajero con el permiso ``price_override`` puede cobrar una línea a un precio
distinto del de lista, con motivo. Dos columnas en ``pos_operation_lines``:

- ``price_overridden``: la línea se cobró a mano. En columna y no sólo en la
  auditoría para poder listar los cambios de precio sin parsear JSON.
- ``price_override_reason``: por qué.

El precio de lista de esa línea queda en ``unit_price_catalog_ars`` (que ya
existía para las divergencias de B7).

Aditiva e idempotente (convención E8c; el lock de ``migrations/env.py``
serializa a la API y al worker). Sin backfill: ninguna línea anterior se cobró
a mano, y ``false`` es exactamente eso.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261002_0001"
down_revision = "20261001_0001"
branch_labels = None
depends_on = None

_TABLA = "pos_operation_lines"


def _columnas() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLA)}


def upgrade() -> None:
    existentes = _columnas()
    if "price_overridden" not in existentes:
        op.add_column(
            _TABLA,
            sa.Column(
                "price_overridden",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )
    if "price_override_reason" not in existentes:
        op.add_column(
            _TABLA,
            sa.Column("price_override_reason", sa.String(200), nullable=True),
        )


def downgrade() -> None:
    existentes = _columnas()
    if "price_override_reason" in existentes:
        op.drop_column(_TABLA, "price_override_reason")
    if "price_overridden" in existentes:
        op.drop_column(_TABLA, "price_overridden")
