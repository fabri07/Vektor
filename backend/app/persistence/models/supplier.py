"""ORM model: suppliers."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import PGJSONB, Base, TimestampMixin, UUIDPrimaryKeyMixin

# Helper del flag de sentinela ahora compartido con clientes (ver models/_sentinel).
# Se re-exporta acá por compatibilidad con los imports existentes
# (``from app.persistence.models.supplier import SENTINEL_FLAG_KEY, is_sentinel_value``).
from app.persistence.models._sentinel import (
    SENTINEL_FLAG_KEY,
    is_flag_true,
    is_sentinel_value,
)

# Flag de proveedor provisional derivado de una marca: lo escribe
# ``scripts/revert_brand_supplier_collapse.py`` cuando reconstruye un proveedor a
# partir de la marca de un producto (reasignable a un proveedor real más tarde).
PROVISIONAL_FLAG_KEY = "_provisional_from_brand"

# Flag de marca colapsada por error de clasificación: lo escribe
# ``scripts/deactivate_brand_suppliers.py`` (y su backfill) al dar de baja un
# "proveedor" que en realidad era una marca. Estas filas NO se listan ni se
# reactivan desde la UI — la vía sancionada de restauración es el script de revert.
BRAND_COLLAPSED_FLAG_KEY = "_brand_collapsed"

__all__ = [
    "BRAND_COLLAPSED_FLAG_KEY",
    "PROVISIONAL_FLAG_KEY",
    "SENTINEL_FLAG_KEY",
    "Supplier",
    "is_flag_true",
    "is_sentinel_value",
]


class Supplier(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "suppliers"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    # ``name`` = nombre o razón social (obligatorio). Para personas, el apellido
    # va en ``last_name``; para empresas queda NULL (la razón social va en name).
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    last_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Los DOS conviven a propósito (mig `20260813_0001`): un proveedor persona
    # física monotributista tiene CUIL; una empresa —que es la mayoría de los
    # proveedores de una PYME— tiene CUIT. Hasta acá sólo existía `cuil`, así que
    # el dato fiscal del proveedor típico no se podía guardar.
    cuil: Mapped[str | None] = mapped_column(String(13), nullable=True)
    cuit: Mapped[str | None] = mapped_column(String(13), nullable=True)
    iva_condition: Mapped[str | None] = mapped_column(String(25), nullable=True)
    payment_method: Mapped[str | None] = mapped_column(String(30), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    custom_fields: Mapped[dict[str, Any]] = mapped_column(
        PGJSONB, nullable=False, server_default="'{}'::jsonb", default=dict
    )
    # URL del catálogo web o API de precios del proveedor (informativo, nullable).
    catalog_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    api_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Soft-delete: NULL = activo; timestamp = desactivado.
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_suppliers_tenant_id", "tenant_id"),)

    @property
    def is_sentinel(self) -> bool:
        """¿Es el proveedor sentinela 'No identificado' (agrupa compras sin proveedor)?"""
        return is_sentinel_value((self.custom_fields or {}).get(SENTINEL_FLAG_KEY))

    @property
    def is_provisional(self) -> bool:
        """¿Es un proveedor provisional derivado de una marca (reasignable)?"""
        return is_flag_true((self.custom_fields or {}).get(PROVISIONAL_FLAG_KEY))

    @property
    def is_brand_collapsed(self) -> bool:
        """¿Es una marca que fue confundida con proveedor y colapsada (baja por error)?"""
        return is_flag_true((self.custom_fields or {}).get(BRAND_COLLAPSED_FLAG_KEY))

    def __repr__(self) -> str:
        return f"<Supplier tenant={self.tenant_id} name={self.name!r}>"
