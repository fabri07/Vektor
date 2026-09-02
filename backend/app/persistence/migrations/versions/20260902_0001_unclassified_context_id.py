"""``unclassified_records.context_id`` — de qué hoja salió cada fila de "Otros"

Revision ID: 20260902_0001
Revises: 20260831_0002
Create Date: 2026-09-02

Contexto
--------
Migración ADDITIVE — agrega ``context_id`` (nullable, sin backfill) a
``unclassified_records``.

Hasta acá la única pista del origen de una fila capturada era
``context_label``, que NO es una identidad: es el nombre de la hoja
(``"Ganancias"``) en el camino multi-hoja y un MOTIVO en castellano
(``"Fila sin monto: no se pudo registrar..."``) en la docena de capturas por
fila. Dos hojas pueden llamarse igual, un rename lo cambia, y el motivo no
identifica hoja alguna. Sobre eso no se puede decidir a qué contexto pertenece
un pendiente.

Hace falta porque la relectura tiene que poder descartar los pendientes de un
contexto que NO se importó (hoja derivada, o desmarcada por el usuario). Ese
join es exacto por ``context_id`` — el mismo identificador que ya usan
``mapping_contexts``, las decisiones de riesgo de columna (F8) y el ancla de
huella de fila.

Nullable y sin backfill a propósito: los 2.288 registros que Asteria ya tiene
se escribieron sin la columna y no hay de dónde derivarles el ``context_id``
sin re-parsear el archivo. Para esos, y solo para esos, la relectura cae al
match por ``context_label`` acotado a (tenant, archivo) — ver
``reread_service._descartar_pendientes_de_contextos_no_importados``. El
fallback es explícitamente legacy: las capturas nuevas traen ``context_id`` y
no lo necesitan.

Forward-safe: no reescribe ni depende de datos existentes, y el código viejo
ignora la columna.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260902_0001"
down_revision = "20260831_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "unclassified_records",
        sa.Column("context_id", sa.String(length=100), nullable=True),
    )
    # El descarte de la relectura filtra por (archivo, contexto) sobre los
    # PENDING de UN archivo. El índice existente es (tenant_id, status), que
    # no cubre el archivo: sin esto, limpiar un archivo con miles de pendientes
    # escanea todos los del tenant.
    op.create_index(
        "ix_unclassified_records_file_context",
        "unclassified_records",
        ["uploaded_file_id", "context_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_unclassified_records_file_context", "unclassified_records")
    op.drop_column("unclassified_records", "context_id")
