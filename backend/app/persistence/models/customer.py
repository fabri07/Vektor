"""ORM model: customers."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Connection,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    event,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, Mapper, mapped_column

from app.domain.text_norm import normalize_external_code
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
    # F-ID: código Véktor permanente (capa 2 de la identidad transversal), formato
    # "CLI-0001". Denormalizado (barato de mostrar/buscar); la fuente de verdad de
    # PROCEDENCIA y de todo código externo adicional es `entity_identifiers`. Un
    # sentinela NUNCA recibe código — se aplica en `entity_identity_service`, no acá.
    vektor_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    vektor_code_normalized: Mapped[str | None] = mapped_column(String(20), nullable=True)

    __table_args__ = (
        Index("ix_customers_tenant_id", "tenant_id"),
        Index("ix_customers_tenant_vektor_code_norm", "tenant_id", "vektor_code_normalized"),
        # Parcial: solo entre ACTIVOS y solo con el código presente — mismo criterio
        # que `uq_products_tenant_sku_norm`. El no-reciclo del VALOR lo garantiza la
        # secuencia atómica (`entity_code_sequences`), no este índice: éste sólo evita
        # que dos filas ACTIVAS colisionen por un bug de asignación.
        Index(
            "uq_customers_tenant_vektor_code_norm",
            "tenant_id",
            "vektor_code_normalized",
            unique=True,
            postgresql_where=text(
                "deactivated_at IS NULL AND vektor_code_normalized IS NOT NULL "
                "AND vektor_code_normalized <> ''"
            ),
            sqlite_where=text(
                "deactivated_at IS NULL AND vektor_code_normalized IS NOT NULL "
                "AND vektor_code_normalized <> ''"
            ),
        ),
    )

    @property
    def is_sentinel(self) -> bool:
        """¿Es el cliente sentinela 'Local' (agrupa ventas sin cliente registrado)?"""
        return is_sentinel_value((self.custom_fields or {}).get(SENTINEL_FLAG_KEY))

    def __repr__(self) -> str:
        return f"<Customer tenant={self.tenant_id} name={self.name!r}>"


@event.listens_for(Customer, "before_insert")
def _customer_before_insert(
    mapper: Mapper[Customer], connection: Connection, target: Customer
) -> None:
    target.vektor_code_normalized = normalize_external_code(target.vektor_code)


@event.listens_for(Customer, "before_update")
def _customer_before_update(
    mapper: Mapper[Customer], connection: Connection, target: Customer
) -> None:
    target.vektor_code_normalized = normalize_external_code(target.vektor_code)
