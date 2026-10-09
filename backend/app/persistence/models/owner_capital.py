"""Capital de los socios: aportes, retiros y préstamos (D·2).

Un aporte, un retiro o un préstamo de socio **no es venta ni gasto**. Meterlo en
``sales_entries`` o ``expense_entries`` contamina ventas, margen, gastos y el
score; por eso vive en su propia tabla.

**El importe es siempre positivo; el sentido lo da ``kind``.** Entra plata con
``aporte`` y ``prestamo_socio``; sale con ``retiro`` y ``devolucion_prestamo``.

**No se borra.** Una carga equivocada se anula (``voided_at`` + quién + motivo)
y deja de contar en todos lados; la fila queda para la auditoría.

**Procedencia** (``source_type``): ``manual`` (API), ``archivo`` (con
``source_upload_id`` + ``source_ref``), ``onboarding_declarado`` y
``migrado_de_gasto`` (``source_ref`` = id del gasto original, para revertir).
Los tres últimos los escriben pasos posteriores (D·3, D·5, D·7); el CHECK ya los
admite para que esos pasos no necesiten otra migración.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, Index, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

KIND_APORTE = "aporte"
KIND_RETIRO = "retiro"
KIND_PRESTAMO_SOCIO = "prestamo_socio"
KIND_DEVOLUCION_PRESTAMO = "devolucion_prestamo"

#: Movimientos con los que ENTRA plata al negocio.
KINDS_ENTRADA: tuple[str, ...] = (KIND_APORTE, KIND_PRESTAMO_SOCIO)
#: Movimientos con los que SALE plata del negocio.
KINDS_SALIDA: tuple[str, ...] = (KIND_RETIRO, KIND_DEVOLUCION_PRESTAMO)
KINDS: tuple[str, ...] = KINDS_ENTRADA + KINDS_SALIDA

FORM_DINERO = "dinero"
FORM_ESPECIE = "especie"
FORMS: tuple[str, ...] = (FORM_DINERO, FORM_ESPECIE)

SOURCE_MANUAL = "manual"
SOURCE_TYPES: tuple[str, ...] = (
    SOURCE_MANUAL,
    "archivo",
    "onboarding_declarado",
    "migrado_de_gasto",
)

#: Los mismos medios que el resto del sistema (``PAYMENT_METHOD_PATTERN``).
PAYMENT_METHODS: tuple[str, ...] = (
    "cash",
    "debit_card",
    "credit_card",
    "transfer",
    "qr",
    "account",
    "other",
)


def _in(col: str, valores: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in valores)})"


class OwnerCapitalMovement(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Un movimiento de capital entre un socio y el negocio."""

    __tablename__ = "owner_capital_movements"
    __table_args__ = (
        CheckConstraint(_in("kind", KINDS), name="ck_owner_capital_movements_kind"),
        CheckConstraint(_in("form", FORMS), name="ck_owner_capital_movements_form"),
        CheckConstraint(
            _in("source_type", SOURCE_TYPES), name="ck_owner_capital_movements_source_type"
        ),
        CheckConstraint("amount_ars > 0", name="ck_owner_capital_movements_amount_positive"),
        # El medio de pago sólo tiene sentido si se movió plata; en especie no hay.
        # El IS NOT NULL es necesario: con NULL, el IN da NULL y un CHECK que da
        # NULL PASA, así que un movimiento en dinero sin medio entraba igual.
        CheckConstraint(
            "(form = 'dinero' AND payment_method IS NOT NULL"
            f" AND {_in('payment_method', PAYMENT_METHODS)})"
            " OR (form = 'especie' AND payment_method IS NULL)",
            name="ck_owner_capital_movements_payment_method",
        ),
        Index("ix_owner_capital_movements_tenant_date", "tenant_id", "occurred_on"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    form: Mapped[str] = mapped_column(String(20), nullable=False, default=FORM_DINERO)
    # Socio: nombre libre por movimiento. No hay maestro de socios a propósito.
    partner_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Fecha de NEGOCIO (invariante 2d): cuándo ocurrió, no cuándo se cargó.
    occurred_on: Mapped[date] = mapped_column(Date, nullable=False)
    payment_method: Mapped[str | None] = mapped_column(String(30), nullable=True)
    amount_ars: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    notes: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    source_type: Mapped[str] = mapped_column(
        String(30), nullable=False, default=SOURCE_MANUAL
    )
    source_upload_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("uploaded_files.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
    )
    voided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    voided_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
    )
    void_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
