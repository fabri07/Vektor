"""ORM model: google_mcp_connections.

Estado de la conexión OAuth de un usuario con el MCP server de Google.
Scopo: un registro por (tenant_id, user_id).

Estados:
  DISCONNECTED       — nunca conectado o desconectado manualmente
  CONNECTING         — OAuth iniciado, esperando callback del MCP server
  CONNECTED          — OAuth completado, scopes concedidos
  INSUFFICIENT_SCOPE — conectado pero faltan scopes para la operación pedida
  RECONNECT_REQUIRED — el token expiró o fue revocado externamente
  ERROR              — fallo inesperado en el MCP server
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import PGJSONB, Base, TimestampMixin, UUIDPrimaryKeyMixin

VALID_STATUSES = frozenset(
    {
        "DISCONNECTED",
        "CONNECTING",
        "CONNECTED",
        "INSUFFICIENT_SCOPE",
        "RECONNECT_REQUIRED",
        "ERROR",
    }
)


class GoogleMcpConnection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "google_mcp_connections"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", name="uq_google_mcp_connection_tenant_user"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="DISCONNECTED",
    )
    scopes_granted: Mapped[list[Any]] = mapped_column(
        PGJSONB,
        nullable=False,
        default=list,
    )
    # Token CSRF para verificar que el callback es del flujo que iniciamos
    state_token: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )
    connected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # Último failure_code de McpToolAuthError (not_connected | insufficient_scope | refresh_failed)
    last_error_code: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
    )
