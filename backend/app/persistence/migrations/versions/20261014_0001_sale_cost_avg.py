"""Costo promedio de la venta en ``sales_entries`` (M·P3).

Revision ID: 20261014_0001
Revises: 20261013_0002

Cada venta guarda, al lado del costo de reposición congelado de M·2a (que NO se
toca), el costo promedio de su producto a la fecha de la venta
(``product_cost_history``). Es un DATO derivado: cuando se republica el
historial del producto, se recalcula. TODAS nullable: ``cost_avg_estado`` NULL =
nunca se calculó (ventas anteriores a M·P3, hasta el backfill).
``cost_avg_pendiente`` es interno: el valor es el último publicado y el worker
lo va a actualizar (la UI no muestra «recalculando», decisión del dueño).

Aditiva e idempotente (convención E8c; el lock de ``migrations/env.py``
serializa a la API y al worker). Comprueba PRESENCIA, no forma. Los CHECK van
``NOT VALID`` → ``VALIDATE`` (las columnas nuevas están en NULL). SQLite ignora
los CHECK.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20261014_0001"
down_revision = "20261013_0002"
branch_labels = None
depends_on = None

_TABLA = "sales_entries"

_COLUMNAS: tuple[tuple[str, sa.types.TypeEngine[object]], ...] = (
    ("cost_avg_unit_ars", sa.Numeric(12, 2)),
    ("cost_avg_ars", sa.Numeric(12, 2)),
    ("cost_avg_estado", sa.String(20)),
    ("cost_avg_aproximado", sa.Boolean()),
    ("cost_avg_motivo", sa.String(200)),
    ("cost_avg_pendiente", sa.Boolean()),
)

# Con NULL el CHECK da NULL y pasa.
_CHECKS: tuple[tuple[str, str], ...] = (
    (
        "ck_sales_entries_cost_avg_estado",
        "cost_avg_estado IN ('medido','incompleto','sin_medicion')",
    ),
    ("ck_sales_entries_cost_avg_unit_ars", "cost_avg_unit_ars >= 0"),
    ("ck_sales_entries_cost_avg_ars", "cost_avg_ars >= 0"),
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
