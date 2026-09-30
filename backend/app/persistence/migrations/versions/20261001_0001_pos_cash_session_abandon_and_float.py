"""Turno de caja: cerrar sin arqueo y fondo declarado (B8, pantalla).

Revision ID: 20261001_0001
Revises: 20260929_0001

Dos cambios chicos que la pantalla del turno necesita:

1. **``status`` acepta ``ABANDONED``.** La caja creaba un turno nuevo en cada
   montaje de la pantalla, así que producción tiene turnos abiertos con fondo
   $0 que nadie va a contar. Cerrarlos con un conteo inventado firmaría un
   arqueo falso; ``ABANDONED`` los cierra dejando esperado, contado y
   diferencia en NULL.

2. **``float_declared_at``.** La apertura se puede hacer sin red, así que una
   venta del turno puede llegar antes que ella y adoptarlo con fondo $0. Esta
   columna distingue "fondo declarado" de "fondo por adopción": la primera
   apertura explícita que llega fija el fondo aunque el turno ya exista. Sin
   backfill: en los turnos existentes el fondo nunca se declaró.

Aditiva e idempotente (convención E8c; el lock de ``migrations/env.py``
serializa a la API y al worker). El CHECK se reemplaza con
``DROP CONSTRAINT IF EXISTS`` + ``ADD``, idempotente por construcción. Sólo en
PostgreSQL: en SQLite el CHECK sale del modelo.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261001_0001"
down_revision = "20260929_0001"
branch_labels = None
depends_on = None

_TABLA = "pos_cash_sessions"
_COLUMNA = "float_declared_at"
_CHECK = "ck_pos_cash_sessions_status"


def _columnas() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLA)}


def upgrade() -> None:
    if _COLUMNA not in _columnas():
        op.add_column(
            _TABLA,
            sa.Column(_COLUMNA, sa.DateTime(timezone=True), nullable=True),
        )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(f"ALTER TABLE {_TABLA} DROP CONSTRAINT IF EXISTS {_CHECK}")
        op.execute(
            f"ALTER TABLE {_TABLA} ADD CONSTRAINT {_CHECK} "
            "CHECK (status IN ('OPEN','CLOSED','ABANDONED'))"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        # Un turno ABANDONED no entra en el CHECK viejo: el downgrade fallaría a
        # mitad de camino. Se lo lleva a CLOSED, que con los importes en NULL
        # sigue diciendo que no hubo arqueo.
        op.execute(f"UPDATE {_TABLA} SET status = 'CLOSED' WHERE status = 'ABANDONED'")
        op.execute(f"ALTER TABLE {_TABLA} DROP CONSTRAINT IF EXISTS {_CHECK}")
        op.execute(
            f"ALTER TABLE {_TABLA} ADD CONSTRAINT {_CHECK} "
            "CHECK (status IN ('OPEN','CLOSED'))"
        )
    if _COLUMNA in _columnas():
        op.drop_column(_TABLA, _COLUMNA)
