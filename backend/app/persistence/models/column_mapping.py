"""ORM model: tenant_column_mappings (mapeo aprendido de columnas por tenant)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import Base


class TenantColumnMapping(Base):
    __tablename__ = "tenant_column_mappings"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "entity_type",
            "source_column",
            name="uq_tcm_tenant_entity_col",
        ),
        CheckConstraint(
            "entity_type IN ('sale', 'expense', 'product', 'inventory')",
            name="ck_tcm_entity_type",
        ),
        Index("ix_tcm_tenant_entity", "tenant_id", "entity_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default="gen_random_uuid()",
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    entity_type: Mapped[str] = mapped_column(String(30), nullable=False)
    source_column: Mapped[str] = mapped_column(String(200), nullable=False)
    target_field: Mapped[str] = mapped_column(String(80), nullable=False)
    confirmed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
