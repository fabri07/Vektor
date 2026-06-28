"""ORM model: purchase_orders (borradores de pedidos a proveedor)."""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import ForeignKey, Index, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import PGJSONB, Base, TimestampMixin, UUIDPrimaryKeyMixin

__all__ = ["PurchaseOrder"]


class PurchaseOrder(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Borrador de pedido a proveedor generado por AgentSupplier.

    ``status="draft"`` no compromete dinero: el usuario lo edita/confirma.
    ``items`` es una lista JSONB de ``PurchaseOrderItem`` serializado.
    ``supplier_id`` puede ser NULL si el agente no resolvió el proveedor.
    """

    __tablename__ = "purchase_orders"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
    )
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("suppliers.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="draft",
        server_default="draft",
    )
    total: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        nullable=False,
        default=Decimal("0"),
        server_default="0",
    )
    items: Mapped[list[dict[str, Any]]] = mapped_column(
        PGJSONB,
        nullable=False,
        default=list,
        server_default="'[]'::jsonb",
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (Index("ix_purchase_orders_tenant_id", "tenant_id"),)

    def __repr__(self) -> str:
        return (
            f"<PurchaseOrder tenant={self.tenant_id} supplier={self.supplier_id} "
            f"status={self.status!r} total={self.total}>"
        )
