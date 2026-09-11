"""``import_attempts.capabilities_json`` — capacidades efectivas al confirmar (E7a-lite)

Revision ID: 20260910_0005
Revises: 20260910_0004
Create Date: 2026-09-10

Qué cierra
----------
Las compuertas de rollout del importador se leen en el momento de importar, desde
el entorno del proceso que importa. Con la ejecución asíncrona, el registro pasa
por ``vektor-api`` y la ejecución por ``vektor-worker``: dos procesos, dos
entornos, redespliegues en paralelo sin orden garantizado. Alguien puede confirmar
viendo el preview con el motor de costos de compra encendido y que el worker lo
importe con el motor apagado — números distintos de los que mostró la pantalla,
sin un solo error a la vista.

Esta columna guarda qué capacidades estaban efectivas cuando el usuario dijo que
sí, para que el ejecutor pueda verificar que siguen siendo las mismas antes de
escribir nada.

Additive y NULLABLE a propósito
-------------------------------
Los intentos registrados antes de este deploy quedan con ``NULL``, y ``NULL``
significa "no hay con qué comparar": esos intentos se ejecutan como antes. Hacerlos
fallar sería romper en el deploy justamente los intentos en vuelo que la ruta
asíncrona existe para no perder.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260910_0005"
down_revision = "20260910_0004"
branch_labels = None
depends_on = None

_JSONB = postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.add_column(
        "import_attempts",
        sa.Column("capabilities_json", _JSONB, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("import_attempts", "capabilities_json")
