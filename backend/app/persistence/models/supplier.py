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

# El sentinela "No identificado" se marca con ``custom_fields["_sentinel"]``. El
# valor puede llegar como string ``"true"`` (lo que escribe la ingestión) o como
# booleano JSON ``true`` (si se edita la fila a mano). Fuente única de verdad para
# reconocerlo — evita que cada call site invente su propia comparación frágil.
SENTINEL_FLAG_KEY = "_sentinel"


def is_sentinel_value(value: object) -> bool:
    """True si el valor del flag de sentinela representa "activo" (string o bool)."""
    return value in ("true", True)


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
    cuil: Mapped[str | None] = mapped_column(String(13), nullable=True)
    payment_method: Mapped[str | None] = mapped_column(String(30), nullable=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    custom_fields: Mapped[dict[str, Any]] = mapped_column(
        PGJSONB, nullable=False, server_default="'{}'::jsonb", default=dict
    )
    # Soft-delete: NULL = activo; timestamp = desactivado.
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (Index("ix_suppliers_tenant_id", "tenant_id"),)

    @property
    def is_sentinel(self) -> bool:
        """¿Es el proveedor sentinela 'No identificado' (agrupa compras sin proveedor)?"""
        return is_sentinel_value((self.custom_fields or {}).get(SENTINEL_FLAG_KEY))

    def __repr__(self) -> str:
        return f"<Supplier tenant={self.tenant_id} name={self.name!r}>"
