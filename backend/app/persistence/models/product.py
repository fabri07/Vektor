"""ORM model: products."""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Connection,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    event,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, Mapper, mapped_column

from app.domain.text_norm import (
    normalize_barcode,
    normalize_brand,
    normalize_product_name,
    normalize_sku,
)
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
    # Fase 2 (F2-T1) — identidad de producto: campo raw + columnas normalizadas
    # (fuente única de cálculo: el listener before_insert/before_update de más
    # abajo). Claves normalizadas INDEPENDIENTES por campo (no una jerárquica
    # excluyente) para que T2 pueda matchear por barcode, sku, nombre o marca.
    barcode: Mapped[str | None] = mapped_column(String(64), nullable=True)
    barcode_normalized: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sku_normalized: Mapped[str | None] = mapped_column(String(120), nullable=True)
    name_normalized: Mapped[str | None] = mapped_column(String(400), nullable=True)
    brand_normalized: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Vencimiento informativo (se USA recién en F6; acá solo la columna).
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
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
    # Relectura de archivos: marca si este producto importado fue editado a mano
    # (la re-importación no debe pisarlo). `source_row_ref` ata el registro a su
    # fila de origen en el archivo para reconciliar al re-importar.
    has_user_edits: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    source_row_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
        # Índices de BÚSQUEDA (NO únicos — la unicidad es Fase 5).
        Index("ix_products_tenant_barcode_norm", "tenant_id", "barcode_normalized"),
        Index("ix_products_tenant_sku_norm", "tenant_id", "sku_normalized"),
        Index("ix_products_tenant_name_norm", "tenant_id", "name_normalized"),
    )

    def __repr__(self) -> str:
        return f"<Product tenant={self.tenant_id} name={self.name!r}>"


def _sync_product_identity_columns(target: Product) -> None:
    """Recomputa las 4 columnas ``*_normalized`` desde los campos raw.

    Fuente ÚNICA de cálculo (no duplicar con ``@validates``): cubre todos los
    ``session.add(Product(...))``/updates del código sin depender de timing de
    flush por parte del caller, porque ``before_insert``/``before_update``
    disparan en flush.
    """
    target.name_normalized = normalize_product_name(target.name)
    target.sku_normalized = normalize_sku(target.sku)
    target.barcode_normalized = normalize_barcode(target.barcode)
    marca = target.custom_fields.get("marca") if isinstance(target.custom_fields, dict) else None
    target.brand_normalized = normalize_brand(marca)


@event.listens_for(Product, "before_insert")
def _product_before_insert(
    mapper: Mapper[Product], connection: Connection, target: Product
) -> None:
    _sync_product_identity_columns(target)


@event.listens_for(Product, "before_update")
def _product_before_update(
    mapper: Mapper[Product], connection: Connection, target: Product
) -> None:
    _sync_product_identity_columns(target)
