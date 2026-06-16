"""ORM model: external_operation_logs — log unificado de llamadas a proveedores externos.

Una fila por intento de operación externa (email/Resend, WhatsApp, Meta/TikTok sync,
OAuth, Gmail/MCP, …). Se correlaciona con DecisionAuditLog y communication_log por
`trace_id`. Los payloads se guardan redactados (sin secretos).
"""

import uuid

from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.observability.trace import get_trace_id
from app.persistence.db.base import PGJSONB, Base, TimestampMixin, UUIDPrimaryKeyMixin


class ExternalOperationLog(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "external_operation_logs"

    # Correlación transversal (autocapturada del request vigente).
    trace_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, default=lambda: get_trace_id()
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
    )
    # p. ej. "email_send", "whatsapp_send", "marketing_sync", "oauth"
    operation_type: Mapped[str] = mapped_column(String(40), nullable=False)
    # p. ej. "resend", "whatsapp", "meta", "tiktok", "google"
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_account_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # "success" | "failed"
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    request_payload_redacted: Mapped[dict | None] = mapped_column(PGJSONB, nullable=True)
    response_payload_redacted: Mapped[dict | None] = mapped_column(PGJSONB, nullable=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __table_args__ = (
        Index("ix_external_operation_logs_tenant", "tenant_id"),
        Index("ix_external_operation_logs_trace", "trace_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<ExternalOperationLog provider={self.provider!r} "
            f"op={self.operation_type!r} status={self.status!r}>"
        )
