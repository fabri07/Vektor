"""Servicio de leads del formulario público de contacto.

Persiste el lead (con deduplicación de envíos recientes), hashea la IP para
anti-abuso (nunca guarda la IP cruda) y arma el email de notificación. El ENVÍO
del email NO ocurre acá: lo hace una tarea Celery desacoplada
(``app/jobs/contact_lead_worker.py``) para que un fallo del correo nunca pierda
el lead ni rompa la respuesta al visitante.
"""

from __future__ import annotations

import hashlib
import html
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.domain.contact_lead import (
    CONSENT_VERSION,
    DEDUP_WINDOW_SECONDS,
    EmailNotificationStatus,
    LeadStatus,
)
from app.persistence.models.contact_lead import ContactLead


@dataclass(frozen=True)
class LeadInput:
    """Datos ya validados/normalizados listos para persistir."""

    nombre: str
    celular: str
    email: str
    empresa: str
    rubro: str
    cantidad_usuarios: str
    gestion_actual: str | None
    cta_source: str | None


def hash_ip(ip: str | None) -> str | None:
    """SHA-256 de la IP con sal del SECRET_KEY. Guardamos el hash, no la IP,
    solo para detección de abuso. Retención acotada, documentado en /privacidad."""
    if not ip:
        return None
    salt = get_settings().SECRET_KEY
    return hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()


async def _recent_duplicate(
    session: AsyncSession, *, email: str, empresa: str
) -> ContactLead | None:
    """Busca un lead con el mismo email+empresa creado dentro de la ventana de
    deduplicación (doble clic / reintento del navegador)."""
    cutoff = datetime.now(UTC) - timedelta(seconds=DEDUP_WINDOW_SECONDS)
    result = await session.execute(
        select(ContactLead)
        .where(
            ContactLead.email == email,
            ContactLead.empresa == empresa,
            ContactLead.created_at >= cutoff,
        )
        .order_by(ContactLead.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def create_lead(
    session: AsyncSession, *, data: LeadInput, ip_hash: str | None
) -> tuple[ContactLead, bool]:
    """Crea el lead o devuelve el duplicado reciente.

    Returns ``(lead, created)``. Si ``created`` es False, es un duplicado reciente
    y NO se debe reencolar el email. El commit lo hace el llamador (router).
    """
    existing = await _recent_duplicate(
        session, email=data.email, empresa=data.empresa
    )
    if existing is not None:
        return existing, False

    lead = ContactLead(
        nombre=data.nombre,
        celular=data.celular,
        email=data.email,
        empresa=data.empresa,
        rubro=data.rubro,
        cantidad_usuarios=data.cantidad_usuarios,
        gestion_actual=data.gestion_actual,
        status=LeadStatus.NEW.value,
        email_notification_status=EmailNotificationStatus.PENDING.value,
        consent_version=CONSENT_VERSION,
        consent_accepted_at=datetime.now(UTC),
        cta_source=data.cta_source,
        ip_hash=ip_hash,
    )
    session.add(lead)
    await session.flush()  # asigna lead.id sin cerrar la transacción
    return lead, True


def build_lead_email(lead: ContactLead) -> tuple[str, str, str]:
    """Arma ``(subject, html, text)`` de la notificación interna del lead."""
    subject = f"Nuevo contacto: {lead.empresa} ({lead.rubro})"
    rows = [
        ("Nombre", lead.nombre),
        ("Celular", lead.celular),
        ("Email", lead.email),
        ("Empresa", lead.empresa),
        ("Rubro", lead.rubro),
        ("Usuarios", lead.cantidad_usuarios),
        ("Gestión actual", lead.gestion_actual or "—"),
        ("Origen", lead.cta_source or "—"),
    ]
    # Escapar los valores del lead (input del visitante) antes de meterlos en el
    # HTML del email interno — evita inyección de HTML/markup en la notificación.
    html_rows = "".join(
        f"<tr><td style='padding:4px 12px 4px 0;color:#666'>{html.escape(k)}</td>"
        f"<td style='padding:4px 0'><strong>{html.escape(str(v))}</strong></td></tr>"
        for k, v in rows
    )
    html = (
        "<h2>Nuevo lead de contacto</h2>"
        f"<table style='font-family:sans-serif;font-size:14px'>{html_rows}</table>"
        "<p style='color:#999;font-size:12px'>Enviado desde el formulario público de Véktor.</p>"
    )
    text = "Nuevo lead de contacto\n\n" + "\n".join(f"{k}: {v}" for k, v in rows)
    return subject, html, text
