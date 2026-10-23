"""Seguimiento de los recálculos pendientes del costo promedio (M·P2b).

Revision ID: 20261013_0002
Revises: 20261013_0001

``product_cost_state`` gana ``marcado_en`` (cuándo se marcó el pendiente: con él
se drenan los viejos y se mide la antigüedad), ``intentos`` y ``ultimo_error``
(los recálculos que fallan quedan a la vista). Todas nullable o con default: la
tabla es derivada y las filas existentes no pierden nada.

Aditiva e idempotente (convención E8c; el lock de ``migrations/env.py``
serializa a la API y al worker). Comprueba PRESENCIA, no forma.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261013_0002"
down_revision = "20261013_0001"
branch_labels = None
depends_on = None

_TABLA = "product_cost_state"
_INDICE = "ix_product_cost_state_pendientes"


def _insp() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    existentes = {c["name"] for c in _insp().get_columns(_TABLA)}
    if "marcado_en" not in existentes:
        op.add_column(_TABLA, sa.Column("marcado_en", sa.DateTime(timezone=True), nullable=True))
    if "intentos" not in existentes:
        op.add_column(
            _TABLA, sa.Column("intentos", sa.Integer(), nullable=False, server_default="0")
        )
    if "ultimo_error" not in existentes:
        op.add_column(_TABLA, sa.Column("ultimo_error", sa.String(300), nullable=True))
    indices = {ix["name"] for ix in _insp().get_indexes(_TABLA)}
    if _INDICE not in indices:
        # El drenaje busca los pendientes viejos de un tenant.
        op.create_index(_INDICE, _TABLA, ["tenant_id", "marcado_en"])


def downgrade() -> None:
    indices = {ix["name"] for ix in _insp().get_indexes(_TABLA)}
    if _INDICE in indices:
        op.drop_index(_INDICE, table_name=_TABLA)
    existentes = {c["name"] for c in _insp().get_columns(_TABLA)}
    for col in ("ultimo_error", "intentos", "marcado_en"):
        if col in existentes:
            op.drop_column(_TABLA, col)
