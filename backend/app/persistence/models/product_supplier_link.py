"""ORM model: product_supplier_links (Bloque 2 — Tienda → proveedor).

Muchos-a-muchos entre `Product` y `Supplier`: un producto real de Asteria
("ganchos para cortina de baño") se repuso desde DOS tiendas distintas, así
que una FK simple `Product.supplier_id` perdería esa segunda fuente. La
unicidad es por par — un mismo (producto, proveedor) no se declara dos veces,
pero el mismo producto puede tener varias filas con proveedores distintos.
"""

import uuid
from datetime import datetime
from typing import Literal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

#: `catalog_declared` — el usuario mapeó la columna (p. ej. "Tienda") a
#: `supplier:name` en un catálogo, sin evidencia transaccional de compra.
#: `purchase_evidence` — respaldado por al menos un movimiento de compra o
#: gasto real. Una vez en `purchase_evidence` nunca se degrada de vuelta: una
#: relectura que deja de declarar el vínculo por catálogo NO lo borra si ya
#: tiene evidencia real (ver `product_supplier_link_service.py`).
ProductSupplierLinkSource = Literal["catalog_declared", "purchase_evidence"]


class ProductSupplierLink(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "product_supplier_links"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False,
    )
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("suppliers.id", ondelete="CASCADE"),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    # Procedencia suficiente para revertir en una relectura — igual criterio que
    # `sales_entries.source_upload_id`/`source_row_ref`: sin esto, una relectura
    # no puede distinguir "este vínculo lo trajo ESTE archivo" de "lo cargó otra
    # cosa", y terminaría revirtiendo de más o de menos.
    source_upload_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("uploaded_files.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_context_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Soft-delete: NULL = vínculo activo. Una relectura que deja de declarar el
    # vínculo lo anula (no lo borra) para poder auditar/revertir, mismo patrón
    # que `sales_entries.voided_at`.
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "product_id",
            "supplier_id",
            name="uq_product_supplier_links_tenant_product_supplier",
        ),
        CheckConstraint(
            "source IN ('catalog_declared', 'purchase_evidence')",
            name="ck_product_supplier_links_source",
        ),
        Index("ix_product_supplier_links_tenant_id", "tenant_id"),
        Index("ix_product_supplier_links_product_id", "product_id"),
        Index(
            "ix_product_supplier_links_source_upload_id",
            "source_upload_id",
        ),
    )
