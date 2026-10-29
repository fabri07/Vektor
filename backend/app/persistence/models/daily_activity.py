"""ORM model: tenant_daily_activity.

Una fila por (usuario, día de negocio argentino) en que el usuario hizo al menos
una petición autenticada. Es la fuente de "último uso" y "días activos" del panel
SUPERADMIN. No es el log de eventos de funnel (`user_activity_events`).
"""

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.db.base import Base


class TenantDailyActivity(Base):
    __tablename__ = "tenant_daily_activity"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        primary_key=True,
    )
    activity_date: Mapped[date] = mapped_column(Date, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        return f"<TenantDailyActivity user={self.user_id} date={self.activity_date}>"
