"""Router público: formulario de contacto ('Contactanos').

Endpoint SIN auth ni tenant (excepción consciente: lo usa un visitante anónimo).
Anti-spam en capas (rate limit por IP + honeypot + tiempo mínimo de envío) e
idempotencia por duplicado reciente.

REGLA CARDINAL: se persiste el lead y se responde éxito ANTES de intentar el
email; la notificación va desacoplada en una tarea Celery. Un fallo del correo
nunca pierde el lead ni le muestra error al visitante.
"""

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.contact_lead_service import (
    LeadInput,
    create_lead,
    hash_ip,
)
from app.domain.contact_lead import CONSENT_VERSION, MIN_SUBMIT_ELAPSED_MS
from app.main import limiter
from app.observability.logger import get_logger
from app.persistence.db.session import get_db_session
from app.schemas.contact import CreateLeadRequest, LeadResponse

router = APIRouter()
logger = get_logger(__name__)

_OK = LeadResponse()


def _client_ip(request: Request) -> str | None:
    """IP real detrás del proxy (Railway) o la del cliente directo."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def _looks_like_bot(body: CreateLeadRequest) -> bool:
    """Honeypot completado o envío sospechosamente rápido ⇒ bot."""
    if body.website:  # el honeypot debe venir vacío
        return True
    return body.elapsed_ms is not None and body.elapsed_ms < MIN_SUBMIT_ELAPSED_MS


@router.post(
    "/leads",
    response_model=LeadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Enviar un lead del formulario público de contacto",
)
@limiter.limit("5/hour")
async def create_contact_lead(
    request: Request,
    body: CreateLeadRequest,
    session: AsyncSession = Depends(get_db_session),
) -> LeadResponse:
    # Anti-spam silencioso: al bot le devolvemos éxito sin persistir ni notificar.
    if _looks_like_bot(body):
        logger.info("contact_lead.spam_dropped")
        return _OK

    # El backend es autoritativo sobre la versión del consentimiento; si el front
    # declara otra, lo dejamos registrado (posible front desactualizado).
    if body.consent_version and body.consent_version != CONSENT_VERSION:
        logger.warning(
            "contact_lead.consent_version_mismatch",
            client_version=body.consent_version,
            server_version=CONSENT_VERSION,
        )

    ip_hash = hash_ip(_client_ip(request))
    lead, created = await create_lead(
        session,
        data=LeadInput(
            nombre=body.nombre,
            celular=body.celular,
            email=body.email,
            empresa=body.empresa,
            rubro=body.rubro.value,
            cantidad_usuarios=body.cantidad_usuarios.value,
            gestion_actual=body.gestion_actual,
            cta_source=body.cta_source,
        ),
        ip_hash=ip_hash,
    )
    # Persistimos y confirmamos ANTES de tocar el email (regla cardinal).
    await session.commit()

    if not created:
        # Duplicado reciente (doble clic/reintento): idempotente, no reencolar.
        logger.info("contact_lead.duplicate", lead_id=str(lead.id))
        return _OK

    # Notificación por email desacoplada; su fallo no afecta esta respuesta.
    try:
        from app.jobs.contact_lead_worker import notify_contact_lead  # noqa: PLC0415

        notify_contact_lead.delay(str(lead.id))
    except Exception as exc:  # noqa: BLE001 — encolar nunca debe romper el alta
        logger.warning("contact_lead.enqueue_failed", lead_id=str(lead.id), error=str(exc))

    logger.info("contact_lead.created", lead_id=str(lead.id), rubro=lead.rubro)
    return _OK
