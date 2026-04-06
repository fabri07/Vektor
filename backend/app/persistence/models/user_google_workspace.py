"""ORM model: user_google_workspace_connections.

Credenciales de Google Workspace API por usuario.
Máximo una fila por user_id (unicidad enforced por UNIQUE constraint).

Separado de user_auth_identities: este modelo almacena tokens de API
de larga vida, no identidad de login.

RLS: la tabla tiene tenant_id para mantener el aislamiento DB-level del resto del proyecto.
Aunque la conexión Workspace es por usuario (no por tenant), incluir tenant_id permite
aplicar la misma política RLS que el resto y evita exposición cross-tenant.

Los tokens (access y refresh) se almacenan cifrados con Fernet
via app.application.security.token_cipher.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKeyConstraint, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import PGTEXTARRAY, Base


class UserGoogleWorkspaceConnection(Base):
    __tablename__ = "user_google_workspace_connections"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    # tenant_id requerido para RLS (mismo patrón que el resto del proyecto)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    # Una sola conexión Workspace por usuario (UNIQUE enforced en DB)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, unique=True)

    # ── Tokens cifrados con Fernet ────────────────────────────────────────────
    access_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)

    # Scopes otorgados en el último consentimiento
    scopes_granted: Mapped[list[str]] = mapped_column(
        PGTEXTARRAY, nullable=False, default=list
    )

    connected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    # Cuándo vence el access token actual (None = desconocido)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Cuándo fue revocada la conexión (None = activa)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Último refresh exitoso
    last_refresh_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Último error de Google (invalid_grant, insufficient_scope, etc.)
    last_error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "user_id"],
            ["users.tenant_id", "users.user_id"],
            ondelete="CASCADE",
            name="fk_workspace_connections_user_tenant",
        ),
        UniqueConstraint("user_id", name="uq_workspace_connection_user"),
    )

    @property
    def is_active(self) -> bool:
        """True si la conexión existe y no fue revocada."""
        return self.revoked_at is None

    def __repr__(self) -> str:
        return (
            f"<UserGoogleWorkspaceConnection user={self.user_id}"
            f" active={self.is_active} scopes={len(self.scopes_granted)}>"
        )
