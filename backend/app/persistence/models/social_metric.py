"""ORM model: social_metrics — métricas de redes sociales cargadas manualmente."""

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import (
    PGJSONB,
    Base,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class SocialMetric(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "social_metrics"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    # "instagram" | "facebook" | "tiktok" | "other"
    platform: Mapped[str] = mapped_column(String(20), nullable=False)
    metric_date: Mapped[date] = mapped_column(Date, nullable=False)
    followers: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reach: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    engagement: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ads_spend_ars: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, default=Decimal("0")
    )
    # Columnas propias del tenant (entity_type="marketing"), definidas en
    # tenant_custom_field_definitions y editables inline en la tabla.
    custom_fields: Mapped[dict[str, Any]] = mapped_column(
        PGJSONB, nullable=False, server_default="'{}'::jsonb", default=dict
    )

    __table_args__ = (Index("ix_social_metrics_tenant", "tenant_id"),)

    def __repr__(self) -> str:
        return (
            f"<SocialMetric tenant={self.tenant_id} platform={self.platform!r} "
            f"date={self.metric_date}>"
        )
