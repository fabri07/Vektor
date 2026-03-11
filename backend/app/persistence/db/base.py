"""
SQLAlchemy declarative base and shared mixins.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB as _JSONB
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# JSONB on PostgreSQL, JSON on other backends (SQLite in tests)
PGJSONB = JSON().with_variant(_JSONB(), "postgresql")


class Base(DeclarativeBase):
    """Declarative base — all ORM models inherit from this."""

    type_annotation_map: dict[Any, Any] = {}


class TimestampMixin:
    """
    Adds created_at / updated_at to any model.
    Python-side defaults ensure timestamps are set in SQLite test environments
    where server_default (func.now()) may not trigger.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        default=lambda: datetime.now(UTC),
        onupdate=func.now(),
        nullable=False,
    )


class UUIDPrimaryKeyMixin:
    """UUID primary key with server-side default."""

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
