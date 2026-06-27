"""ORM model: customers."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import PGJSONB, Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.persistence.models._sentinel import SENTINEL_FLAG_KEY, is_sentinel_value


class Customer(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "customers"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    # ``name`` = nombre (persona) o razón social (empresa), obligatorio. Para personas
    # el apellido va en ``last_name``; para empresas queda NULL.
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    # "person" | "company" (NULL = legacy/sin definir). Orienta validación y UI.
    customer_type: Mapped[str | None] = mapped_column(String(10), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Documento fiscal: ``doc_type`` distingue DNI de CUIT; cada uno en su columna.
    doc_type: Mapped[str | None] = mapped_column(String(10), nullable=True)
    dni: Mapped[str | None] = mapped_column(String(15), nullable=True)
    cuit: Mapped[str | None] = mapped_column(String(13), nullable=True)
    # consumidor_final | monotributo | responsable_inscripto | exento (NULL = no seteado).
    iva_condition: Mapped[str | None] = mapped_column(String(25), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    telegram_username: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Domicilio de entrega (logística argentina).
    address: Mapped[str | None] = mapped_column(Text, nullable=True)
    locality: Mapped[str | None] = mapped_column(String(120), nullable=True)
    province: Mapped[str | None] = mapped_column(String(120), nullable=True)
    postal_code: Mapped[str | None] = mapped_column(String(12), nullable=True)
    # Cumpleaños — solo se guarda (sin automatización de marketing por ahora).
    birthday: Mapped[date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    custom_fields: Mapped[dict[str, Any]] = mapped_column(
        PGJSONB, nullable=False, server_default="'{}'::jsonb", default=dict
    )
    # Límite de crédito (Fase 2 — cobro→cliente). NULL = sin límite configurado.
    credit_limit: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    # Soft-delete: NULL = activo; timestamp = desactivado.
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_customers_tenant_id", "tenant_id"),)

    @property
    def is_sentinel(self) -> bool:
        """¿Es el cliente sentinela 'Local' (agrupa ventas sin cliente registrado)?"""
        return is_sentinel_value((self.custom_fields or {}).get(SENTINEL_FLAG_KEY))

    def __repr__(self) -> str:
        return f"<Customer tenant={self.tenant_id} name={self.name!r}>"
