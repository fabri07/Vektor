"""ORM model: user_auth_identities.

Almacena la identidad OIDC de un usuario por proveedor social (Google, Facebook…).
Un usuario puede tener múltiples identidades (una por proveedor).

Este modelo es SOLO para autenticación (login/registro social).
Las credenciales API de Google Workspace viven en UserGoogleWorkspaceConnection.

RLS: la tabla tiene tenant_id para mantener el aislamiento DB-level del resto del proyecto.
Aunque conceptualmente la identidad es "por usuario", users pertenecen a un tenant
y la política RLS protege contra lecturas cross-tenant si app.current_tenant_id no está seteado.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import Base


class UserAuthIdentity(Base):
    __tablename__ = "user_auth_identities"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    # tenant_id requerido para RLS (mismo patrón que el resto del proyecto)
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
    # "google" | "facebook" — texto libre para no migrar al agregar proveedor
    provider: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    # Sub claim de Google OIDC (identificador único del proveedor)
    provider_subject: Mapped[str] = mapped_column(Text, nullable=False)
    # Email reportado por el proveedor (puede diferir del users.email)
    provider_email: Mapped[str] = mapped_column(Text, nullable=False)
    linked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_subject",
            name="uq_auth_identity_provider_subject",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<UserAuthIdentity user={self.user_id}"
            f" provider={self.provider!r} subject={self.provider_subject!r}>"
        )
