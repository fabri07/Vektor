"""ORM models: inventory_balances, inventory_movements."""

import uuid
from decimal import Decimal

from sqlalchemy import ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class InventoryBalance(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Current stock level per product per tenant."""

    __tablename__ = "inventory_balances"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    current_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:
        return (
            f"<InventoryBalance product={self.product_id}"
            f" qty={self.current_qty} reserved={self.reserved_qty}>"
        )


class InventoryMovement(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Immutable log of every stock change. Insert-only."""

    __tablename__ = "inventory_movements"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # movement_type: sale | purchase | loss | adjustment | return
    movement_type: Mapped[str] = mapped_column(String(30), nullable=False)
    # positive = stock increase, negative = stock decrease
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    source_event_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<InventoryMovement product={self.product_id}"
            f" type={self.movement_type!r} qty={self.qty:+d}>"
        )
