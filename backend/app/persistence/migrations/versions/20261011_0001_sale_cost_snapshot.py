"""Costo congelado por línea de venta en ``sales_entries`` (M·2a).

Revision ID: 20261011_0001
Revises: 20261010_0001

Cada venta con producto guarda, al confirmarse, el costo de la fuente elegible a
su fecha de negocio y si es una base comparable confirmada
(``costo_de_venta.py``). Una compra posterior no lo cambia y las ventas pasadas
no se revalúan. TODAS nullable: NULL = «sin costear» (las ventas anteriores a
M·2a y las que no tienen producto). Las viejas NO se costean acá: es un script
aparte (M·2c), siempre como estimación.

Aditiva e idempotente (convención E8c; el lock de ``migrations/env.py``
serializa a la API y al worker). Comprueba PRESENCIA, no forma. Los CHECK van
``NOT VALID`` → ``VALIDATE`` bajo el ACCESS EXCLUSIVE que ya tomó el ``ADD
COLUMN`` (las columnas nuevas están en NULL: el ``VALIDATE`` no rechaza nada).
SQLite ignora los CHECK.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261011_0001"
down_revision = "20261010_0001"
branch_labels = None
depends_on = None

_TABLA = "sales_entries"

_ORIGENES = "('compra','stock_inicial','manual','catalogo','costo_cargado','sin_costo')"

_COLUMNAS: tuple[tuple[str, sa.types.TypeEngine[object]], ...] = (
    ("cost_unit_ars", sa.Numeric(12, 2)),
    ("cost_ars", sa.Numeric(12, 2)),
    ("cost_confirmed", sa.Boolean()),
    ("cost_origin", sa.String(20)),
    ("cost_reason", sa.String(200)),
    ("cost_snapshot_version", sa.SmallInteger()),
)

# Con NULL el CHECK da NULL y pasa: NULL = sin costear.
_CHECKS: tuple[tuple[str, str], ...] = (
    ("ck_sales_entries_cost_origin", f"cost_origin IN {_ORIGENES}"),
    ("ck_sales_entries_cost_unit_ars", "cost_unit_ars >= 0"),
    ("ck_sales_entries_cost_ars", "cost_ars >= 0"),
    ("ck_sales_entries_cost_snapshot_version", "cost_snapshot_version >= 1"),
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
        for nombre, expr in _CHECKS:
            if nombre not in presentes:
                bind.execute(
                    sa.text(
                        f"ALTER TABLE {_TABLA} ADD CONSTRAINT {nombre} CHECK ({expr}) NOT VALID"
                    )
                )
            bind.execute(sa.text(f"ALTER TABLE {_TABLA} VALIDATE CONSTRAINT {nombre}"))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for nombre, _ in _CHECKS:
            bind.execute(sa.text(f"ALTER TABLE {_TABLA} DROP CONSTRAINT IF EXISTS {nombre}"))
    existentes = {c["name"] for c in _insp().get_columns(_TABLA)}
    for nombre, _ in reversed(_COLUMNAS):
        if nombre in existentes:
            op.drop_column(_TABLA, nombre)
