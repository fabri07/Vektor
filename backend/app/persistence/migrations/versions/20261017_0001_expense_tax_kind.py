"""Tipo de impuesto en ``expense_entries`` (M·3-3).

Revision ID: 20261017_0001
Revises: 20261014_0001

``tax_kind`` sólo tiene sentido con ``category = 'TAXES'``: lo necesita el margen
neto, porque el IVA de un Responsable Inscripto no es gasto (sus ventas ya entran
sin IVA) y el resto de los impuestos sí resta. NULL = sin tipo: resta igual y el
margen lo avisa. No hay backfill: los gastos viejos quedan sin tipo.

Aditiva e idempotente (convención E8c; el lock de ``migrations/env.py``
serializa a la API y al worker). Comprueba PRESENCIA, no forma. El CHECK va
``NOT VALID`` → ``VALIDATE`` (la columna nueva está en NULL). SQLite ignora el
CHECK.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261017_0001"
down_revision = "20261014_0001"
branch_labels = None
depends_on = None

_TABLA = "expense_entries"
_COLUMNA = "tax_kind"
_CHECK = "ck_expense_entries_tax_kind"
# Con NULL el CHECK da NULL y pasa.
_EXPR = "tax_kind IN ('iibb','monotributo','iva','tasa_municipal','otro')"


def _insp() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def upgrade() -> None:
    bind = op.get_bind()
    existentes = {c["name"] for c in _insp().get_columns(_TABLA)}
    if _COLUMNA not in existentes:
        op.add_column(_TABLA, sa.Column(_COLUMNA, sa.String(20), nullable=True))

    if bind.dialect.name == "postgresql":
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
                sa.text(f"ALTER TABLE {_TABLA} ADD CONSTRAINT {_CHECK} CHECK ({_EXPR}) NOT VALID")
            )
        bind.execute(sa.text(f"ALTER TABLE {_TABLA} VALIDATE CONSTRAINT {_CHECK}"))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(sa.text(f"ALTER TABLE {_TABLA} DROP CONSTRAINT IF EXISTS {_CHECK}"))
    existentes = {c["name"] for c in _insp().get_columns(_TABLA)}
    if _COLUMNA in existentes:
        op.drop_column(_TABLA, _COLUMNA)
