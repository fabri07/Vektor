"""ORM de los alias de producto (B·8a): product_aliases.

Un alias es otro nombre con el que el negocio llama a un producto de su
catálogo —«Coca 500», «coca cola 1/2»—, aprendido cuando alguien vincula a mano
una venta de «Otros» con el producto. El importador lo consulta después del
código de barras y del SKU; nunca el escaneo del POS ni `/products/lookup`.

Sin FK al archivo ni a la fila de «Otros» a propósito: lo aprendió una persona,
no un archivo, así que borrar el archivo (F11) no lo borra.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import Base, UUIDPrimaryKeyMixin

#: El único origen hoy: lo vinculó una persona desde «Otros».
ALIAS_ORIGIN_MANUAL = "manual"


class ProductAlias(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "product_aliases"

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
    #: Tal como lo escribió el archivo (para mostrarlo).
    alias: Mapped[str] = mapped_column(String(300), nullable=False)
    #: `normalize_product_name(alias)`: la clave por la que se busca.
    alias_normalizado: Mapped[str] = mapped_column(String(300), nullable=False)
    origin: Mapped[str] = mapped_column(String(20), nullable=False, default=ALIAS_ORIGIN_MANUAL)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        # Un alias identifica UN producto del negocio: si fuera de dos, no
        # identificaría nada.
        UniqueConstraint("tenant_id", "alias_normalizado", name="uq_product_aliases_tenant_alias"),
        Index("ix_product_aliases_tenant_product", "tenant_id", "product_id"),
        CheckConstraint("origin IN ('manual')", name="ck_product_aliases_origin"),
    )
