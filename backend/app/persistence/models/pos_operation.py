"""ORM de la operación de caja: pos_operations, pos_operation_lines, pos_tenders.

**Por qué una tabla y no `custom_fields["sale_group_id"]`.** Hasta ahora una
venta multi-línea se agrupaba con un UUID adentro del JSON de cada
``SaleEntry``. Eso alcanza para mostrarlas juntas y para nada más: no se puede
referenciar con una FK, ni bloquear, ni auditar como un todo, ni colgarle los
pagos. Con el PATCH y el DELETE genéricos actuando sobre líneas sueltas,
"anular la venta" sobre un grupo así deja tickets a medio anular —con sus
pagos intactos— que ningún arqueo cierra.

Las ``SaleEntry`` **siguen siendo la fuente de verdad contable**: cada línea
guarda el ``sale_entry_id`` que produjo y el histórico no se toca. Esta tabla
agrega la identidad de la operación, que es lo que faltaba.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

#: Estados de una operación. `VOIDED` lo escribe la anulación de ticket (B10);
#: acá existe para que el estado nazca cerrado y no haya que migrar un CHECK.
POS_OPERATION_STATUSES = ("COMPLETED", "VOIDED")


class PosOperation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "pos_operations"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Lo genera el cliente ANTES del primer envío y viaja como Idempotency-Key.
    # Es lo que hace recuperable una respuesta perdida: sin un id nacido en la
    # caja, un reintento tras un corte es indistinguible de una venta nueva.
    client_operation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # El cajero. SET NULL y no CASCADE: dar de baja a un empleado no puede
    # borrar las ventas que cobró.
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True
    )
    customer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("customers.id", ondelete="SET NULL"), nullable=True
    )
    # Desde qué caja se cobró (B6). NULL en lo histórico y en lo que el dueño
    # carga desde el navegador sin terminal: rellenarlo sería inventar.
    terminal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pos_terminals.id", ondelete="SET NULL"), nullable=True
    )
    operation_date: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)

    #: Suma de las líneas con sus descuentos de línea, antes del global.
    subtotal_ars: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    discount_ars: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, server_default="0", default=Decimal("0")
    )
    #: Lo cobrado. Invariante: == Σ line_total == Σ tenders.
    total_ars: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    # Entregado y vuelto NO son la venta: recibir $10.000 para pagar $8.500
    # registra $8.500 de venta y $1.500 de vuelto. Viven en la cabecera
    # justamente para que ningún lector de caja los confunda con un importe.
    cash_received_ars: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    cash_change_ars: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)

    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="COMPLETED", default="COMPLETED"
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # El candado de la identidad de operación. Dos envíos de la misma caja
        # con el mismo id son la misma venta, no dos.
        UniqueConstraint(
            "tenant_id", "client_operation_id", name="uq_pos_operations_tenant_client_id"
        ),
        CheckConstraint(
            "status IN ('COMPLETED','VOIDED')", name="ck_pos_operations_status"
        ),
        CheckConstraint("total_ars >= 0", name="ck_pos_operations_total_no_negativo"),
        CheckConstraint("discount_ars >= 0", name="ck_pos_operations_discount_no_negativo"),
        # Entregado y vuelto van juntos o no van: un vuelto sin entregado no se
        # puede explicar, y un entregado sin vuelto esconde plata en el cajón.
        CheckConstraint(
            "(cash_received_ars IS NULL) = (cash_change_ars IS NULL)",
            name="ck_pos_operations_efectivo_consistente",
        ),
        Index("ix_pos_operations_tenant_fecha", "tenant_id", "operation_date"),
        Index("ix_pos_operations_tenant_cajero", "tenant_id", "created_by_user_id"),
    )

    def __repr__(self) -> str:
        return f"<PosOperation tenant={self.tenant_id} total={self.total_ars} status={self.status}>"


class PosOperationLine(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "pos_operation_lines"

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
    # La SaleEntry que esta línea produjo: el puente con lo contable. SET NULL
    # para que anular la venta no se lleve puesta la línea del ticket, que es
    # lo que permite reimprimirlo y auditar qué pasó.
    sale_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sales_entries.id", ondelete="SET NULL"), nullable=True
    )
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("products.id", ondelete="SET NULL"), nullable=True
    )
    #: Orden en el carrito. Es lo que hace reproducible el reparto de centavos:
    #: con empate de importes el residuo va a la primera línea, y "primera"
    #: tiene que significar lo mismo al releer que al cobrar.
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    #: En UNIDADES BASE, igual que `products.stock_units`.
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    #: Precio de lista al momento de vender, del catálogo — nunca del cliente.
    unit_price_list: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    discount_line_ars: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, server_default="0", default=Decimal("0")
    )
    # Cuánto del descuento global le tocó en el reparto. Se guarda aparte del
    # descuento de línea porque sin eso el número no se puede explicar: quedaría
    # un total distinto del precio por la cantidad y nada que diga por qué.
    discount_global_share_ars: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), nullable=False, server_default="0", default=Decimal("0")
    )
    line_total_ars: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    __table_args__ = (
        UniqueConstraint("operation_id", "position", name="uq_pos_operation_lines_posicion"),
        CheckConstraint("quantity > 0", name="ck_pos_operation_lines_cantidad"),
        CheckConstraint("line_total_ars >= 0", name="ck_pos_operation_lines_total_no_negativo"),
    )


class PosTender(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Un pago de una operación. Varias filas = pago mixto.

    Registrar `debit_card`/`credit_card`/`qr` acá **no procesa ni verifica** el
    cobro: con terminales externas, el cajero confirma que salió antes de cerrar
    el ticket. La tabla dice a qué método se imputó cada peso, nada más.
    """

    __tablename__ = "pos_tenders"

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
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    payment_method: Mapped[str] = mapped_column(String(30), nullable=False)
    amount_ars: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    __table_args__ = (
        UniqueConstraint("operation_id", "position", name="uq_pos_tenders_posicion"),
        CheckConstraint("amount_ars > 0", name="ck_pos_tenders_monto_positivo"),
        Index("ix_pos_tenders_tenant_metodo", "tenant_id", "payment_method"),
    )
