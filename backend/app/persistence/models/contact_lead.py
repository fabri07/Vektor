"""ORM model: contact_leads — leads del formulario público de contacto.

Tabla PÚBLICA (sin tenant_id): la genera el visitante anónimo de la landing.
Guarda solo lo necesario para que el equipo comercial dé seguimiento, más el
consentimiento de privacidad y un hash de IP (no la IP cruda) para abuso/spam.
"""

from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.contact_lead import EmailNotificationStatus, LeadStatus
from app.persistence.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ContactLead(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "contact_leads"

    # Datos del contacto
    nombre: Mapped[str] = mapped_column(String(120), nullable=False)
    celular: Mapped[str] = mapped_column(String(40), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    empresa: Mapped[str] = mapped_column(String(160), nullable=False)
    rubro: Mapped[str] = mapped_column(String(40), nullable=False)
    cantidad_usuarios: Mapped[str] = mapped_column(String(20), nullable=False)
    gestion_actual: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Estado operativo + notificación
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=LeadStatus.NEW.value, index=True
    )
    email_notification_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=EmailNotificationStatus.PENDING.value
    )

    # Consentimiento de privacidad (Ley 25.326)
    consent_version: Mapped[str] = mapped_column(String(20), nullable=False)
    consent_accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    # Trazabilidad / anti-abuso
    cta_source: Mapped[str | None] = mapped_column(String(60), nullable=True)
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    def __repr__(self) -> str:
        return f"<ContactLead email={self.email!r} status={self.status!r}>"
