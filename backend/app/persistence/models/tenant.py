"""ORM models: tenants, suscripciones, catálogo de planes y cupos.

Column names match the migration schema exactly.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.persistence.db.base import PGJSONB, Base, TimestampMixin


class Tenant(TimestampMixin, Base):
    __tablename__ = "tenants"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    legal_name: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    currency: Mapped[str] = mapped_column(Text, nullable=False, default="ARS")
    pricing_reference_mode: Mapped[str] = mapped_column(Text, nullable=False, default="MEP")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="ACTIVE")
    is_demo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Relationships
    subscriptions: Mapped[list["Subscription"]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )
    users: Mapped[list["User"]] = relationship(  # type: ignore[name-defined]  # noqa: F821
        "User", back_populates="tenant", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<Tenant id={self.tenant_id} display_name={self.display_name!r}>"


class PlanDefinition(TimestampMixin, Base):
    """Catálogo de planes vigente — fuente para NUEVAS activaciones/renovaciones.

    Editarlo NUNCA cambia una `Subscription` ya otorgada: sus cupos y precio
    quedan copiados ahí (`granted_*`) en el momento de activar/renovar. Este
    catálogo solo importa para la PRÓXIMA vez que alguien active o renueve.
    """

    __tablename__ = "plan_definitions"

    plan_code: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    seats_included: Mapped[int] = mapped_column(Integer, nullable=False)
    ia_queries_per_month: Mapped[int] = mapped_column(Integer, nullable=False)
    imports_per_month: Mapped[int] = mapped_column(Integer, nullable=False)
    photo_pdf_reads_per_month: Mapped[int] = mapped_column(Integer, nullable=False)
    price_usd_reference: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    price_ars_reference: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    #: `False` retira el plan de nuevas activaciones sin borrar el catálogo
    #: (una `Subscription` ya otorgada con ese `plan_code` sigue funcionando
    #: con sus `granted_*` propios — no depende de que el plan siga ofrecido).
    is_offered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    def __repr__(self) -> str:
        return f"<PlanDefinition {self.plan_code!r}>"


class Subscription(TimestampMixin, Base):
    __tablename__ = "subscriptions"

    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    plan_code: Mapped[str] = mapped_column(
        Text, ForeignKey("plan_definitions.plan_code"), nullable=False, default="FREE"
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="ACTIVE")

    # --- Condiciones OTORGADAS — copiadas del catálogo al activar/renovar,
    # nunca resueltas en vivo. `seats_included`/`plan_price_*` ya existían;
    # los `granted_*` de abajo son la snapshot de cupos que les faltaba. -----
    seats_included: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    plan_price_usd_reference: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    plan_price_ars: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    billing_index_reference: Mapped[str] = mapped_column(Text, nullable=False, default="MEP")
    granted_ia_queries_per_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    granted_imports_per_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    granted_photo_pdf_reads_per_month: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- Vigencia — todas timezone-aware (UTC). -----------------------------
    current_period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    grace_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # ── Período SIGUIENTE ya pago (pago anticipado / cambio de plan programado) ──
    # `effective_access` lo hace regir por fecha cuando termina el actual; el
    # pase a `current_*` (`roll_forward`) es prolijidad, no requisito. Con
    # `next_period_end` en NULL, `next_plan_code` solo es un cambio PROGRAMADO
    # que se aplicará en la próxima renovación.
    next_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_plan_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_seats_included: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_granted_ia_queries_per_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_granted_imports_per_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    next_granted_photo_pdf_reads_per_month: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    #: Día del mes en que arrancó el ciclo pago. No se pierde en meses cortos
    #: (31 ene → 28 feb → 31 mar): ver `add_months_anchored`.
    billing_anchor_day: Mapped[int | None] = mapped_column(Integer, nullable=True)

    tenant: Mapped["Tenant"] = relationship(back_populates="subscriptions")

    __table_args__ = (
        CheckConstraint(
            "status IN ('TRIAL','ACTIVE','GRACE','READ_ONLY','CANCELLED')",
            name="ck_subscriptions_status",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<Subscription tenant={self.tenant_id} plan={self.plan_code!r} "
            f"status={self.status!r}>"
        )


class SubscriptionQuotaUsage(TimestampMixin, Base):
    """Contador agregado por tenant/recurso/período — `used` + `reserved`.

    `used` son unidades COMMITTED (consumo real); `reserved` son unidades con
    una reserva en curso (`SubscriptionQuotaReservation.state == RESERVED`) que
    todavía no se sabe si van a terminar bien. `limit_snapshot` es el cupo
    vigente para ESTE período puntual — copiado una sola vez al crear la fila,
    nunca actualizado si el plan cambia a mitad de período.
    """

    __tablename__ = "subscription_quota_usage"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.subscription_id", ondelete="CASCADE"),
        nullable=False,
    )
    resource: Mapped[str] = mapped_column(Text, nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reserved: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    limit_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "resource", "period_start", name="uq_quota_usage_tenant_resource_period"
        ),
        CheckConstraint(
            "resource IN ('ia_query','import','photo_pdf_read')",
            name="ck_quota_usage_resource",
        ),
        CheckConstraint("used >= 0", name="ck_quota_usage_used_non_negative"),
        CheckConstraint("reserved >= 0", name="ck_quota_usage_reserved_non_negative"),
    )


class SubscriptionQuotaReservation(TimestampMixin, Base):
    """Una reserva identificada por operación — la unidad de idempotencia.

    `operation_id` lo elige el llamador (el `request_id` del chat, el
    `attempt_id` de la importación, un id sintético de la extracción) y tiene
    que ser estable entre reintentos de la MISMA operación: reintentar con el
    mismo id encuentra esta fila en vez de reservar una unidad nueva.
    """

    __tablename__ = "subscription_quota_reservations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False
    )
    resource: Mapped[str] = mapped_column(Text, nullable=False)
    operation_id: Mapped[str] = mapped_column(Text, nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    units: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="RESERVED")

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "resource", "operation_id", name="uq_quota_reservation_operation"
        ),
        CheckConstraint(
            "resource IN ('ia_query','import','photo_pdf_read')",
            name="ck_quota_reservation_resource",
        ),
        CheckConstraint(
            "state IN ('RESERVED','COMMITTED','RELEASED')", name="ck_quota_reservation_state"
        ),
        CheckConstraint("units > 0", name="ck_quota_reservation_units_positive"),
    )


class SubscriptionPayment(TimestampMixin, Base):
    """Un pago acreditado y el período que otorgó. Insert-only.

    Es el registro comercial (la auditoría en `decision_audit_log` se conserva
    aparte): fuente, referencia, importe, operador, período y copia de las
    condiciones otorgadas. El UNIQUE `(source, reference)` es EL candado de la
    idempotencia — dos ejecuciones simultáneas de la misma referencia no pueden
    otorgar dos períodos, cosa que buscar la referencia antes de escribir no
    garantiza. Lo comparten la transferencia manual y, después, Mercado Pago.
    """

    __tablename__ = "subscription_payments"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False
    )
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("subscriptions.subscription_id", ondelete="CASCADE"),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    reference: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    plan_code: Mapped[str] = mapped_column(Text, nullable=False)
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False)
    amount_ars: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    operator: Mapped[str] = mapped_column(String(100), nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    granted_json: Mapped[dict[str, Any]] = mapped_column(PGJSONB, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("source", "reference", name="uq_subscription_payments_source_reference"),
        Index("ix_subscription_payments_tenant", "tenant_id", "period_start"),
    )
