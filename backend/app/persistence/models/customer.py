"""ORM model: customers."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
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
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, Mapper, mapped_column

from app.domain.external_code import clave_de_codigo_externo
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
    # E6a — ¿alguien editó esta ficha A MANO? Lo marca el PATCH del endpoint, y
    # lo lee el import: una vez que el usuario tocó la ficha, una carga posterior
    # deja de pisar lo que él escribió y pasa a COMPLETAR sólo lo que está vacío.
    #
    # Es a nivel entidad y no por campo, igual que `sales_entries.has_user_edits`.
    # La granularidad gruesa se banca porque la política es aditiva y no un
    # bloqueo: el import sigue pudiendo llenar los campos que el usuario nunca
    # cargó, sólo pierde el derecho a sobrescribir los que sí.
    has_user_edits: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
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
        # El predicado excluye las bajas por una razón operativa, no estética: el
        # índice tiene que ver lo MISMO que la búsqueda. Los índices de identidad
        # se consultan sobre entidades vivas (`list_for_dedup` / `is_active`), así
        # que un índice total dejaría que una entidad dada de baja —invisible para
        # la búsqueda— hiciera fallar el insert con un IntegrityError que nadie
        # puede explicar mirando los datos activos. Mismo predicado que
        # `uq_products_tenant_barcode_norm`, y misma consecuencia conocida: el
        # código de una baja se puede reciclar.
        Index(
            "uq_customers_tenant_external_code",
            "tenant_id",
            "external_code_key",
            unique=True,
            postgresql_where=text("deactivated_at IS NULL AND external_code_key IS NOT NULL"),
            sqlite_where=text("deactivated_at IS NULL AND external_code_key IS NOT NULL"),
        ),
    )

    @property
    def is_sentinel(self) -> bool:
        """¿Es el cliente sentinela 'Local' (agrupa ventas sin cliente registrado)?"""
        return is_sentinel_value((self.custom_fields or {}).get(SENTINEL_FLAG_KEY))

    def __repr__(self) -> str:
        return f"<Customer tenant={self.tenant_id} name={self.name!r}>"


# ── E6a: la clave del código externo se recomputa sola ───────────────────────
# Mismo mecanismo que `_sync_product_identity_columns`: fuente ÚNICA de cálculo
# en `before_insert`/`before_update`, no un `@validates` ni una llamada del
# caller. Si dependiera de que cada call site se acuerde de llenarla, el índice
# único —que es el candado de la identidad— quedaría fuera de sincronía con el
# dato justo en el camino que se olvidó, y eso no falla: deja pasar duplicados.
def _sync_external_code_key(target: Customer) -> None:
    target.external_code_key = clave_de_codigo_externo(
        target.external_code, target.external_source
    )


@event.listens_for(Customer, "before_insert")
def _customer_external_code_before_insert(
    mapper: Mapper[Customer], connection: Connection, target: Customer
) -> None:
    _sync_external_code_key(target)


@event.listens_for(Customer, "before_update")
def _customer_external_code_before_update(
    mapper: Mapper[Customer], connection: Connection, target: Customer
) -> None:
    _sync_external_code_key(target)
