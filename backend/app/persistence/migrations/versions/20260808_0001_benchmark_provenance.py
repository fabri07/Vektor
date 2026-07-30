"""Procedencia y confianza del benchmark en cada snapshot de health score.

Hasta acá `confidence_level` respondía una sola pregunta: "¿qué tan completos
están los datos del negocio?". No decía nada sobre la vara contra la que se lo
midió, así que un negocio con datos impecables medido contra un umbral que nadie
fundamentó salía `HIGH` igual.

Estas dos columnas guardan las dos piezas que faltaban para poder auditar un
score viejo:

* ``benchmark_provenance`` — de dónde salió el umbral de margen:
  ``static_sourced`` (JSON con fuente sectorial declarada), ``static_provisional``
  (JSON sin fuente), ``tenant_override`` (el objetivo que puso el dueño) o
  ``data_driven`` (percentiles cross-tenant; hoy no puntúa).
* ``benchmark_confidence`` — la confianza que aporta esa procedencia.

``confidence_level``, que ya existía, pasa a ser la confianza EFECTIVA: el mínimo
entre la de los datos y la del benchmark. Es el valor que todos los consumidores
ya gatean, y volverlo más estricto es la dirección segura.

Additive y nullable, sin backfill: los snapshots viejos se calcularon sin esta
distinción y NO se les puede inferir la procedencia sin reescribir historia —
quedan en NULL, que es lo honesto ("no se registró"), y se llenan solos con el
próximo recálculo de cada tenant.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260808_0001"
down_revision = "20260807_0001"
branch_labels = None
depends_on = None

_TABLE = "health_score_snapshots"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("benchmark_provenance", sa.String(length=24), nullable=True),
    )
    op.add_column(
        _TABLE,
        sa.Column("benchmark_confidence", sa.String(length=8), nullable=True),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "benchmark_confidence")
    op.drop_column(_TABLE, "benchmark_provenance")
