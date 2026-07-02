"""ORM model: chat_coverage_gaps — backlog de producto desde los rechazos del chat.

Cada vez que el chat rechaza o no cubre una consulta (fuera de scope, intent
desconocido, baja confianza, sin datos, contexto de UI faltante), se registra
acá como gap de cobertura. Insert-only best-effort: la escritura NUNCA puede
romper ni demorar la respuesta al usuario (ver CoverageGapService).
"""

import uuid
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, Float, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import PGJSONB, Base, TimestampMixin, UUIDPrimaryKeyMixin

# Set cerrado de razones de fallback. Agregar una razón nueva = tocar también el
# CheckConstraint de la migración correspondiente.
COVERAGE_GAP_REASONS = (
    "out_of_scope",
    "intent_desconocido",
    "baja_confianza",
    "sin_datos",
    "ui_context_missing",
    "advice_blocked",
)


class ChatCoverageGap(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "chat_coverage_gaps"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Nullable: el logging es best-effort y no debe fallar si el user no está a mano.
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    original_message: Mapped[str] = mapped_column(Text, nullable=False)
    classified_intent: Mapped[str | None] = mapped_column(String(60), nullable=True)
    classified_domain: Mapped[str | None] = mapped_column(String(40), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    fallback_reason: Mapped[str] = mapped_column(String(30), nullable=False)
    ui_context: Mapped[dict[str, Any] | None] = mapped_column(PGJSONB, nullable=True)
    reviewed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    # created_at/updated_at vienen de TimestampMixin.

    __table_args__ = (
        CheckConstraint(
            "fallback_reason IN ("
            "'out_of_scope','intent_desconocido','baja_confianza',"
            "'sin_datos','ui_context_missing','advice_blocked')",
            name="ck_chat_coverage_gaps_reason",
        ),
        # Backlog de producto: se consulta por tenant + pendientes de revisar.
        Index("ix_chat_coverage_gaps_tenant_reviewed", "tenant_id", "reviewed"),
    )

    def __repr__(self) -> str:
        return (
            f"<ChatCoverageGap tenant={self.tenant_id} reason={self.fallback_reason!r}>"
        )
