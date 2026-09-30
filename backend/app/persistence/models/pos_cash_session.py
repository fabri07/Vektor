"""Turno de caja: quién cobró, en qué caja, y cuánto había al cerrar (B8).

Un turno es de UN cajero en UNA caja. Abre con el fondo que declara quien lo
abre, acumula ventas, y al cerrar se cuenta el efectivo del cajón contra lo que
el sistema esperaba.

**El id lo genera la caja.** ``client_session_id`` lo crea el cliente al abrir
el turno —incluso sin red, que es lo que B7b habilitó— y el servidor lo adopta.
Por eso B7 ya venía guardando ese id en cada ``pos_operation``: sin él, una
venta que sincroniza después del arqueo no se puede atribuir a su turno nunca
más.

**Un arqueo cerrado no se reescribe.** Los tres números del cierre se congelan
y una venta que llega después no los toca: queda en ``PosLateSale``. Reescribir
el arqueo haría que el número que el cajero firmó deje de ser el que firmó.

**Al cajón sólo entra el efectivo.** Tarjeta, transferencia y fiado no están
ahí, así que no entran al número que se compara contra el conteo físico.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

#: Un turno abierto todavía admite ventas; uno cerrado ya tiene su arqueo firmado.
ESTADO_ABIERTO = "OPEN"
ESTADO_CERRADO = "CLOSED"
#: Cerrado SIN arqueo: esperado, contado y diferencia quedan en NULL. Es para
#: los turnos que nadie va a contar (los que la caja creaba en cada recarga):
#: cerrarlos con un conteo inventado firmaría un arqueo falso.
ESTADO_ABANDONADO = "ABANDONED"


class PosCashSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """El turno de un cajero en una caja, con su arqueo."""

    __tablename__ = "pos_cash_sessions"
    __table_args__ = (
        # Dos aperturas del mismo turno —la explícita y la que adopta la primera
        # venta que llega— no pueden crear dos filas.
        UniqueConstraint(
            "tenant_id", "client_session_id", name="uq_pos_cash_sessions_client_session"
        ),
        Index("ix_pos_cash_sessions_tenant_status", "tenant_id", "status"),
        CheckConstraint(
            f"status IN ('{ESTADO_ABIERTO}','{ESTADO_CERRADO}','{ESTADO_ABANDONADO}')",
            name="ck_pos_cash_sessions_status",
        ),
        CheckConstraint("opening_float_ars >= 0", name="ck_pos_cash_sessions_fondo"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: El id que generó la caja. Liga las ventas con este turno.
    client_session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    #: `SET NULL` y no `CASCADE`: dar de baja la caja no borra el arqueo.
    terminal_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pos_terminals.id", ondelete="SET NULL"), nullable=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True
    )
    opening_float_ars: Mapped[Decimal] = mapped_column(
        Numeric(14, 2), nullable=False, server_default="0", default=Decimal("0")
    )
    #: Cuándo quien abrió declaró el fondo. NULL si el turno lo adoptó una
    #: venta que llegó antes que la apertura (la apertura puede ser sin red):
    #: la primera apertura explícita que llega fija el fondo aunque el turno ya
    #: exista, y las siguientes ya no lo cambian.
    float_declared_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True
    )
    #: Los tres se escriben AL CERRAR y no se vuelven a tocar.
    expected_cash_ars: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    counted_cash_ars: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    difference_ars: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=ESTADO_ABIERTO, default=ESTADO_ABIERTO
    )

    @property
    def esta_abierto(self) -> bool:
        return self.status == ESTADO_ABIERTO


class PosLateSale(UUIDPrimaryKeyMixin, Base):
    """Una venta que llegó después de que su turno cerrara.

    No modifica el arqueo firmado. Existe para que alguien la concilie: es plata
    que entró en ese turno y que el conteo de ese turno no pudo haber visto.
    """

    __tablename__ = "pos_late_sales"
    __table_args__ = (
        UniqueConstraint("operation_id", name="uq_pos_late_sales_operacion"),
        Index("ix_pos_late_sales_tenant_session", "tenant_id", "session_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pos_cash_sessions.id", ondelete="CASCADE"), nullable=False
    )
    operation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pos_operations.id", ondelete="CASCADE"), nullable=False
    )
    #: Cuánto de esa venta era efectivo: es lo único que habría movido el arqueo.
    #: Se guarda en vez de recalcularlo, porque después puede anularse.
    cash_ars: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
