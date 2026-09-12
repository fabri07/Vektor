"""``uploaded_files.parse_attempt_id`` — propiedad del parseo

Revision ID: 20260906_0001
Revises: 20260903_0001
Create Date: 2026-09-06

Contexto
--------
Migración ADDITIVE — columna nueva nullable, sin backfill, sin tocar ninguna
existente.

El worker de ingestión escribía ``processing_status`` sin comprobarlo:
``_load_and_lock`` hacía un ``SELECT`` plano —sin ``FOR UPDATE``, pese al
nombre— y asignaba ``PROCESSING`` de forma incondicional, sin ``rowcount``, sin
guard de estado y sin filtrar ``deleted_at``. Con ``task_acks_late=True`` una
re-entrega podía llevar un archivo ``DONE`` o ``REJECTED`` de vuelta a
``PROCESSING`` y de ahí a ``NEEDS_CONFIRMATION``, que es un estado que el CAS de
``acquire_import_lease`` acepta.

El estado se protege en dos lugares distintos y hacen falta los dos:

* la **adquisición** ya se puede cerrar sin columna nueva (``UPDATE ... WHERE
  processing_status = 'PENDING'`` + ``rowcount``);
* la **escritura del resultado** no. Un ``WHERE processing_status =
  'PROCESSING'`` no distingue entre dos intentos que están AMBOS en
  ``PROCESSING``, que es justo lo que deja el camino de recuperación:
  ``reprocess_file`` devuelve a ``PENDING`` un archivo trabado (``updated_at``
  más viejo que 300 s) y encola de nuevo, así que el worker viejo —el que quedó
  colgado, no muerto— sigue vivo y con derecho aparente a escribir. Sin un token
  de propiedad, su ``FAILED`` tardío pisa el ``NEEDS_CONFIRMATION`` del intento
  que sí terminó.

``parse_attempt_id`` es ese token: lo escribe la adquisición y lo verifica cada
escritura de resultado (éxito, rechazo y error). Es **independiente de
``import_attempt_id``**, que pertenece al lease del confirm
(``ingestion_lease_service``) y tiene otro ciclo de vida: mezclarlos haría que un
reparse invalide el fencing de un import en vuelo.

**Nullable y sin backfill** a propósito. Consecuencia declarada: los archivos que
hoy están en ``PROCESSING`` quedan con ``parse_attempt_id = NULL`` y ninguna
escritura de resultado va a reconocerlos como propios — terminan por el camino de
recuperación (``reprocess_file`` los devuelve a ``PENDING`` pasados los 300 s), que
es exactamente lo que ya hacían cuando el worker moría. No se backfillea un token
inventado: eso le daría propiedad a un intento cuyo dueño real no se conoce.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "20260906_0001"
down_revision = "20260903_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "uploaded_files",
        sa.Column("parse_attempt_id", UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("uploaded_files", "parse_attempt_id")
