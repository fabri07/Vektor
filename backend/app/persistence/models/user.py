"""ORM model: users.

Column names match the migration schema exactly.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.persistence.db.base import Base, TimestampMixin
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
    # Teléfono/WhatsApp de contacto del usuario (opcional; se pide en el registro
    # y se edita en /settings). Informativo — los links wa.me no lo necesitan.
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Step-up auth — PIN de 4 dígitos (bcrypt). NULL = todavía no configurado.
    pin_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    pin_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Permiso de sub-cuenta para modificar datos sensibles. El OWNER está
    # habilitado implícitamente (no depende de esta columna).
    can_modify_sensitive: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="users")

    __table_args__ = (
        UniqueConstraint("user_id", "tenant_id", name="uq_users_user_tenant"),
        UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
    )

    def __repr__(self) -> str:
        return f"<User id={self.user_id} email={self.email!r} tenant={self.tenant_id}>"
