"""ORM model: communication_log — registro de mensajes salientes a clientes/proveedores."""

import uuid

from sqlalchemy import (
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.observability.trace import get_trace_id
from app.persistence.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class CommunicationLog(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "communication_log"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    # "customer" | "supplier"
    recipient_type: Mapped[str] = mapped_column(String(20), nullable=False)
    recipient_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # "email" | "whatsapp"
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(300), nullable=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # "sent" | "failed"
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # id del mensaje en el proveedor (p. ej. Resend) — para rastrear entrega.
    provider_message_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Correlación transversal: autocapturado del request vigente en cada INSERT.
    trace_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, default=lambda: get_trace_id()
    )

    __table_args__ = (Index("ix_communication_log_tenant", "tenant_id"),)

    def __repr__(self) -> str:
        return (
            f"<CommunicationLog tenant={self.tenant_id} channel={self.channel!r} "
            f"status={self.status!r}>"
        )
