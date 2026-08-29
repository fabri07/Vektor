from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.text("CURRENT_TIMESTAMP"),
        nullable=False,
    )


class GoogleOAuthToken(TimestampMixin, Base):
    __tablename__ = "google_oauth_tokens"
    __table_args__ = (
        sa.UniqueConstraint(
            "tenant_id",
            "user_id",
            "provider",
            name="uq_google_oauth_tokens_tenant_user_provider",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), nullable=False, index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(as_uuid=True), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(sa.String(32), nullable=False, default="google")
    email: Mapped[str | None] = mapped_column(sa.String(320), nullable=True)
    state_token: Mapped[str | None] = mapped_column(sa.Text(), nullable=True, index=True)
    state_expires_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    scopes_requested: Mapped[list[str]] = mapped_column(sa.JSON(), nullable=False, default=list)
    scopes_granted: Mapped[list[str]] = mapped_column(sa.JSON(), nullable=False, default=list)
    access_token_encrypted: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    refresh_token_encrypted: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    token_type: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
