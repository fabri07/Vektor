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

#: Contrato de escritura vigente. Se bumpea cuando cambia el SIGNIFICADO de un
#: campo ya existente — no cuando se agrega uno nuevo.
#:
#: v1 → v2: ``margin_ratio`` y ``cash_ratio`` pasaron a ser ``NULL`` cuando no se
#: pueden calcular. Antes se escribía ``0.0``, y una vez en la tabla ese cero
#: fabricado es indistinguible de un margen genuinamente nulo: entraba a los
#: percentiles del rubro como si fuera una observación real.
#:
#: Es el reemplazo de un corte por fecha, que dependía de cuándo ocurriera el
#: deploy. Esto no depende de nada: lo escribe el mismo commit que cambia la
#: semántica. Los lectores filtran por versión mínima (ver AnalyticsRepository).
EVENT_SCHEMA_VERSION = 2


class AnalyticsEvent(Base):
    __tablename__ = "analytics_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    vertical_code: Mapped[str] = mapped_column(String(50), nullable=False, index=True)

    #: Qué contrato escribió ESTA fila. Los dos defaults son distintos a
    #: propósito: el de Python marca lo que escribe el código de hoy, y el de la
    #: base (``server_default="1"``) marca como viejo a todo lo que llegue sin
    #: pronunciarse — filas preexistentes y las que inserte la versión anterior
    #: de la app en la ventana entre el preDeploy y su reemplazo.
    schema_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=EVENT_SCHEMA_VERSION,
        server_default="1",
    )

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
