"""``data_repair_runs.queued_at`` — ancla del cronómetro de relectura

Revision ID: 20260826_0001
Revises: 20260819_0001
Create Date: 2026-08-26

Contexto
--------
Migración ADDITIVE — agrega ``queued_at`` (nullable, sin backfill) a
``data_repair_runs``.

El endpoint de estado de relectura servía ``applying_since`` desde
``updated_at``, que se pisa con un ``now()`` explícito DOS veces: al entrar a
``QUEUED`` y otra vez en el reclamo atómico ``QUEUED->APPLYING`` del worker. Un
run que esperó 30s en cola mostraba "empezado hace 30s" y saltaba a "hace 0s"
cuando el worker lo tomaba — el reloj iba para atrás justo cuando algo
finalmente empezaba a pasar.

No alcanza con dejar de pisar ``updated_at``: el sweep de runs huérfanos
(``sweep_stale_reread_runs``) mide staleness por último toque real, así que esa
columna TIENE que seguir moviéndose. Son dos preguntas distintas — "¿desde
cuándo está en curso?" y "¿cuándo se tocó por última vez?" — y una sola columna
no puede responder las dos. Precedente idéntico ya resuelto en el codebase:
``uploaded_files.import_started_at`` es columna separada de ``updated_at`` por
exactamente este motivo (migración ``20260801_0001``).

``queued_at`` se escribe UNA sola vez, al entrar a ``QUEUED``, y nunca más.
Nullable sin backfill: los runs que ya estaban en vuelo al momento del deploy
no lo tienen, y el endpoint cae explícitamente a ``updated_at`` para esos (ver
``api/v1/ingestion.py``) — un ancla imprecisa es mejor que un contexto vacío.
Forward-safe: no reescribe ni depende de datos existentes, y el código viejo
ignora la columna.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260826_0001"
down_revision = "20260819_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "data_repair_runs",
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("data_repair_runs", "queued_at")
