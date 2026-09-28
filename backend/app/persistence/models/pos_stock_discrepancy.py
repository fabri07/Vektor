"""Salida pendiente: lo que se vendió y el stock no pudo respaldar (B11).

Una venta de caja cobrada sin conexión puede llegar al servidor cuando ya no hay
stock: otro cajero vendió lo último, o el conteo estaba mal. Rechazarla no
deshace nada —la plata entró y el cliente se fue con la mercadería—, así que la
venta entra completa y lo que queda es una **deuda de stock** que alguien tiene
que resolver.

**Se registra la salida PENDIENTE, no el faltante.** Si había 3 y se vendieron
5, quedan **5 unidades pendientes de salir**, no 2 de diferencia: registrar un
ajuste por 2 dejaría el stock mal, porque las 5 salieron del negocio.

`pending_units` es la cantidad COMPLETA de la línea. `stock_at_capture` es lo
que había cuando se capturó: sirve para explicar el caso, nunca para calcular la
resolución —eso se recalcula al resolver, porque entre medio pudo haber compras
o ventas.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

ESTADO_ABIERTA = "OPEN"
ESTADO_RESUELTA = "RESOLVED"
ESTADOS = (ESTADO_ABIERTA, ESTADO_RESUELTA)

#: "La mercadería estaba, el sistema no la tenía cargada": entra el faltante como
#: ajuste de reconciliación y después sale la venta completa.
RESOLUCION_AJUSTE = "ajuste_de_entrada"
#: "La venta está mal": se anula el ticket entero (B10).
RESOLUCION_ANULAR = "anular_ticket"
RESOLUCIONES = (RESOLUCION_AJUSTE, RESOLUCION_ANULAR)


class PosStockDiscrepancy(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "pos_stock_discrepancies"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    operation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pos_operations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Una línea no puede tener dos deudas de stock: el UNIQUE lo garantiza.
    line_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("pos_operation_lines.id", ondelete="CASCADE"),
        nullable=False,
    )
    # SET NULL en las dos: que se anule la venta o desaparezca el producto no
    # puede borrar el registro de que hubo una deuda de stock.
    sale_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sales_entries.id", ondelete="SET NULL"), nullable=True
    )
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )

    #: La cantidad COMPLETA de la línea, en unidades base. Ver el docstring.
    pending_units: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Lo que había al capturar. Diagnóstico, nunca insumo del cálculo.
    stock_at_capture: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=ESTADO_ABIERTA, default=ESTADO_ABIERTA
    )
    resolution: Mapped[str | None] = mapped_column(String(30), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("line_id", name="uq_pos_stock_discrepancies_linea"),
        CheckConstraint("pending_units > 0", name="ck_pos_stock_discrepancies_pendiente"),
        CheckConstraint(
            "status IN ('OPEN','RESOLVED')", name="ck_pos_stock_discrepancies_status"
        ),
        Index("ix_pos_stock_discrepancies_tenant_status", "tenant_id", "status"),
    )

    def __repr__(self) -> str:
        return (
            f"<PosStockDiscrepancy tenant={self.tenant_id} "
            f"pendiente={self.pending_units} status={self.status}>"
        )
