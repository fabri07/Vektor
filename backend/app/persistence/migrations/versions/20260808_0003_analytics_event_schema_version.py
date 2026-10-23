"""`schema_version` en analytics_events: qué contrato escribió cada fila.

Reemplaza al corte por fecha (`_TRUSTED_EVENTS_SINCE`) que traía el fix anterior.
Ese corte resolvía el problema correcto —las filas viejas guardan `margin_ratio =
0.0` para negocios sin ventas, y ese cero fabricado es indistinguible de un cero
genuino una vez en la tabla— pero lo resolvía con una constante de calendario
elegida a mano, y por lo tanto con un supuesto sobre cuándo iba a ocurrir el
deploy. Si el deploy caía después de esa fecha, las filas del código viejo
entraban como confiables y nada fallaba.

La versión no es un supuesto: la escribe el mismo commit que cambia la semántica.

* Filas preexistentes → ``1`` por el ``server_default``.
* Filas nuevas → ``2``, que es lo que pone el ORM (``EVENT_SCHEMA_VERSION``).

El ``server_default`` se DEJA puesto a propósito, no se limpia después del
backfill: es lo que cubre la ventana entre este preDeploy y el arranque de la
versión nueva de la app. En esos minutos el código viejo sigue insertando, no
manda la columna, y la base le pone ``1`` — que es exactamente lo que esas filas
son. La dirección del default es la conservadora: un escritor que no se
pronuncia se asume viejo.

En Postgres ≥ 11 ``ADD COLUMN NOT NULL DEFAULT`` no reescribe la tabla, así que
es instantáneo y se puede aplicar con la versión anterior sirviendo tráfico.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260808_0003"
down_revision = "20260808_0002"
branch_labels = None
depends_on = None

_TABLE = "analytics_events"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "schema_version")
