"""ORM model: uploaded_files."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import PGJSONB, Base, TimestampMixin, UUIDPrimaryKeyMixin

# Valid processing_status values
PROCESSING_STATUS_PENDING = "PENDING"
PROCESSING_STATUS_PROCESSING = "PROCESSING"
PROCESSING_STATUS_NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"
PROCESSING_STATUS_NEEDS_COMPLETION = "NEEDS_COMPLETION"
# F4: el confirm tomó el lease y está insertando los datos (import inline en curso).
PROCESSING_STATUS_IMPORTING = "IMPORTING"
PROCESSING_STATUS_DONE = "DONE"
PROCESSING_STATUS_FAILED = "FAILED"
PROCESSING_STATUS_REJECTED = "REJECTED"

# Valid reread_status values (F9a: versionado de ingestión)
REREAD_STATUS_NONE = "NONE"
# PREVIEWED/UP_TO_DATE están reservados para un estado más granular a futuro
# (ej. cuando se defina un criterio de confiabilidad del mapeo re-derivado que
# hoy no existe — ver ``ResolvedRisk`` en ``reread_service.py``). Hoy NINGÚN
# outcome de riesgo (ni siquiera ``NO_RISK_FOUND``) escribe estos dos valores
# a propósito: el diseño actual es deliberadamente conservador y marca todo lo
# que no sea ``REAPPLIED`` como ``NEEDS_REVIEW``, sin excepción.
REREAD_STATUS_PREVIEWED = "PREVIEWED"
REREAD_STATUS_UP_TO_DATE = "UP_TO_DATE"
REREAD_STATUS_NEEDS_REVIEW = "NEEDS_REVIEW"
REREAD_STATUS_AUTO_APPLIED = "AUTO_APPLIED"
REREAD_STATUS_APPLIED = "APPLIED"
REREAD_STATUS_FAILED = "FAILED"


class UploadedFile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "uploaded_files"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
    )
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    s3_key: Mapped[str] = mapped_column(String(1000), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    purpose: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # file_hint: ventas|gastos|stock|general
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="uploaded")

    # ── Ingestion pipeline fields ─────────────────────────────────────────────
    processing_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=PROCESSING_STATUS_PENDING
    )
    parsed_summary_json: Mapped[Any] = mapped_column(PGJSONB, nullable=True, default=None)
    rejection_reason: Mapped[str | None] = mapped_column(String(100), nullable=True, default=None)

    # ── FASE 0: trazabilidad + preservación ───────────────────────────────────
    # Agrupa los eventos del pipeline de este upload en pipeline_events.
    trace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    # SHA-256 del contenido crudo — dedup de re-upload (FASE 1).
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, default=None)
    # Soft delete: el crudo en R2 se preserva aunque el usuario "borre" el archivo.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

    # ── F4: lease del confirm (concurrencia) ──────────────────────────────────
    # Token del intento de import en curso. El confirm hace un CAS
    # NEEDS_CONFIRMATION→IMPORTING seteando este token; la transición final a
    # DONE lo verifica (fencing). NULL = sin import en vuelo.
    import_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, default=None
    )
    # Momento en que se tomó el lease — SEPARADO de updated_at (que se mueve con
    # cualquier escritura). Se usa para el takeover por staleness. Reloj de PG.
    import_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    # Fase del import en curso (inserting/finalizing) — trazabilidad opcional.
    import_phase: Mapped[str | None] = mapped_column(String(30), nullable=True, default=None)

    # ── H15: propiedad del PARSEO (distinta del lease del confirm) ────────────
    # Token del intento de parseo en curso. Lo escribe la adquisición atómica
    # (`_claim_for_processing`, PENDING→PROCESSING con rowcount) y lo verifica
    # CADA escritura de resultado del worker — éxito, rechazo y error.
    #
    # No se reusa `import_attempt_id`: son dos ciclos de vida distintos y el del
    # confirm ya está en uso mientras un archivo se importa. Un `WHERE
    # processing_status = 'PROCESSING'` tampoco alcanzaría como fencing, porque
    # el camino de recuperación (`reprocess_file` devuelve a PENDING lo trabado y
    # reencola) deja legítimamente DOS intentos en ese mismo estado: sin token, el
    # `FAILED` tardío del worker viejo pisa el resultado del que sí terminó.
    #
    # NULL = sin parseo en vuelo, o archivo anterior a la migración `20260906_0001`
    # (esos no son propiedad de nadie y se recuperan por el camino de staleness).
    parse_attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, default=None
    )

    # ── F9a: versionado de lógica de ingestión ───────────────────────────────
    # Qué versión del protocolo de interpretación de ingestión fue usada
    # cuando este archivo se procesó/confirmó. Permite evolucionar el pipeline
    # sin perder trazabilidad (ej. pre/post-F8 riesgo contextual de columnas).
    ingestion_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )
    # La versión más reciente de la preview que se mostró. Separada de
    # ingestion_version para distinguir "reread y visto" de "procesado".
    latest_preview_version: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )
    # Estado del reprocesamiento (reread): NONE, PREVIEWED, UP_TO_DATE, etc.
    # Permite reprocess multipass del archivo sin resubir (F9a+).
    reread_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=REREAD_STATUS_NONE
    )
    # Cuándo se hizo el reprocesamiento más reciente. Reloj de PG.
    reread_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    # Summary del reread (diagnóstico, cambios detectados, etc.). JSONB para
    # flexibilidad: puede contener {has_column_risk_changes: bool, ...} u otro
    # formato según la versión del protocolo que lo generó.
    reread_summary: Mapped[Any] = mapped_column(PGJSONB, nullable=True, default=None)

    def __repr__(self) -> str:
        return (
            f"<UploadedFile tenant={self.tenant_id} name={self.original_filename!r} "
            f"status={self.processing_status!r}>"
        )
