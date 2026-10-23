"""Historial materializado del costo promedio ponderado (M·P2).

El promedio es un DATO: el costo principal de cada venta sigue siendo el de
reposición (``costo_de_referencia``). Recorrer la historia de un producto en cada
ticket del POS no escala, así que se guarda un registro por cada compra,
apertura o ancla con el estado que deja (``domain/costo_promedio.py``). El costo
a una fecha es el último registro de ese día o anterior.

``product_cost_history`` es derivada: se puede borrar y reconstruir desde el
ledger en cualquier momento (``costo_promedio_service.reconstruir``).
``product_cost_state`` guarda, por producto, un contador de cambios
(``revision``: lo suben las marcas y las publicaciones) y el día mínimo pendiente
de recalcular (``pendiente_desde``; ``date.min`` = todo el historial).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import Base, UUIDPrimaryKeyMixin

ESTADOS = ("medido", "incompleto", "sin_medicion")
CLASES = ("apertura", "compra", "ancla", "entrada", "salida", "complejo")


class ProductCostHistory(UUIDPrimaryKeyMixin, Base):
    """El estado del promedio después de una compra, apertura o ancla, o de
    cualquier evento que cambie lo que se lee (``domain.costo_promedio.registros``)."""

    __tablename__ = "product_cost_history"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    # La clave de orden del evento (``Evento.clave``): día → clase → hora →
    # carga → id. Se guarda entera para reconstruir desde un checkpoint.
    dia: Mapped[date] = mapped_column(Date, nullable=False)
    clase_orden: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    fecha: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    creado: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    evento_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    clase: Mapped[str] = mapped_column(String(10), nullable=False)

    u_conocidas: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    u_sin_costo: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    costo_promedio: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    estado: Mapped[str] = mapped_column(String(15), nullable=False)
    stock_conocido: Mapped[bool] = mapped_column(Boolean, nullable=False)
    aproximado: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    motivo: Mapped[str | None] = mapped_column(String(200), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "estado IN ('medido','incompleto','sin_medicion')",
            name="ck_product_cost_history_estado",
        ),
        CheckConstraint(
            "clase IN ('apertura','compra','ancla','entrada','salida','complejo')",
            name="ck_product_cost_history_clase",
        ),
        Index("ix_product_cost_history_lookup", "tenant_id", "product_id", "dia"),
    )


class ProductCostState(UUIDPrimaryKeyMixin, Base):
    """Una fila por producto: qué revisión está publicada y qué falta recalcular."""

    __tablename__ = "product_cost_state"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="CASCADE"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Día mínimo sucio; NULL = el historial está al día.
    pendiente_desde: Mapped[date | None] = mapped_column(Date, nullable=True)
    stock_fuera_del_ledger: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    aproximado: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reconstruido_en: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Cuándo se marcó el pendiente (M·P2b): el drenaje levanta los viejos.
    marcado_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    intentos: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    ultimo_error: Mapped[str | None] = mapped_column(String(300), nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "product_id", name="uq_product_cost_state_product"),
        Index("ix_product_cost_state_pendientes", "tenant_id", "marcado_en"),
    )
