"""ORM models: access_requests + access_request_tokens.

Tablas PÚBLICAS (sin ``tenant_id``): las genera un visitante anónimo desde el
formulario de solicitud de acceso, ANTES de que exista ningún tenant. Cuando el
dueño aprueba, recién ahí se crean Tenant/User/Subscription/BusinessProfile y la
solicitud queda apuntando a ellos (``approved_tenant_id`` / ``approved_user_id``).

Los CHECK de esta tabla no son decorativos: son la garantía a nivel DB de dos
decisiones de producto que un bug de aplicación no debe poder violar.

1. **``otros`` es inescribible como vertical operativo.** El solicitante puede
   declarar ``requested_vertical='otros'`` (obligado a explicar en
   ``vertical_other_text``), pero ``assigned_vertical_code`` solo acepta los tres
   verticales reales, y una solicitud aprobada no puede quedarse sin él.
2. **``requested_plan`` es intención, no suscripción.** Se conserva para ordenar
   la cola de revisión y medir demanda; la suscripción creada al aprobar es
   siempre ``FREE``.

**Sin columna ``is_priority``**: la prioridad de la cola se DERIVA (``premium``
primero, después la verificada más antigua). Un booleano redundante solo
habilitaría estados incoherentes (``requested_plan='free'`` + ``is_priority``).

El índice único parcial ``uq_access_requests_open_email`` (un solo trámite
abierto por email) vive SOLO en la migración ``20260806_0001`` y solo en
PostgreSQL — misma convención que ``uq_customers_dni_per_tenant`` y los demás
índices parciales del repo. En SQLite (tests) se omite a propósito: declararlo
sin el predicado lo volvería un único TOTAL, que prohibiría casos legítimos
(volver a solicitar después de un rechazo) con un modo de falla que en Postgres
no existe. La dedup de aplicación es la que cubre los tests.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.access_request import AccessRequestStatus, RequestedPlan
from app.domain.contact_lead import EmailNotificationStatus
from app.domain.verticals import RequestedVertical, Vertical
from app.persistence.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


def _sql_in(values: tuple[str, ...]) -> str:
    """Renderiza una lista de literales para un ``IN (...)`` de SQL."""
    return ", ".join(f"'{v}'" for v in values)


# Los predicados se DERIVAN de los enums canónicos a propósito: escritos a mano
# se desincronizan. La migración 20260806_0001 los repite como literales (una
# migración es una foto del pasado, no sigue la evolución del código); si alguno
# cambia acá, cambia allá con una migración nueva.
_STATUS_VALUES = tuple(s.value for s in AccessRequestStatus)
_PLAN_VALUES = tuple(p.value for p in RequestedPlan)
_VERTICAL_VALUES = tuple(v.value for v in Vertical)
_REQUESTED_VERTICAL_VALUES = tuple(v.value for v in RequestedVertical)


class AccessRequest(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "access_requests"

    # --- Contacto -----------------------------------------------------------
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    business_name: Mapped[str] = mapped_column(String(200), nullable=False)

    # --- Rubro declarado por el solicitante ---------------------------------
    # Acepta 'otros' (a diferencia de assigned_vertical_code, más abajo).
    requested_vertical: Mapped[str] = mapped_column(String(40), nullable=False)
    vertical_other_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Intención de plan (NO es una suscripción) --------------------------
    requested_plan: Mapped[str] = mapped_column(String(20), nullable=False)

    # --- Screening del negocio ----------------------------------------------
    years_operating: Mapped[str] = mapped_column(String(20), nullable=False)
    staff_size: Mapped[str] = mapped_column(String(20), nullable=False)
    monthly_revenue_band: Mapped[str] = mapped_column(String(20), nullable=False)
    main_concern: Mapped[str] = mapped_column(String(10), nullable=False)
    records_format: Mapped[str] = mapped_column(String(20), nullable=False)
    history_depth: Mapped[str] = mapped_column(String(20), nullable=False)
    can_share_files: Mapped[str] = mapped_column(String(20), nullable=False)
    records_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    applicant_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Estado + doble opt-in ----------------------------------------------
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=AccessRequestStatus.UNVERIFIED.value,
        index=True,
    )
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- Revisión manual ----------------------------------------------------
    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reviewed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
    )
    # 'api' | 'script' — el script de consola revisa sin usuario logueado.
    reviewed_via: Mapped[str | None] = mapped_column(String(10), nullable=True)
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Resultado de la aprobación -----------------------------------------
    # El vertical lo asigna el DUEÑO, no el solicitante: nunca puede ser 'otros'.
    assigned_vertical_code: Mapped[str | None] = mapped_column(
        String(40), nullable=True
    )
    approved_tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="SET NULL"),
        nullable=True,
    )
    approved_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="SET NULL"),
        nullable=True,
    )

    # --- Estado de los tres emails del flujo --------------------------------
    verification_email_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=EmailNotificationStatus.PENDING.value
    )
    owner_notification_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=EmailNotificationStatus.PENDING.value
    )
    decision_email_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=EmailNotificationStatus.PENDING.value
    )

    # --- Consentimiento (Ley 25.326) ----------------------------------------
    consent_version: Mapped[str] = mapped_column(String(20), nullable=False)
    consent_accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # --- Trazabilidad / anti-abuso ------------------------------------------
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cta_source: Mapped[str | None] = mapped_column(String(60), nullable=True)
    # Subject de Google cuando la solicitud arrancó desde "Continuar con Google".
    google_subject: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        CheckConstraint(
            f"status IN ({_sql_in(_STATUS_VALUES)})",
            name="ck_access_requests_status",
        ),
        CheckConstraint(
            f"requested_plan IN ({_sql_in(_PLAN_VALUES)})",
            name="ck_access_requests_requested_plan",
        ),
        # El rubro declarado también es vocabulario cerrado. Sin este CHECK,
        # una sola fila con un valor fuera del catálogo (un script, un backfill)
        # hace levantar `AccessRequestAdminItem.model_validate` —que lo tipa
        # como `RequestedVertical`— y el listado ENTERO de la cola devuelve 500.
        CheckConstraint(
            f"requested_vertical IN ({_sql_in(_REQUESTED_VERTICAL_VALUES)})",
            name="ck_access_requests_requested_vertical",
        ),
        # Declarar 'otros' obliga a explicar de qué es el negocio.
        CheckConstraint(
            "requested_vertical <> 'otros' OR vertical_other_text IS NOT NULL",
            name="ck_access_requests_vertical_other_text",
        ),
        # 'otros' es INESCRIBIBLE como vertical asignado.
        CheckConstraint(
            "assigned_vertical_code IS NULL OR assigned_vertical_code IN "
            f"({_sql_in(_VERTICAL_VALUES)})",
            name="ck_access_requests_assigned_vertical_code",
        ),
        # Y una solicitud aprobada no puede quedarse sin vertical.
        CheckConstraint(
            "status <> 'approved' OR assigned_vertical_code IS NOT NULL",
            name="ck_access_requests_approved_needs_vertical",
        ),
        # Cola de revisión: premium primero, después la verificada más antigua.
        Index(
            "ix_access_requests_review_queue",
            "status",
            "requested_plan",
            "email_verified_at",
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<AccessRequest email={self.email!r} status={self.status!r} "
            f"plan={self.requested_plan!r}>"
        )


class AccessRequestToken(Base, TimestampMixin):
    """Token de verificación de email de una solicitud.

    Tabla propia y NO ``EmailVerificationToken``: ese modelo tiene ``user_id``
    FK NOT NULL a ``users`` y en este flujo todavía no existe ningún usuario.
    El PK ES el token que se envía por mail (UUID v4).
    """

    __tablename__ = "access_request_tokens"

    token_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    access_request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("access_requests.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    used: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<AccessRequestToken request={self.access_request_id} used={self.used}>"
        )
