"""Routers de solicitudes de acceso: el formulario público y la cola de revisión.

Dos routers en un archivo porque son las dos caras del MISMO trámite, y separarlos
invita a que la ficha administrativa y el payload público se desincronicen:

* **Público** (`/access-requests`, sin auth ni tenant — excepción consciente, lo
  usa un visitante anónimo): alta, doble opt-in y reenvío del mail. Anti-spam en
  capas (rate limit por IP + honeypot + tiempo mínimo de envío), como
  `api/v1/contact.py`.
* **SUPERADMIN** (`/admin/access-requests`): listar la cola, ver una solicitud y
  las tres decisiones (aprobar / rechazar / postergar). `require_role("SUPERADMIN")`
  por endpoint, convención de `api/v1/admin.py`.

**Neutralidad a enumeración de cuentas.** Los tres endpoints públicos devuelven
SIEMPRE el mismo status y el mismo cuerpo, exista o no una cuenta con ese email.
El `POST /auth/register` viejo respondía 409 *"An account with this email already
exists"*, que es un oráculo de enumeración; este endpoint es anónimo y no puede
repetirlo. El único canal que distingue el caso es la casilla del dueño del
correo (el mail "ya tenés cuenta"), y ese mail lo manda el servicio.

Este archivo **no tiene lógica de negocio**: toda la máquina de estados vive en
`AccessRequestService`. Acá solo se traduce HTTP ↔ servicio y errores de dominio ↔
códigos de estado.
"""

# ⚠️ Este módulo NO lleva `from __future__ import annotations`, igual que el resto
# de los routers. Con PEP 563 las anotaciones quedan como strings y FastAPI las
# resuelve contra el `__globals__` de la función que recibe — que, detrás del
# wrapper de `@limiter.limit`, es el módulo de slowapi: el schema del body no se
# resuelve y el parámetro termina interpretado como query param (422 "field
# required" en `["query","body"]` para TODO payload válido).

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi import status as http_status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import client_ip, get_current_user, require_role
from app.application.services.access_request_service import (
    AccessRequestEmailTaken,
    AccessRequestGoogleIdentityTaken,
    AccessRequestInput,
    AccessRequestInvalidTransition,
    AccessRequestNotApprovable,
    AccessRequestNotFound,
    AccessRequestService,
)
from app.application.services.contact_lead_service import hash_ip
from app.application.services.google_oauth_service import (
    consume_google_prefill,
    read_google_prefill,
)
from app.domain.access_request import AccessRequestStatus, RequestedPlan
from app.domain.contact_lead import normalize_email
from app.main import limiter
from app.observability.logger import get_logger
from app.persistence.db.redis_client import get_redis
from app.persistence.db.session import get_db_session
from app.persistence.models.user import User
from app.schemas.access_request import (
    AccessRequestAcceptedResponse,
    AccessRequestAdminItem,
    ApproveAccessRequest,
    ApproveAccessRequestResponse,
    CreateAccessRequestRequest,
    RejectAccessRequest,
    ResendAccessRequestRequest,
    VerifiedAccessRequestResponse,
    VerifyAccessRequestRequest,
    WaitlistAccessRequest,
)
from app.schemas.common import MessageResponse, PaginatedResponse
from app.schemas.oauth import GooglePrefillResponse

router = APIRouter()
admin_router = APIRouter()
logger = get_logger(__name__)

#: Cuerpo único del alta pública. Instancia constante para que sea imposible que
#: una rama del endpoint devuelva un mensaje distinto de otra (ver neutralidad).
_ACEPTADA = AccessRequestAcceptedResponse()

#: Respuesta genérica del reenvío. Igual exista o no una solicitud con ese email.
_REENVIO_OK = MessageResponse(
    message=(
        "Si hay una solicitud pendiente de confirmar con ese email, "
        "te reenviamos el link."
    )
)

#: Quién revisó. `'api'` para las decisiones tomadas por HTTP; el script de
#: consola registra `'script'`.
_VIA_API = "api"


# ── Formulario público ────────────────────────────────────────────────────────


async def _resolver_google_subject(
    redis: Redis, token: str | None, email: str
) -> str | None:
    """Canjea el token de prefill por el `google_subject` de la identidad.

    **Acá se consume el token** (GETDEL): el `GET /prefill/{token}` que hizo el
    formulario para mostrar el email es una lectura, la única toma es esta.

    Tres desenlaces:

    * Sin token → `None`. Es el alta normal del formulario público.
    * Token vencido o ya canjeado → `None` **y la solicitud se manda igual**.
      El TTL del prefill es de 10 minutos y este formulario es largo: abortar el
      envío por eso tiraría a la basura todo lo que el visitante contestó, y no
      hay nada que proteger — sin subject la solicitud sigue siendo válida, el
      usuario aprobado simplemente entra con el link de contraseña. Lo que NO se
      hace es inventar un subject.
    * Token válido pero de OTRO email → 403. Es el mismo agujero que tapa
      `complete_link` con `email_mismatch`: sin este chequeo, quien controla una
      cuenta de Google podría ligar su identidad a una solicitud hecha con el
      email de otra persona.
    """
    if not token:
        return None

    prefill = await consume_google_prefill(redis, token)
    if prefill is None:
        logger.warning("access_request.google_prefill_expired")
        return None

    if prefill.email != normalize_email(email):
        logger.warning("access_request.google_prefill_email_mismatch")
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail="google_prefill_email_mismatch",
        )
    return prefill.provider_subject


@router.post(
    "",
    response_model=AccessRequestAcceptedResponse,
    status_code=http_status.HTTP_201_CREATED,
    summary="Enviar una solicitud de acceso (formulario público)",
)
@limiter.limit("5/hour")
async def create_access_request(
    request: Request,
    body: CreateAccessRequestRequest,
    session: AsyncSession = Depends(get_db_session),
    redis: Redis = Depends(get_redis),
) -> AccessRequestAcceptedResponse:
    """Recibe una solicitud. **No crea ninguna cuenta.**

    Ni Tenant, ni User, ni Subscription, ni BusinessProfile, ni MomentumProfile:
    esas cinco filas las acuña recién la aprobación manual. Lo único que se
    persiste acá es la solicitud.

    Devuelve el MISMO cuerpo en todos los desenlaces —incluido "ese email ya
    tiene cuenta" y "el envío parece un bot"—, por neutralidad a enumeración.
    """
    google_subject = await _resolver_google_subject(
        redis, body.google_prefill_token, str(body.email)
    )
    servicio = AccessRequestService(session)
    _, desenlace = await servicio.create(
        AccessRequestInput(
            full_name=body.full_name,
            email=str(body.email),
            phone=body.phone,
            business_name=body.business_name,
            requested_vertical=body.requested_vertical.value,
            vertical_other_text=body.vertical_other_text,
            requested_plan=body.requested_plan.value,
            years_operating=body.years_operating.value,
            staff_size=body.staff_size.value,
            monthly_revenue_band=body.monthly_revenue_band.value,
            main_concern=body.main_concern,
            records_format=body.records_format.value,
            history_depth=body.history_depth.value,
            can_share_files=body.can_share_files.value,
            records_notes=body.records_notes,
            applicant_notes=body.applicant_notes,
            cta_source=body.cta_source,
            # Sale del prefill de "Continuar con Google" (ver
            # `_resolver_google_subject`): `None` cuando el alta es del
            # formulario público a secas o cuando el token venció.
            google_subject=google_subject,
            consent_version=body.consent_version,
            website=body.website,
            elapsed_ms=body.elapsed_ms,
        ),
        ip_hash=hash_ip(client_ip(request)),
    )
    # El desenlace se loguea, nunca se responde.
    logger.info("access_request.submit", outcome=desenlace.value)
    return _ACEPTADA


@router.post(
    "/verify",
    response_model=VerifiedAccessRequestResponse,
    summary="Confirmar el email de una solicitud (doble opt-in)",
)
@limiter.limit("10/5minutes")
async def verify_access_request(
    request: Request,
    body: VerifyAccessRequestRequest,
    session: AsyncSession = Depends(get_db_session),
) -> VerifiedAccessRequestResponse:
    """Consume el token del mail. **POST y no GET**: los escáneres de correo y los
    prefetchers de links hacen GET y consumirían el token antes que el usuario.

    Idempotente: entrar dos veces con el mismo token (doble click en el mail)
    responde 200 las dos veces. Un token inexistente, vencido sin usar o basura
    responde 400 `token_invalido_o_expirado` (lo levanta el servicio).
    """
    solicitud = await AccessRequestService(session).verify(body.token)
    return VerifiedAccessRequestResponse(
        requested_plan=RequestedPlan(solicitud.requested_plan)
    )


@router.post(
    "/resend",
    response_model=MessageResponse,
    summary="Reenviar el mail de confirmación de una solicitud",
)
@limiter.limit("3/15minutes")
async def resend_access_request_verification(
    request: Request,
    body: ResendAccessRequestRequest,
    session: AsyncSession = Depends(get_db_session),
) -> MessageResponse:
    """Siempre 200 con el mismo mensaje.

    No distingue "no hay solicitud", "ya está verificada" ni "todavía corre el
    cooldown": cualquiera de esas señales convertiría el endpoint en un oráculo de
    "este email pidió acceso".
    """
    await AccessRequestService(session).resend_verification(str(body.email))
    return _REENVIO_OK


@router.get(
    "/prefill/{token}",
    response_model=GooglePrefillResponse,
    summary="Datos de la identidad de Google para prellenar el formulario",
)
@limiter.limit("10/5minutes")
async def get_google_prefill(
    request: Request,
    token: str,
    redis: Redis = Depends(get_redis),
) -> GooglePrefillResponse:
    """Devuelve el email (y el nombre, si Google lo mandó) de un prefill vigente.

    **No consume el token**: el formulario lo devuelve después en el POST, que es
    donde se resuelve el `google_subject`. Si esta lectura lo borrara, el linkeo
    con Google se perdería en silencio.

    Token inexistente, vencido o ya canjeado → 404. El frontend cae al
    formulario vacío: no hay nada que revelar ni que inventar.
    """
    prefill = await read_google_prefill(redis, token)
    if prefill is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="prefill_invalido_o_expirado",
        )
    return GooglePrefillResponse(
        email=prefill.email,
        full_name=prefill.full_name,
        provider=prefill.provider,
    )


# ── Cola de revisión (SUPERADMIN) ─────────────────────────────────────────────


async def _buscar(servicio: AccessRequestService, request_id: uuid.UUID) -> AccessRequestAdminItem:
    solicitud = await servicio.get(request_id)
    if solicitud is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="access_request_not_found",
        )
    return AccessRequestAdminItem.model_validate(solicitud)


@admin_router.get(
    "",
    response_model=PaginatedResponse[AccessRequestAdminItem],
    summary="Listar solicitudes de acceso (SUPERADMIN)",
    dependencies=[Depends(require_role("SUPERADMIN"))],
)
async def list_access_requests(
    status: AccessRequestStatus | None = Query(
        default=None,
        description="Sin filtro lista LA COLA (pending + waitlist), no todo el histórico.",
    ),
    requested_plan: RequestedPlan | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db_session),
) -> PaginatedResponse[AccessRequestAdminItem]:
    filas, total = await AccessRequestService(session).list_requests(
        status=status,
        requested_plan=requested_plan,
        limit=limit,
        offset=offset,
    )
    return PaginatedResponse[AccessRequestAdminItem](
        items=[AccessRequestAdminItem.model_validate(f) for f in filas],
        total=total,
        limit=limit,
        offset=offset,
    )


@admin_router.get(
    "/{request_id}",
    response_model=AccessRequestAdminItem,
    summary="Ver una solicitud de acceso (SUPERADMIN)",
    dependencies=[Depends(require_role("SUPERADMIN"))],
)
async def get_access_request(
    request_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
) -> AccessRequestAdminItem:
    return await _buscar(AccessRequestService(session), request_id)


@admin_router.post(
    "/{request_id}/approve",
    response_model=ApproveAccessRequestResponse,
    summary="Aprobar una solicitud y acuñar la cuenta (SUPERADMIN)",
    dependencies=[Depends(require_role("SUPERADMIN"))],
)
async def approve_access_request(
    request_id: uuid.UUID,
    body: ApproveAccessRequest,
    reviewer: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> ApproveAccessRequestResponse:
    """Acuña Tenant + User + Subscription + BusinessProfile + MomentumProfile.

    Idempotente: re-aprobar devuelve el tenant existente con
    `already_approved=true` y no acuña un segundo.

    Tres motivos distintos de 409, que el `detail` distingue (el email de la
    solicitud ya tiene cuenta, la identidad de Google ya está vinculada a otro
    usuario, o la solicitud no es aprobable — con estos dos submotivos):

    1. **Estado**: la solicitud no está en la cola (`unverified`, `rejected`,
       `expired`).
    2. **Email sin confirmar**: `email_verified_at is NULL`. Es un chequeo
       aparte y no redundante — una solicitud `unverified` se puede mandar a
       `waitlist` (triar spam sin esperar el click es legítimo), y `waitlist`
       SÍ es un estado aprobable, así que sin este segundo motivo se acuñaría
       una cuenta contra un email que nadie confirmó. Se sale del caso solo:
       cuando el usuario clickea el link, `verify()` sella `email_verified_at`
       aunque la solicitud ya esté en `waitlist`.
    """
    servicio = AccessRequestService(session)
    try:
        resultado = await servicio.approve(
            request_id,
            vertical=body.assigned_vertical,
            reviewer_user_id=reviewer.user_id,
            via=_VIA_API,
            notes=body.notes,
        )
    except AccessRequestNotFound:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="access_request_not_found",
        ) from None
    except (
        AccessRequestNotApprovable,
        AccessRequestEmailTaken,
        AccessRequestGoogleIdentityTaken,
    ) as exc:
        # Los tres son 409 y el `detail` (el mensaje de la excepción) es lo que
        # los distingue; ver el docstring.
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT, detail=str(exc)
        ) from None

    return ApproveAccessRequestResponse(
        request=AccessRequestAdminItem.model_validate(resultado.request),
        tenant_id=resultado.tenant_id,
        user_id=resultado.user_id,
        already_approved=resultado.already_approved,
    )


@admin_router.post(
    "/{request_id}/reject",
    response_model=AccessRequestAdminItem,
    summary="Rechazar una solicitud (SUPERADMIN)",
    dependencies=[Depends(require_role("SUPERADMIN"))],
)
async def reject_access_request(
    request_id: uuid.UUID,
    body: RejectAccessRequest,
    reviewer: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> AccessRequestAdminItem:
    servicio = AccessRequestService(session)
    try:
        solicitud = await servicio.reject(
            request_id,
            reviewer_user_id=reviewer.user_id,
            via=_VIA_API,
            reason=body.reason,
            notify=body.notify,
        )
    except AccessRequestNotFound:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="access_request_not_found",
        ) from None
    except AccessRequestInvalidTransition as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT, detail=str(exc)
        ) from None
    return AccessRequestAdminItem.model_validate(solicitud)


@admin_router.post(
    "/{request_id}/waitlist",
    response_model=AccessRequestAdminItem,
    summary="Dejar una solicitud en lista de espera (SUPERADMIN)",
    dependencies=[Depends(require_role("SUPERADMIN"))],
)
async def waitlist_access_request(
    request_id: uuid.UUID,
    body: WaitlistAccessRequest,
    reviewer: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> AccessRequestAdminItem:
    servicio = AccessRequestService(session)
    try:
        solicitud = await servicio.waitlist(
            request_id,
            reviewer_user_id=reviewer.user_id,
            via=_VIA_API,
            notes=body.notes,
        )
    except AccessRequestNotFound:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="access_request_not_found",
        ) from None
    except AccessRequestInvalidTransition as exc:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT, detail=str(exc)
        ) from None
    return AccessRequestAdminItem.model_validate(solicitud)
