"""ORM model: pending_actions.

Acciones de negocio en espera de confirmación del usuario (riesgo MEDIUM/HIGH).
TTL de 10 minutos.

Dos ejes de estado independientes:
  - `status`           → estado de aprobación del usuario (PENDING|APPROVED|REJECTED|EXPIRED)
  - `execution_status` → estado de ejecución              (NOT_STARTED|IN_PROGRESS|SUCCEEDED|FAILED|REQUIRES_RECONNECT)

Ciclo de vida para acciones LOCALES (external_system=None):
  - create_pending_action() → status=PENDING, execution_status=NOT_STARTED
  - confirm() → status=APPROVED, execution_status=SUCCEEDED (mismo commit)
  - No tienen idempotency_key ni retry.

Ciclo de vida para acciones EXTERNAS (CREATE_SUPPLIER_DRAFT, SYNC_TO_GOOGLE):
  - create_pending_action() → status=PENDING, execution_status=NOT_STARTED,
                               external_system="GOOGLE_GMAIL|GOOGLE_SHEETS",
                               idempotency_key=<uuid único>
  - confirm() → status=APPROVED, execution_status=IN_PROGRESS (flush)
              → éxito:            execution_status=SUCCEEDED
              → WorkspaceTokenError: execution_status=REQUIRES_RECONNECT, failure_code=exc.reason
              → excepción genérica: execution_status=FAILED, failure_message=str(exc)[:500]
  - retry() → solo válido cuando is_retryable (APPROVED + FAILED|REQUIRES_RECONNECT)
               y no existe aún un registro AGENT_ACTION_RETRIED en DecisionAuditLog
               (límite de 1 retry por acción)
  - idempotency_key: se genera al crear y NUNCA cambia; el UNIQUE INDEX evita
    que se creen dos acciones externas con la misma clave.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import PGJSONB, Base, UUIDPrimaryKeyMixin


class PendingAction(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "pending_actions"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    action_type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(PGJSONB, nullable=False)
    risk_level: Mapped[str] = mapped_column(Text, nullable=False)

    # ── Estado de aprobación ──────────────────────────────────────────────────
    # PENDING | APPROVED | REJECTED | EXPIRED
    status: Mapped[str] = mapped_column(Text, nullable=False, default="PENDING")
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC) + timedelta(minutes=10),
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Alias histórico; para acciones locales sigue siendo el timestamp de ejecución
    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Estado de ejecución externa ───────────────────────────────────────────
    # NOT_STARTED | IN_PROGRESS | SUCCEEDED | FAILED | REQUIRES_RECONNECT
    execution_status: Mapped[str] = mapped_column(
        Text, nullable=False, default="NOT_STARTED"
    )
    # Sistema externo al que apunta (GOOGLE_GMAIL, GOOGLE_DRIVE, GOOGLE_SHEETS…)
    external_system: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Clave de idempotencia: garantiza que la operación externa no se ejecute dos veces.
    # UNIQUE INDEX en DB (parcial: WHERE idempotency_key IS NOT NULL).
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Detalles del fallo (failure_code: invalid_grant, insufficient_scope, etc.)
    failure_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    @property
    def is_retryable(self) -> bool:
        """True si la acción puede reintentarse (aprobada pero fallo de ejecución)."""
        return (
            self.status == "APPROVED"
            and self.execution_status in ("FAILED", "REQUIRES_RECONNECT")
        )

    @property
    def is_external(self) -> bool:
        """True si la acción involucra un sistema externo."""
        return self.external_system is not None

    def __repr__(self) -> str:
        return (
            f"<PendingAction id={self.id} type={self.action_type!r}"
            f" status={self.status!r} exec={self.execution_status!r}"
            f" tenant={self.tenant_id}>"
        )
