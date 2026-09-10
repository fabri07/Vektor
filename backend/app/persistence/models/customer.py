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
    text,
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

    # E6a — CÓDIGO EXTERNO: el identificador que el negocio ya usa en su propio
    # sistema (o el que le asigna su proveedor). Es la única clave fuerte que
    # existe para el maestro de un kiosco, donde no hay CUIT ni código de barras.
    # Ver `domain/external_code.py`.
    external_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    #: De qué sistema salió. NULL = código propio del negocio. Entra en la clave
    #: porque el "1024" de un sistema no es el "1024" de otro.
    external_source: Mapped[str | None] = mapped_column(String(60), nullable=True)
    #: Forma canónica indexada (`clave_de_codigo_externo`). Se persiste aparte del
    #: crudo por la misma razón que `sku_normalized`: el índice tiene que evaluar
    #: exactamente lo mismo que la búsqueda, y una función en el predicado dejaría
    #: las dos definiciones libres de divergir.
    external_code_key: Mapped[str | None] = mapped_column(String(200), nullable=True)

    __table_args__ = (
        Index("ix_customers_tenant_id", "tenant_id"),
        # E6a — unicidad del código externo. PARCIAL sobre los no nulos: las
        # columnas son nuevas y nadie tiene código todavía, así que el único no
        # necesita prevalidación de colisiones — no hay datos que colisionar.
        #
        # SIN filtro por baja, igual que `uq_products_tenant_internal_sku`: el
        # código de una entidad dada de baja NO se recicla, porque sigue escrito
        # en los documentos viejos que la nombran. Un índice que la excluyera
        # además haría fallar la reactivación contra quien le hubiera tomado el
        # código.
        Index(
            "uq_customers_tenant_external_code",
            "tenant_id",
            "external_code_key",
            unique=True,
            postgresql_where=text("external_code_key IS NOT NULL"),
            sqlite_where=text("external_code_key IS NOT NULL"),
        ),
    )

    @property
    def is_sentinel(self) -> bool:
        """¿Es el cliente sentinela 'Local' (agrupa ventas sin cliente registrado)?"""
        return is_sentinel_value((self.custom_fields or {}).get(SENTINEL_FLAG_KEY))

    def __repr__(self) -> str:
        return f"<Customer tenant={self.tenant_id} name={self.name!r}>"
