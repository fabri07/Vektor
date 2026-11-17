"""Pagos de deuda a proveedores: supplier_payments.

Un pago a un proveedor por una compra que quedó a cuenta corriente **no es un gasto ni
una compra**: la compra fiada ya se contó como costo el día que se hizo, y sumar el
pago como otro gasto contaba el mismo costo dos veces (margen más bajo y caja restada
dos veces). Lo que hace es SACAR plata de la caja y BAJAR lo que se le debe al
proveedor; no toca el resultado ni el margen.

Mismo contrato que el capital de los socios (``owner_capital``): el importe es siempre
positivo, no se borra —se anula con quién y por qué— y la procedencia dice si lo cargó
una persona o un archivo (``source_upload_id``; borrar el archivo lo anula).

La deuda de un proveedor no se guarda: se calcula (compras a cuenta corriente menos
pagos vivos), así que no hay un saldo que se pueda desfasar.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

SOURCE_MANUAL = "manual"
SOURCE_ARCHIVO = "archivo"
SOURCE_TYPES: tuple[str, ...] = (SOURCE_MANUAL, SOURCE_ARCHIVO)

#: Con qué se pagó. `account` no: pagar «a cuenta corriente» no saca plata de nada.
PAYMENT_METHODS: tuple[str, ...] = (
    "cash",
    "debit_card",
    "credit_card",
    "transfer",
    "qr",
    "other",
)


def _in(col: str, valores: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in valores)})"


class SupplierPayment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Un pago de deuda a un proveedor."""

    __tablename__ = "supplier_payments"
    __table_args__ = (
        CheckConstraint("amount_ars > 0", name="ck_supplier_payments_amount_positive"),
        CheckConstraint(_in("payment_method", PAYMENT_METHODS), name="ck_supplier_payments_method"),
        CheckConstraint(_in("source_type", SOURCE_TYPES), name="ck_supplier_payments_source_type"),
        Index("ix_supplier_payments_tenant_date", "tenant_id", "occurred_on"),
        Index("ix_supplier_payments_tenant_supplier", "tenant_id", "supplier_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("suppliers.id", ondelete="CASCADE"), nullable=False
    )
    #: Fecha de NEGOCIO (invariante 2d): cuándo se pagó, no cuándo se cargó.
    occurred_on: Mapped[date] = mapped_column(Date, nullable=False)
    amount_ars: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    payment_method: Mapped[str] = mapped_column(String(30), nullable=False)
    notes: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    source_type: Mapped[str] = mapped_column(String(30), nullable=False, default=SOURCE_MANUAL)
    source_upload_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("uploaded_files.id", ondelete="SET NULL"), nullable=True
    )
    source_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True
    )
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    voided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True
    )
    void_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
