"""AnalyticsEvent — log insert-only de métricas anonimizadas cross-tenant.

No contiene tenant_id ni PII: solo el código de vertical y ratios numéricos.
Es la base del data moat: acumula señal real para computar benchmarks estadísticos.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import Base


class AnalyticsEvent(Base):
    __tablename__ = "analytics_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    vertical_code: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    # Dimensiones del health score
    score_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score_cash: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score_margin: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score_stock: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score_supplier: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Ratios del negocio (anonimizados — nunca valores ARS absolutos)
    margin_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    cash_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    supplier_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    product_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    low_stock_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    data_completeness: Mapped[float | None] = mapped_column(Float, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
