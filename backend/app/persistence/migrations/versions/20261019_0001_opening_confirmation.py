"""Confirmación del stock inicial en ``inventory_movements`` (M·2d).

Revision ID: 20261019_0001
Revises: 20261017_0002

Una apertura (``adjustment`` + ``catalog_initial_stock``) tiene fecha de negocio
CONFIRMADA sólo si el usuario la dio (o el archivo la traía por fila y el resumen
del confirm se la mostró):

- ``opening_confirmed_at``: cuándo se confirmó; NULL = sin confirmar.
- ``opening_confirmed_by_user_id``: quién (SET NULL al borrar el usuario).
- ``opening_date_source``: ``usuario`` (la tipeó o la eligió) | ``archivo`` (la
  fecha de la fila).

Todas nullable. Las aperturas viejas quedan en NULL: no se marcan confirmadas
retroactivamente (como M·1a). Una apertura PENDIENTE además lleva
``occurred_at`` NULL, y su fecha de negocio es la de carga
(``COALESCE(occurred_at, created_at)``).

Aditiva e idempotente (convención E8c; el lock de ``migrations/env.py``
serializa a la API y al worker). Comprueba PRESENCIA, no forma. El CHECK va
``NOT VALID`` → ``VALIDATE`` como en ``20261010_0001`` (las columnas nuevas
están en NULL: no hay nada que rechazar). SQLite ignora los CHECK.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "20261019_0001"
down_revision = "20261017_0002"
branch_labels = None
depends_on = None

_TABLA = "inventory_movements"
_FK = "fk_inventory_movements_opening_confirmed_by_user_id"
_CHECK = "ck_inventory_movements_opening_date_source"
_CHECK_EXPR = "opening_date_source IN ('usuario','archivo')"

_COLUMNAS: tuple[tuple[str, sa.types.TypeEngine[object]], ...] = (
    ("opening_confirmed_at", sa.DateTime(timezone=True)),
    ("opening_confirmed_by_user_id", UUID(as_uuid=True)),
    ("opening_date_source", sa.String(20)),
)


def _insp() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    bind = op.get_bind()
    existentes = {c["name"] for c in _insp().get_columns(_TABLA)}
    for nombre, tipo in _COLUMNAS:
        if nombre not in existentes:
            op.add_column(_TABLA, sa.Column(nombre, tipo, nullable=True))

    if bind.dialect.name == "postgresql":
        fks = {fk["name"] for fk in _insp().get_foreign_keys(_TABLA)}
        if _FK not in fks:
            op.create_foreign_key(
                _FK, _TABLA, "users", ["opening_confirmed_by_user_id"], ["user_id"],
                ondelete="SET NULL",
            )
        presentes = {
            r[0]
            for r in bind.execute(
                sa.text(
                    "SELECT conname FROM pg_constraint"
                    " WHERE conrelid = CAST(:t AS regclass) AND contype = 'c'"
                ),
                {"t": _TABLA},
            )
        }
        if _CHECK not in presentes:
            bind.execute(
                sa.text(
                    f"ALTER TABLE {_TABLA} ADD CONSTRAINT {_CHECK} CHECK ({_CHECK_EXPR}) NOT VALID"
                )
            )
        bind.execute(sa.text(f"ALTER TABLE {_TABLA} VALIDATE CONSTRAINT {_CHECK}"))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text(f"ALTER TABLE {_TABLA} DROP CONSTRAINT IF EXISTS {_CHECK}"))
        bind.execute(sa.text(f"ALTER TABLE {_TABLA} DROP CONSTRAINT IF EXISTS {_FK}"))
    existentes = {c["name"] for c in _insp().get_columns(_TABLA)}
    for nombre, _ in reversed(_COLUMNAS):
        if nombre in existentes:
            op.drop_column(_TABLA, nombre)
