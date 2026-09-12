"""``import_attempts`` + ``import_outbox`` — ejecución duradera (E6c-3)

Revision ID: 20260910_0004
Revises: 20260910_0003
Create Date: 2026-09-10

Contexto
--------
Migración ADDITIVE: dos tablas nuevas, ninguna columna existente tocada. Nada las
lee todavía; el confirm y el ejecutor las usan en los commits siguientes.

Qué cierran
-----------
El confirm de ingestión corre el import **dentro del request HTTP**, con un
cliente de hasta 16 minutos. Si esa conexión se corta —la pestaña, el proxy, el
wifi— no hay adónde volver a preguntar: el trabajo puede haber terminado, puede
estar a medias, y desde afuera se ven igual. Reintentar por timeout no reanuda: crea
una importación nueva.

``import_attempts`` le da identidad a la intención: se consulta, se reanuda y deja
historia aunque haya otro intento después. Guarda la solicitud **congelada**
(``payload_json``) para que el ejecutor consuma la versión que el usuario
confirmó y no lo que el borrador diga cuando le toque correr.

Por qué la orden va en una tabla y no directo al broker
--------------------------------------------------------
Publicar a Celery desde adentro de la transacción no sirve: el broker no participa
del commit, así que puede salir un mensaje por una transacción que después se
revierte. Publicar después del commit tampoco: si el proceso muere entre el commit
y el envío, el trabajo quedó registrado y nadie lo va a ejecutar. Es la ventana
que E3 dejó abierta a propósito.

Con la orden en ``import_outbox``, el commit es lo único que decide. Si commiteó,
la orden existe y un publicador reintentable la va a entregar; si no commiteó, no
existe ni el intento ni la orden. El precio es que la misma orden se puede
entregar dos veces —el publicador murió después de enviar y antes de marcarla— y
eso está previsto: el ejecutor reclama con token y la segunda entrega no encuentra
nada que reclamar. **No se promete "exactly once" de transporte**; la garantía
está en la adquisición y el fencing, que es donde se puede sostener.

Los dos únicos
--------------
El snapshot es COMPLETO: además de las decisiones, el intento congela a qué
versión del archivo se le dijo que sí (``file_content_hash``) y con qué formato de
sobre se guardó (``payload_version``). Sin lo primero, una relectura entre el
confirm y la ejecución haría importar un contenido que el usuario nunca vio; sin
lo segundo, cambiar la forma del payload dejaría intentos viejos que un ejecutor
nuevo interpreta mal sin darse cuenta.

``uq_import_attempts_request_key`` es el candado de la idempotencia de PETICIÓN:
dos requests con la misma clave no pueden crear dos intentos ni llegando a la vez.
``uq_import_outbox_attempt`` hace idempotente el registro de la orden ante un
reintento del propio confirm.

Orden de despliegue
-------------------
Sin riesgo: las tablas nacen vacías y sin lectores. Un servicio viejo contra el
esquema nuevo las ignora; el ejecutor nuevo no existe hasta el commit siguiente.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

#: JSONB en Postgres, JSON en SQLite (la suite). Mismo criterio que `PGJSONB`
#: del ORM: el esquema que crea la suite tiene que ser el mismo que corre en prod.
_JSONB = sa.JSON().with_variant(JSONB, "postgresql")

revision = "20260910_0004"
down_revision = "20260910_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "import_attempts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "file_id",
            UUID(as_uuid=True),
            sa.ForeignKey("uploaded_files.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("request_key", sa.String(128), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("payload_json", _JSONB, nullable=False),
        #: Versión del FORMATO del payload congelado. Sin esto, cambiar la forma
        #: del sobre deja intentos viejos que un ejecutor nuevo interpreta mal en
        #: silencio; con esto, los rechaza diciendo por qué.
        sa.Column("payload_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("ingestion_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("preview_version", sa.Integer, nullable=True),
        #: A QUÉ versión del archivo se le dijo que sí. Si el archivo se releyó
        #: entre el confirm y la ejecución, importar contra el contenido nuevo
        #: sería importar algo que el usuario nunca vio.
        sa.Column("file_content_hash", sa.String(64), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDIENTE"),
        sa.Column("phase", sa.String(30), nullable=True),
        sa.Column("rows_total", sa.Integer, nullable=True),
        sa.Column("rows_done", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(30), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("result_json", _JSONB, nullable=True),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default="3"),
        sa.Column("lease_token", UUID(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "request_key", name="uq_import_attempts_request_key"),
    )
    op.create_index("ix_import_attempts_status", "import_attempts", ["status", "lease_expires_at"])
    op.create_index("ix_import_attempts_file", "import_attempts", ["tenant_id", "file_id"])

    op.create_table(
        "import_outbox",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "attempt_id",
            UUID(as_uuid=True),
            sa.ForeignKey("import_attempts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("publish_attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.UniqueConstraint("attempt_id", name="uq_import_outbox_attempt"),
    )
    op.create_index(
        "ix_import_outbox_pendientes", "import_outbox", ["published_at", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_import_outbox_pendientes", table_name="import_outbox")
    op.drop_table("import_outbox")
    op.drop_index("ix_import_attempts_file", table_name="import_attempts")
    op.drop_index("ix_import_attempts_status", table_name="import_attempts")
    op.drop_table("import_attempts")
