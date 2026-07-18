"""ORM model for tenant_maintenance_locks (Fase 3 — dedup auditado de productos)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import Base, UUIDPrimaryKeyMixin


class TenantMaintenanceLock(UUIDPrimaryKeyMixin, Base):
    """Lease por-tenant para evitar corridas de mantenimiento concurrentes.

    Una fila por tenant activo (``uq_tenant_maintenance_locks_tenant``): el
    script de dedup de productos (Fase 3) adquiere el lease antes de mergear
    duplicados y lo libera/expira al terminar, evitando pisar un import o
    otra corrida en vuelo del mismo tenant.
    """

    __tablename__ = "tenant_maintenance_locks"

    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    lease_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(200), nullable=False)
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(String(120), nullable=False)

    __table_args__ = (UniqueConstraint("tenant_id", name="uq_tenant_maintenance_locks_tenant"),)

    def __repr__(self) -> str:
        return (
            f"<TenantMaintenanceLock tenant={self.tenant_id} lease={self.lease_id} "
            f"expires_at={self.expires_at!r}>"
        )
