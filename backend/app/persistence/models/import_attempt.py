"""ORM: el intento de importación y su orden de ejecución.

Dos tablas y una sola transacción
---------------------------------
``import_attempts`` guarda **qué se pidió** (la solicitud congelada, sobre qué
revisión del archivo) y **cómo va**. ``import_outbox`` guarda **la orden de
ejecutarlo**, y se escribe en la MISMA transacción que el intento.

Que sean dos y no una es lo que cierra la ventana que E3 dejó abierta a
propósito. Publicar a Celery desde adentro de la transacción no sirve —el broker
no participa del commit, así que un mensaje puede salir por una transacción que
después se revierte— y publicar después del commit tampoco —si el proceso muere
entre el commit y el envío, el trabajo quedó registrado y nadie lo va a ejecutar.
Con la orden en la base, el commit es lo único que decide: si commiteó, la orden
existe y un publicador la va a entregar tarde o temprano; si no commiteó, no
existe ni el intento ni la orden.

El precio, y por qué se acepta
------------------------------
El publicador puede entregar la MISMA orden dos veces (murió después de enviar y
antes de marcarla publicada). Eso es aceptable y está previsto: el ejecutor
reclama el intento con un token, y una segunda entrega no encuentra nada que
reclamar. No se promete "exactly once" en el transporte — la garantía está en la
adquisición y el fencing, que es donde se puede sostener.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.import_attempt import PENDIENTE
from app.persistence.db.base import PGJSONB, Base


class ImportAttempt(Base):
    """Una intención de importar, con su versión congelada y su progreso."""

    __tablename__ = "import_attempts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("uploaded_files.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: La clave con la que el cliente identifica SU petición. Repetirla devuelve
    #: este mismo intento en vez de crear otro.
    request_key: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    #: Huella del contenido de la solicitud. Misma clave + misma huella = la misma
    #: petición; misma clave + huella distinta = conflicto, y se dice.
    payload_hash: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    #: **La solicitud entera, congelada.** El ejecutor consume esto y no el
    #: borrador: un usuario que edita el mapeo mientras su import está encolado no
    #: puede cambiar lo que ya confirmó.
    payload_json: Mapped[dict[str, Any]] = mapped_column(PGJSONB, nullable=False)
    #: Versión del FORMATO del sobre. Cambiar la forma del payload sin subirla
    #: deja intentos viejos que un ejecutor nuevo interpreta mal en silencio.
    payload_version: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=1, server_default="1"
    )
    #: Sobre qué revisión del archivo se confirmó.
    ingestion_version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    preview_version: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    #: **Qué compuertas de rollout estaban efectivas cuando el usuario confirmó.**
    #: El ejecutor las verifica antes de escribir: si cambiaron, no importa nada y
    #: lo dice, en vez de guardar números distintos de los que mostró el preview.
    #: ``NULL`` = intento anterior a esta columna, que se ejecuta como antes (no hay
    #: con qué comparar, y romper los intentos en vuelo de un deploy es lo que la
    #: ruta asíncrona existe para evitar). Ver ``domain/import_capabilities.py``.
    capabilities_json: Mapped[dict[str, Any] | None] = mapped_column(PGJSONB, nullable=True)
    #: A qué versión del ARCHIVO se le dijo que sí. Si el archivo se releyó entre
    #: el confirm y la ejecución, importar contra el contenido nuevo sería
    #: importar algo que el usuario nunca vio.
    file_content_hash: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)

    status: Mapped[str] = mapped_column(
        sa.String(20), nullable=False, default=PENDIENTE, server_default=PENDIENTE
    )
    #: En qué anda ahora mismo (``validando``, ``importando``, ``publicando``…) y
    #: cuánto lleva. Es lo que la pantalla muestra mientras espera.
    phase: Mapped[str | None] = mapped_column(sa.String(30), nullable=True)
    rows_total: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    rows_done: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default="0"
    )

    #: Error ESTRUCTURADO: el código se cuenta y decide si conviene reintentar, el
    #: detalle lo lee una persona. Un `str(exc)` no sirve para ninguna de las dos.
    error_code: Mapped[str | None] = mapped_column(sa.String(30), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    #: Lo que hoy devuelve el confirm sincrónico (counts + warnings), para que la
    #: consulta de estado pueda entregar el MISMO resultado sin re-ejecutar nada.
    result_json: Mapped[dict[str, Any] | None] = mapped_column(PGJSONB, nullable=True)

    attempts: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default="0"
    )
    max_attempts: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=3, server_default="3"
    )

    #: Fencing del EJECUTOR (distinto de la clave de petición, que identifica al
    #: cliente). Una segunda entrega de la misma orden no encuentra nada que
    #: reclamar; un ejecutor vencido no puede publicar encima de su reemplazo.
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        onupdate=sa.func.now(),
        nullable=False,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )

    __table_args__ = (
        # El candado de la idempotencia de PETICIÓN: dos requests con la misma
        # clave no pueden crear dos intentos, ni siquiera llegando a la vez.
        sa.UniqueConstraint("tenant_id", "request_key", name="uq_import_attempts_request_key"),
        # La consulta del publicador y la de recuperación de huérfanos.
        sa.Index("ix_import_attempts_status", "status", "lease_expires_at"),
        sa.Index("ix_import_attempts_file", "tenant_id", "file_id"),
    )


class ImportOutbox(Base):
    """La orden de ejecutar un intento, escrita con él en la misma transacción.

    ``published_at IS NULL`` = pendiente de entregar. El publicador la toma, la
    manda al broker y la marca; si muere en el medio, la orden sigue pendiente y
    se vuelve a entregar. Entregar dos veces es aceptable — lo cierra el lease del
    ejecutor — y perder una no lo sería.
    """

    __tablename__ = "import_outbox"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("import_attempts.id", ondelete="CASCADE"),
        nullable=False,
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    published_at: Mapped[datetime | None] = mapped_column(
        sa.TIMESTAMP(timezone=True), nullable=True
    )
    #: Cuántas veces se intentó publicar. Un contador que crece sin que
    #: ``published_at`` se llene señala un broker caído, no un archivo malo.
    publish_attempts: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default="0"
    )
    last_error: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        sa.TIMESTAMP(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
    )

    __table_args__ = (
        # Un intento tiene UNA orden: hace idempotente el registro ante reintentos
        # del propio confirm.
        sa.UniqueConstraint("attempt_id", name="uq_import_outbox_attempt"),
        # La consulta del publicador: las pendientes, más viejas primero.
        sa.Index("ix_import_outbox_pendientes", "published_at", "created_at"),
    )
