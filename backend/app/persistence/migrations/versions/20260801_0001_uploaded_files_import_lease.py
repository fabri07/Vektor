"""Columnas de lease del confirm de ingestión (Fase 4 — concurrencia)

Revision ID: 20260801_0001
Revises: 20260731_0004
Create Date: 2026-08-01

Contexto
--------
Migración ADDITIVE — agrega a ``uploaded_files`` las tres columnas del lease
per-file que usa el confirm de ingestión (Fase 4) para que dos confirms del
mismo archivo no corran en paralelo:

- ``import_attempt_id``  : token del intento de import en curso (fencing).
- ``import_started_at``  : cuándo se tomó el lease — SEPARADO de ``updated_at``
                           (que se mueve con cualquier escritura). Habilita el
                           takeover por staleness. Reloj de PostgreSQL.
- ``import_phase``        : fase del import (inserting/finalizing), trazabilidad.

Todas nullable, sin backfill, forward-safe. ``processing_status`` es
``String(30)`` sin CHECK, así que el nuevo estado ``IMPORTING`` no requiere
tocar constraints. No reescribe ni depende de datos existentes.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260801_0001"
down_revision = "20260731_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "uploaded_files",
        sa.Column("import_attempt_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "uploaded_files",
        sa.Column("import_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "uploaded_files",
        sa.Column("import_phase", sa.String(30), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("uploaded_files", "import_phase")
    op.drop_column("uploaded_files", "import_started_at")
    op.drop_column("uploaded_files", "import_attempt_id")
