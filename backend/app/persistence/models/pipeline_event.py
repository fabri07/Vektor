"""ORM model: pipeline_events — traza append-only del pipeline de ingestión. FASE 0.

Un evento por (trace_id, stage). Permite reconstruir el ciclo completo de un
upload y registrar rechazos sin descartar datos en silencio. Las filas aceptadas
NO se persisten acá: se reconstruyen del crudo en R2 + los registros insertados.
Solo se persiste el detalle de rechazos en `detail` (stage="reject").
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import PGJSONB, Base, UUIDPrimaryKeyMixin

# Stages canónicos del pipeline (orden lógico)
STAGE_UPLOAD = "upload"
STAGE_PARSE = "parse"
STAGE_VALIDATE = "validate"
STAGE_CONFIRM = "confirm"
STAGE_PERSIST = "persist"
STAGE_REJECT = "reject"
# FASE 2: decisiones de las capas LLM de ingestión (mapeo de columnas + tipo de
# archivo). Agrupa la traza de "qué decidió el LLM y si pisó lo determinístico".
STAGE_MAPPING = "mapping"

PIPELINE_STAGES = frozenset(
    {
        STAGE_UPLOAD,
        STAGE_PARSE,
        STAGE_VALIDATE,
        STAGE_CONFIRM,
        STAGE_PERSIST,
        STAGE_REJECT,
        STAGE_MAPPING,
    }
)


class PipelineEvent(UUIDPrimaryKeyMixin, Base):
    """Evento append-only de un stage del pipeline de ingestión."""

    __tablename__ = "pipeline_events"

    # Agrupa todos los eventos del ciclo de vida de un upload.
    trace_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # file_id nullable: el evento `upload` puede emitirse antes de tener el id en algunos flujos.
    file_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    stage: Mapped[str] = mapped_column(String(20), nullable=False)
    rows_in: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rows_out: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rows_rejected: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # detail: para rechazos {row_number, raw_row, reason}; errores; metadata libre.
    detail: Mapped[dict[str, Any] | None] = mapped_column(PGJSONB, nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_pipeline_events_trace", "trace_id"),
        Index("ix_pipeline_events_tenant_created", "tenant_id", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<PipelineEvent trace={self.trace_id} stage={self.stage!r} "
            f"in={self.rows_in} out={self.rows_out} rej={self.rows_rejected}>"
        )
