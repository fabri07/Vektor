"""ORM model: products."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import PGJSONB, Base, TimestampMixin, UUIDPrimaryKeyMixin


class Product(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "products"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    sku: Mapped[str | None] = mapped_column(String(100), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    sale_price_ars: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    unit_cost_ars: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    stock_units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    low_stock_threshold_units: Mapped[int | None] = mapped_column(
        Integer, nullable=True, default=None
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # FASE 3 (B2): producto auto-creado por un import al que le faltan datos clave
    # (precio o costo). El usuario debe completarlo. False = producto completo.
    requires_completion: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    # Fecha de alta / adquisición editable (NULL = no informada; el frontend cae a created_at).
    acquired_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    provenance: Mapped[str] = mapped_column(String(10), nullable=False, default="REAL")
    custom_fields: Mapped[dict[str, Any]] = mapped_column(
        PGJSONB, nullable=False, server_default="'{}'::jsonb", default=dict
    )
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deactivation_reason: Mapped[str | None] = mapped_column(String(30), nullable=True)

    __table_args__ = (
        CheckConstraint("provenance IN ('REAL', 'DEMO')", name="ck_products_provenance"),
        CheckConstraint(
            "deactivation_reason IS NULL OR deactivation_reason IN ("
            "'USER_CANCELLED','DUPLICATE','MANUAL_ADMIN_VOID')",
            name="ck_products_deactivation_reason",
        ),
        Index("ix_products_tenant_provenance", "tenant_id", "provenance"),
    )

    def __repr__(self) -> str:
        return f"<Product tenant={self.tenant_id} name={self.name!r}>"
