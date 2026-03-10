"""ORM model: users."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.persistence.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.persistence.models.tenant import Tenant


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(30), nullable=False, default="viewer")
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="pending_verification"
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    tenant: Mapped["Tenant"] = relationship(back_populates="users")

    __table_args__ = (
        # email unique per tenant (not globally)
        {"schema": None},
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email!r} tenant={self.tenant_id}>"
