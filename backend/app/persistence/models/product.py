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
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, Mapper, mapped_column

from app.domain.internal_sku import generate_internal_sku
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
    # Código propio de Véktor, generado al crear y NUNCA regenerado. Distinto de
    # `sku`, que es el que aporta el archivo o el proveedor: conviven. Distinto
    # de `id`, que es la identidad técnica y no se muestra. Nullable porque la
    # migración `20260903_0001` no backfillea — las filas anteriores lo tienen
    # en NULL hasta que se corra el backfill.
    internal_sku: Mapped[str | None] = mapped_column(String(20), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Fase 2 (F2-T1) — identidad de producto: campo raw + columnas normalizadas
    # (fuente única de cálculo: el listener before_insert/before_update de más
    # abajo). Claves normalizadas INDEPENDIENTES por campo (no una jerárquica
    # excluyente) para que T2 pueda matchear por barcode, sku, nombre o marca.
    barcode: Mapped[str | None] = mapped_column(String(64), nullable=True)
    barcode_normalized: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sku_normalized: Mapped[str | None] = mapped_column(String(120), nullable=True)
    name_normalized: Mapped[str] = mapped_column(String(400), nullable=False)
    brand_normalized: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Vencimiento informativo (se USA recién en F6; acá solo la columna).
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Los tres precios de un producto son conceptos DISTINTOS y coexisten:
    # `sale_price_ars` = precio de venta vigente que configuró el negocio (el único
    # que entra al margen); `list_price_ars` = sugerido por proveedor/lista
    # (informativo); `unit_cost_ars` = costo unitario vigente o de referencia.
    # El precio realmente vendido NO vive acá — va en `SaleEntry.unit_price`,
    # porque cambia por descuento, fecha, canal o cliente.
    sale_price_ars: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    list_price_ars: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
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
            "'USER_CANCELLED','DUPLICATE','MANUAL_ADMIN_VOID','REREAD_UNDO')",
            name="ck_products_deactivation_reason",
        ),
        Index("ix_products_tenant_provenance", "tenant_id", "provenance"),
        # Índices de BÚSQUEDA (NO únicos — la unicidad la imponen los uq_* de abajo).
        Index("ix_products_tenant_barcode_norm", "tenant_id", "barcode_normalized"),
        Index("ix_products_tenant_sku_norm", "tenant_id", "sku_normalized"),
        Index("ix_products_tenant_name_norm", "tenant_id", "name_normalized"),
        # F5-B — UNICIDAD de identidad fuerte. Parciales: solo entre ACTIVOS y solo
        # con la clave presente. Un tenant puede tener N inactivos con el mismo SKU
        # (historial de bajas) y N activos sin SKU.
        #
        # ``sqlite_where`` ADEMÁS de ``postgresql_where`` no es cosmético: sin él
        # SQLite ignora el predicado y crea un único TOTAL, que prohibiría los
        # duplicados entre inactivos —legales— y haría fallar tests con un modo de
        # falla que en Postgres no existe. El espejo de estos dos índices es la
        # migración 20260802_0001; si cambia el predicado, cambian los dos.
        Index(
            "uq_products_tenant_barcode_norm",
            "tenant_id",
            "barcode_normalized",
            unique=True,
            postgresql_where=text(
                "is_active AND barcode_normalized IS NOT NULL AND barcode_normalized <> ''"
            ),
            sqlite_where=text(
                "is_active AND barcode_normalized IS NOT NULL AND barcode_normalized <> ''"
            ),
        ),
        # El código propio: único por tenant entre los que lo tienen. A
        # diferencia de los dos de arriba NO filtra por `is_active`: el código
        # de un producto dado de baja no se recicla, porque puede estar escrito
        # en una etiqueta o en un remito viejo.
        Index(
            "uq_products_tenant_internal_sku",
            "tenant_id",
            "internal_sku",
            unique=True,
            postgresql_where=text("internal_sku IS NOT NULL"),
            sqlite_where=text("internal_sku IS NOT NULL"),
        ),
        Index(
            "uq_products_tenant_sku_norm",
            "tenant_id",
            "sku_normalized",
            unique=True,
            postgresql_where=text(
                "is_active AND sku_normalized IS NOT NULL AND sku_normalized <> ''"
            ),
            sqlite_where=text(
                "is_active AND sku_normalized IS NOT NULL AND sku_normalized <> ''"
            ),
        ),
    )

    def __repr__(self) -> str:
        return f"<Product tenant={self.tenant_id} name={self.name!r}>"


def _ensure_internal_sku(target: Product) -> None:
    """Asigna el código propio si el producto todavía no tiene.

    Acá y no en los constructores: son cinco rutas de alta (import de catálogo,
    import de compra, chat, remito, POST manual) y la que se olvidara dejaría
    productos sin código sin que nada fallara.

    ``is None`` y no falsy: un código ya asignado NUNCA se regenera, ni en una
    relectura ni en un update. Es la propiedad que lo vuelve utilizable — un
    código que cambia no sirve para etiquetar nada.

    **El ``id`` se materializa acá si hace falta.** El ``default=uuid.uuid4`` de
    la columna se evalúa cuando SQLAlchemy arma el INSERT, o sea DESPUÉS de este
    listener: en un ``Product(...)`` que no pasa ``id`` explícito —el chat, el
    remito, el POST manual— acá todavía vale ``None``. Adelantarlo es exactamente
    lo que el default iba a hacer, y deja cierto el invariante que le da valor al
    formato: el código SIEMPRE se puede recomputar desde el id
    (``generate_internal_sku(p.id)``), sin leer la fila ni coordinar nada. De eso
    depende que un backfill sea idempotente y verificable.
    """
    if target.id is None:
        target.id = uuid.uuid4()
    if target.internal_sku is None:
        target.internal_sku = generate_internal_sku(target.id)


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
    _ensure_internal_sku(target)
    _sync_product_identity_columns(target)


@event.listens_for(Product, "before_update")
def _product_before_update(
    mapper: Mapper[Product], connection: Connection, target: Product
) -> None:
    _sync_product_identity_columns(target)
