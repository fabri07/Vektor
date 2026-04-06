"""ORM model: users.

Column names match the migration schema exactly.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.persistence.db.base import PGTEXTARRAY, Base, TimestampMixin
from app.persistence.models.tenant import Tenant


class User(TimestampMixin, Base):
    __tablename__ = "users"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    email: Mapped[str] = mapped_column(Text, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    full_name: Mapped[str] = mapped_column(Text, nullable=False)
    role_code: Mapped[str] = mapped_column(Text, nullable=False, default="OWNER")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── DEPRECATED: Google OAuth tokens ──────────────────────────────────────
    # Estas columnas existían antes de Sprint 1 y quedan en la DB como nullable.
    # Ya NO se usan: los tokens de Workspace migran a user_google_workspace_connections
    # y la identidad de login social vive en user_auth_identities.
    # Se mantienen para no romper datos existentes; no leer ni escribir en código nuevo.
    google_access_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    google_refresh_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    google_scopes: Mapped[list[str] | None] = mapped_column(PGTEXTARRAY, nullable=True)
    google_connected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="users")

    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
    )

    def __repr__(self) -> str:
        return f"<User id={self.user_id} email={self.email!r} tenant={self.tenant_id}>"
