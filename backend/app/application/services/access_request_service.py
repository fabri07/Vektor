"""Servicio de solicitudes de acceso (registro cerrado con aprobación manual).

El visitante ya no crea una cuenta: manda una **solicitud** que el dueño revisa a
mano. Este módulo concentra toda la máquina de estados de ese trámite —crear,
verificar el email, listar la cola, aprobar, rechazar, poner en lista de espera—
más los armadores (puros) de los emails del flujo.

Seis invariantes que este archivo sostiene y que un cambio no puede aflojar:

1. **Neutralidad a enumeración de cuentas.** ``create()`` devuelve el MISMO
   resultado exista o no una cuenta con ese email. ``POST /auth/register``
   respondía 409 *"An account with this email already exists"*, que es un oráculo
   de enumeración; este endpoint es público y sin auth, así que no puede repetir
   ese error. Cuando el email ya tiene cuenta no se persiste nada y se encola un
   mail "ya tenés cuenta, entrá acá".
2. **El claim del token de verificación es atómico**
   (``UPDATE ... SET used=true WHERE token_id=:t AND used=false`` + ``rowcount``),
   NO el read-then-write de ``AuthService.verify_email`` — que es una carrera.
3. **``approve()`` es idempotente y nunca acuña dos tenants**: ``FOR UPDATE`` +
   corto circuito si ya está aprobada. La doble aprobación pasa de verdad, porque
   el dueño puede aprobar desde el script de consola y desde la API.
4. **``requested_plan`` NO se copia a ``Subscription.plan_code``**: la intención
   se conserva para trazabilidad, la suscripción se crea FREE igual (ver el
   docstring de ``provision_tenant``).
5. **``onboarding_completed`` queda en ``False`` al aprobar**: los números
   financieros se piden en el primer login, no en el formulario público.
6. **``approve()`` vincula la identidad de Google si la solicitud la trae**
   (``google_subject``, que puebla el canje del prefill en
   ``api/v1/access_requests.py``). Sin ese ``UserAuthIdentity``, quien pidió
   acceso con "Continuar con Google" es aprobado y no puede entrar con Google.

**Regla cardinal** (documentada en ``app/jobs/contact_lead_worker.py``): se
persiste y se COMMITEA antes de intentar cualquier email. Encolar nunca puede
romper —ni revertir— una solicitud ya guardada.

Nada acá envía correo: los armadores son funciones puras (mismo lugar y forma que
``contact_lead_service.build_lead_email``) y el envío lo hace el worker Celery.

⚠️ Ninguna variable local de este módulo se llama ``html``: en
``contact_lead_service.build_lead_email`` una local con ese nombre shadowea el
import del módulo ``html`` y rompe la función entera con ``NameError``.
"""

from __future__ import annotations

import html
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from importlib import import_module
from typing import TYPE_CHECKING, Any, cast

from fastapi import HTTPException
from fastapi import status as http_status
from sqlalchemy import CursorResult, case, func, nulls_last, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services._savepoint import (
    SavepointConflictError,
    guarded_savepoint,
    unique_violation_classifier,
)
from app.application.services.auth_service import _INVITE_TOKEN_TTL_HOURS, AuthService
from app.application.services.tenant_provisioning import provision_tenant
from app.config.settings import get_settings
from app.domain.access_request import (
    ACCESS_REQUEST_TOKEN_TTL_HOURS,
    CONSENT_VERSION,
    OPEN_ACCESS_REQUEST_STATUSES,
    AccessRequestStatus,
    RequestedPlan,
)
from app.domain.contact_lead import (
    DEDUP_WINDOW_SECONDS,
    EmailNotificationStatus,
    looks_like_bot,
    normalize_ar_phone,
    normalize_email,
)
from app.domain.verticals import Vertical
from app.integrations.email_templates import render_action_email
from app.observability.logger import get_logger
from app.persistence.models.access_request import AccessRequest, AccessRequestToken
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.business import BusinessProfile
from app.persistence.models.user_auth_identity import UserAuthIdentity
from app.persistence.repositories.user_repository import UserRepository

if TYPE_CHECKING:  # pragma: no cover - solo para tipos
    from app.persistence.models.tenant import Tenant
    from app.persistence.models.user import User

logger = get_logger(__name__)

# ── Nombres de las tareas Celery del flujo (las implementa la Task 10) ────────
# Se referencian por nombre de atributo del módulo del worker para que este
# servicio no lo importe en import-time (y para poder encolar sin que exista aún).
TASK_VERIFICACION = "notify_access_request_verification"
TASK_AVISO_DUENIO = "notify_access_request_owner"
TASK_DECISION = "notify_access_request_decision"
TASK_CUENTA_EXISTENTE = "notify_access_request_account_exists"

# ── `decision_type` de cada decisión del flujo en `decision_audit_log` ───────
# Tres de las cuatro no tienen tenant (recién existe al aprobar); por eso la
# migración 20260806_0003 hizo `decision_audit_log.tenant_id` nullable.
DECISION_CREATED = "ACCESS_REQUEST_CREATED"
DECISION_VERIFIED = "ACCESS_REQUEST_VERIFIED"
DECISION_APPROVED = "ACCESS_REQUEST_APPROVED"
DECISION_REJECTED = "ACCESS_REQUEST_REJECTED"
DECISION_WAITLISTED = "ACCESS_REQUEST_WAITLISTED"
DECISION_EXPIRED = "ACCESS_REQUEST_EXPIRED"
DECISION_INVITE_RESENT = "ACCESS_REQUEST_INVITE_RESENT"

#: Único proveedor social del flujo hoy. Es el mismo literal que usa
#: ``google_oauth_service`` al buscar la identidad en el login.
_GOOGLE_PROVIDER = "google"

#: Los dos únicos estados que forman la COLA de revisión. Una solicitud
#: ``unverified`` —aunque sea Premium— no desplaza a una verificada: el doble
#: opt-in es justamente el filtro que evita que emails falsos consuman tiempo de
#: revisión.
ESTADOS_DE_LA_COLA: tuple[str, ...] = (
    AccessRequestStatus.PENDING.value,
    AccessRequestStatus.WAITLIST.value,
)

#: Desde qué estados se puede aprobar. ``unverified`` no está: aprobar sin doble
#: opt-in acuñaría una cuenta contra un email que nadie confirmó.
ESTADOS_APROBABLES: tuple[str, ...] = ESTADOS_DE_LA_COLA

#: Premium primero (0), el resto después (1). Es orden DERIVADO de
#: ``requested_plan``; no existe columna ``is_priority`` a propósito.
_PRIORIDAD_PREMIUM = case(
    (AccessRequest.requested_plan == RequestedPlan.PREMIUM.value, 0), else_=1
)

#: El índice único parcial "un solo trámite abierto por email" solo existe en
#: PostgreSQL (ver el docstring del modelo). Cuando dos envíos simultáneos lo
#: violan, el conflicto se traduce a "ya había una solicitud abierta".
_CONFLICTO_SOLICITUD_ABIERTA = unique_violation_classifier(
    "solicitud_abierta",
    constraint="uq_access_requests_open_email",
    columns=("access_requests.email",),
)

#: "Una identidad de Google pertenece a un solo usuario". A diferencia del de
#: arriba, este índice SÍ existe en los dos motores (lo declara el modelo), así
#: que la carrera es observable también en los tests.
_CONFLICTO_IDENTIDAD_GOOGLE = unique_violation_classifier(
    "identidad_google",
    constraint="uq_auth_identity_provider_subject",
    columns=("user_auth_identities.provider", "user_auth_identities.provider_subject"),
)


class CreateOutcome(StrEnum):
    """Qué pasó realmente al recibir una solicitud.

    El caller responde SIEMPRE lo mismo al visitante (neutralidad a enumeración);
    este valor es para decidir qué encolar y qué loguear, nunca para el cuerpo de
    la respuesta HTTP.
    """

    CREATED = "created"
    """Solicitud nueva persistida; se encoló el mail de verificación."""

    TOKEN_REISSUED = "token_reissued"
    """Ya había una ``unverified`` y venció el cooldown: token reemitido."""

    DUPLICATE_OPEN = "duplicate_open"
    """Ya hay un trámite abierto para ese email: idempotente, no se creó nada."""

    ACCOUNT_EXISTS = "account_exists"
    """El email ya tiene cuenta: no se persiste nada, se encola "entrá acá"."""

    BOT_DISCARDED = "bot_discarded"
    """Honeypot o envío demasiado rápido: se descarta en silencio."""


@dataclass(frozen=True)
class AccessRequestInput:
    """Datos ya validados por Pydantic, listos para persistir.

    Los campos de vocabulario cerrado viajan como ``str`` (el ``.value`` del enum
    validado en el schema), igual que ``contact_lead_service.LeadInput``. Los
    CHECK de la tabla son el backstop.
    """

    full_name: str
    email: str
    phone: str | None
    business_name: str
    requested_vertical: str
    vertical_other_text: str | None
    requested_plan: str
    years_operating: str
    staff_size: str
    monthly_revenue_band: str
    main_concern: str
    records_format: str
    history_depth: str
    can_share_files: str
    records_notes: str | None = None
    applicant_notes: str | None = None
    cta_source: str | None = None
    google_subject: str | None = None
    consent_version: str | None = None
    # Señales anti-bot del formulario (honeypot + tiempo de tipeo).
    website: str | None = None
    elapsed_ms: int | None = None


class ResendKind(StrEnum):
    """Qué mail reencoló ``resend_invite`` — depende del estado de la solicitud."""

    VERIFICATION = "verification"
    """Solicitud ``unverified``: se reemitió el token de doble opt-in."""

    INVITE = "invite"
    """Solicitud ``approved``: se acuñó una invitación nueva para la contraseña."""


@dataclass(frozen=True)
class ResendResult:
    """Resultado de reencolar el mail pendiente de una solicitud.

    ``enqueued=False`` con ``token_id=None`` significa que el cooldown de tokens
    frenó la reemisión: no es un error, es la protección contra el mail por click.
    """

    request: AccessRequest
    kind: ResendKind
    enqueued: bool
    token_id: uuid.UUID | None


@dataclass(frozen=True)
class ApprovalResult:
    """Resultado de aprobar una solicitud.

    ``tenant_id``/``user_id`` son opcionales porque las FK de la solicitud son
    ``ON DELETE SET NULL``: una solicitud aprobada cuyo tenant se borró después
    sigue existiendo, sin puntero. Devolverlos como opcionales es honesto; forzar
    un valor sería inventar.
    """

    request: AccessRequest
    tenant_id: uuid.UUID | None
    user_id: uuid.UUID | None
    invite_token_id: uuid.UUID | None
    already_approved: bool


# ── Errores de dominio del trámite ───────────────────────────────────────────


class AccessRequestError(Exception):
    """Base de los errores del flujo de solicitudes."""


# Los nombres SIN sufijo `Error` son deliberados: el plan del PR los fija así
# (`AccessRequestNotApprovable`, `AccessRequestEmailTaken`) y los routers de la
# Task 9 los importan por ese nombre. Se silencia N818 en el grupo entero para
# que las cuatro excepciones del flujo se lean parejo.
class AccessRequestNotFound(AccessRequestError):  # noqa: N818
    def __init__(self, request_id: uuid.UUID) -> None:
        super().__init__(f"No existe la solicitud {request_id}")
        self.request_id = request_id


class AccessRequestNotApprovable(AccessRequestError):  # noqa: N818
    """La solicitud no cumple alguna precondición de aprobación.

    Dos motivos: el estado no admite aprobación
    (``unverified``/``rejected``/``expired``) o nadie confirmó el email.
    """

    def __init__(self, current_status: str, *, motivo: str | None = None) -> None:
        detalle = motivo or f"aprobables: {', '.join(ESTADOS_APROBABLES)}"
        super().__init__(
            f"Una solicitud en estado '{current_status}' no se puede aprobar; {detalle}"
        )
        self.current_status = current_status
        self.motivo = motivo


class AccessRequestEmailTaken(AccessRequestError):  # noqa: N818
    """Alguien creó una cuenta con ese email entre el envío y la aprobación."""

    def __init__(self, email: str) -> None:
        super().__init__(f"Ya existe una cuenta con el email de la solicitud ({email})")
        self.email = email


class AccessRequestGoogleIdentityTaken(AccessRequestError):  # noqa: N818
    """La identidad de Google de la solicitud ya está vinculada a otro usuario.

    Pasa cuando la misma cuenta de Google abrió dos solicitudes con emails
    distintos y las dos se aprueban. Se levanta ANTES de acuñar nada: la
    alternativa —insertar y dejar que reviente la unique
    ``uq_auth_identity_provider_subject``— sería un 500 sin explicación, y
    saltear el linkeo en silencio dejaría al usuario aprobado sin poder entrar
    con Google sin que nadie se entere.
    """

    def __init__(self, provider_subject: str) -> None:
        super().__init__(
            "La identidad de Google de esta solicitud ya está vinculada a otra "
            f"cuenta (subject {provider_subject[:8]}…)"
        )
        self.provider_subject = provider_subject


class AccessRequestNotResendable(AccessRequestError):  # noqa: N818
    """No hay ningún mail que reencolar para esa solicitud.

    Solo dos estados tienen un mail pendiente que le sirva al destinatario:
    ``unverified`` (el link de doble opt-in) y ``approved`` (la invitación para
    definir la contraseña). Una ``pending``/``waitlist`` está esperando al dueño,
    no al solicitante; una ``rejected``/``expired`` no espera nada.
    """

    def __init__(self, current_status: str, *, motivo: str | None = None) -> None:
        detalle = motivo or (
            f"reencolables: {AccessRequestStatus.UNVERIFIED.value} (verificación) "
            f"y {AccessRequestStatus.APPROVED.value} (invitación)"
        )
        super().__init__(
            f"Una solicitud en estado '{current_status}' no tiene mail para "
            f"reencolar; {detalle}"
        )
        self.current_status = current_status
        self.motivo = motivo


class AccessRequestInvalidTransition(AccessRequestError):  # noqa: N818
    """Transición ilegal: rechazar/postergar algo ya aprobado, o postergar algo
    que todavía no confirmó el email."""

    def __init__(
        self, current_status: str, target_status: str, *, motivo: str | None = None
    ) -> None:
        detalle = f" ({motivo})" if motivo else ""
        super().__init__(
            f"No se puede pasar de '{current_status}' a '{target_status}'{detalle}"
        )
        self.current_status = current_status
        self.target_status = target_status
        self.motivo = motivo


def _encolar(nombre_tarea: str, *args: object) -> None:
    """Encola una notificación del flujo. **Nunca** propaga excepciones.

    El import del worker es perezoso y por nombre de atributo (mismo patrón que
    ``api/v1/contact.py``) por dos razones: el módulo se agrega en la Task 10, y
    encolar no puede romper una solicitud que ya está persistida y commiteada.
    Se usa ``import_module`` y no un ``from app.jobs import ...`` para no tener
    que dejar un ``type: ignore`` que la Task 10 tendría que acordarse de sacar.
    """
    try:
        worker = import_module("app.jobs.access_request_worker")

        tarea = getattr(worker, nombre_tarea)
        tarea.delay(*args)
    except Exception as exc:
        logger.warning(
            "access_request.enqueue_failed", tarea=nombre_tarea, error=str(exc)
        )


class AccessRequestService:
    """Toda la máquina de estados de una solicitud de acceso."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._user_repo = UserRepository(session)

    # ── Alta ──────────────────────────────────────────────────────────────────

    async def create(
        self, data: AccessRequestInput, *, ip_hash: str | None
    ) -> tuple[AccessRequest | None, CreateOutcome]:
        """Recibe una solicitud del formulario público.

        Devuelve ``(solicitud | None, outcome)``. El caller responde lo MISMO en
        todos los casos: el ``outcome`` no puede filtrarse al visitante, porque
        distinguir "creada" de "ese email ya tiene cuenta" sería exactamente el
        oráculo de enumeración que este flujo elimina.

        Orden obligatorio: normalizar → INSERT ``unverified`` → ``flush`` → token
        → ``commit`` → recién ahí encolar el mail (regla cardinal).
        """
        if looks_like_bot(website=data.website, elapsed_ms=data.elapsed_ms):
            # Al bot se le devuelve éxito sin persistir ni notificar.
            logger.info("access_request.spam_dropped")
            return None, CreateOutcome.BOT_DISCARDED

        email = normalize_email(data.email)
        telefono = normalize_ar_phone(data.phone) if data.phone else ""

        # El backend es autoritativo sobre la versión del consentimiento; si el
        # front declara otra, queda registrado (posible bundle desactualizado).
        if data.consent_version and data.consent_version != CONSENT_VERSION:
            logger.warning(
                "access_request.consent_version_mismatch",
                client_version=data.consent_version,
                server_version=CONSENT_VERSION,
            )

        if await self._user_repo.get_by_email_any_tenant(email) is not None:
            # NO se persiste nada y NO se revela nada: mismo resultado que un
            # alta exitosa. El mail que sí se manda ("ya tenés cuenta") solo lo
            # ve quien controla esa casilla.
            logger.info("access_request.account_exists")
            _encolar(TASK_CUENTA_EXISTENTE, email)
            return None, CreateOutcome.ACCOUNT_EXISTS

        abierta = await self._get_open_by_email(email)
        if abierta is not None:
            return await self._reintento_de_solicitud_abierta(
                abierta, data.google_subject
            )

        solicitud = AccessRequest(
            full_name=data.full_name.strip(),
            email=email,
            phone=telefono or None,
            business_name=data.business_name.strip(),
            requested_vertical=data.requested_vertical,
            vertical_other_text=data.vertical_other_text,
            requested_plan=data.requested_plan,
            years_operating=data.years_operating,
            staff_size=data.staff_size,
            monthly_revenue_band=data.monthly_revenue_band,
            main_concern=data.main_concern,
            records_format=data.records_format,
            history_depth=data.history_depth,
            can_share_files=data.can_share_files,
            records_notes=data.records_notes,
            applicant_notes=data.applicant_notes,
            status=AccessRequestStatus.UNVERIFIED.value,
            verification_email_status=EmailNotificationStatus.PENDING.value,
            owner_notification_status=EmailNotificationStatus.PENDING.value,
            decision_email_status=EmailNotificationStatus.PENDING.value,
            consent_version=CONSENT_VERSION,
            consent_accepted_at=datetime.now(UTC),
            ip_hash=ip_hash,
            cta_source=data.cta_source,
            google_subject=data.google_subject,
        )
        try:
            async with guarded_savepoint(self._session, _CONFLICTO_SOLICITUD_ABIERTA):
                # El `add` va DENTRO del savepoint: `begin_nested()` flushea antes
                # de crearlo, así que agregar afuera emitiría el INSERT afuera.
                self._session.add(solicitud)
        except SavepointConflictError as conflicto:
            # Carrera con otro envío del mismo email (solo posible en PostgreSQL,
            # donde vive el índice único parcial): la otra solicitud ganó.
            existente = await self._get_open_by_email(email)
            if existente is None:  # pragma: no cover - el índice garantiza que exista
                raise conflicto.original from conflicto
            # La solicitud que traía el `google_subject` se descarta, así que la
            # identidad hay que colgarla de la que ganó: si no, este camino
            # repite el drop silencioso que arreglamos en el reintento normal.
            await self._adoptar_identidad_google(existente, data.google_subject)
            return existente, CreateOutcome.DUPLICATE_OPEN

        token = await self._emitir_token(solicitud)
        self._auditar(
            solicitud,
            decision_type=DECISION_CREATED,
            triggered_by="public:access_request_form",
            extra={"cta_source": solicitud.cta_source},
        )
        # REGLA CARDINAL: la solicitud está a salvo ANTES de tocar el email.
        await self._session.commit()

        _encolar(TASK_VERIFICACION, str(solicitud.id), str(token.token_id))
        logger.info(
            "access_request.created",
            request_id=str(solicitud.id),
            requested_vertical=solicitud.requested_vertical,
            requested_plan=solicitud.requested_plan,
        )
        return solicitud, CreateOutcome.CREATED

    async def resend_verification(self, email: str) -> None:
        """Reemite el token de verificación de una solicitud ``unverified``.

        No-op silencioso si no hay solicitud abierta, si ya está verificada o si
        todavía corre el cooldown: el endpoint responde 200 genérico siempre, para
        no volverse un oráculo de "este email pidió acceso".
        """
        abierta = await self._get_open_by_email(normalize_email(email))
        if abierta is None or abierta.status != AccessRequestStatus.UNVERIFIED.value:
            return
        if await self._token_en_cooldown(abierta.id):
            logger.info("access_request.resend_cooldown", request_id=str(abierta.id))
            return

        token = await self._emitir_token(abierta)
        await self._session.commit()
        _encolar(TASK_VERIFICACION, str(abierta.id), str(token.token_id))
        logger.info("access_request.verification_resent", request_id=str(abierta.id))

    # ── Doble opt-in ──────────────────────────────────────────────────────────

    async def verify(self, token_str: str) -> AccessRequest:
        """Consume el token del mail y pasa la solicitud a ``pending``.

        El claim es ATÓMICO (``UPDATE ... WHERE used=false`` + ``rowcount``): con
        el read-then-write de ``AuthService.verify_email``, dos requests
        simultáneos consumirían el mismo token.

        Volver a entrar con un token ya usado sobre una solicitud ya verificada es
        **idempotente** (200): el doble click en el mail, los prefetchers de links
        y los escáneres de spam lo hacen todo el tiempo. ``email_verified_at`` no
        se pisa.
        """
        try:
            token_uuid = uuid.UUID(token_str)
        except ValueError:
            raise self._token_invalido() from None

        ahora = datetime.now(UTC)
        resultado = await self._session.execute(
            update(AccessRequestToken)
            .where(
                AccessRequestToken.token_id == token_uuid,
                AccessRequestToken.used.is_(False),
                AccessRequestToken.expires_at > ahora,
            )
            .values(used=True)
            # `synchronize_session=False` es obligatorio, no una optimización: con
            # la sincronización por defecto ("evaluate") SQLAlchemy re-evalúa el
            # WHERE en Python sobre los objetos de la sesión, y en SQLite los
            # DateTime vuelven SIN tzinfo → `TypeError: can't compare offset-naive
            # and offset-aware`. La condición la resuelve el motor; acá solo nos
            # importa el rowcount.
            .execution_options(synchronize_session=False)
        )
        if cast("CursorResult[Any]", resultado).rowcount != 1:
            return await self._verify_idempotente(token_uuid)

        token = await self._session.get(AccessRequestToken, token_uuid)
        if token is None:  # pragma: no cover - lo acabamos de actualizar
            raise self._token_invalido()
        solicitud = await self._session.get(AccessRequest, token.access_request_id)
        if solicitud is None:  # pragma: no cover - FK ON DELETE CASCADE
            raise self._token_invalido()

        if solicitud.status != AccessRequestStatus.UNVERIFIED.value:
            # Token válido y NUNCA usado sobre una solicitud ya revisada: el
            # doble opt-in ocurrió igual —el usuario recibió el mail y clickeó—,
            # lo único distinto es que el dueño ya la había triado. Pasa de
            # verdad: `reject()` acepta una `unverified` (descartar spam sin
            # esperar el click es legítimo y útil). `waitlist()` ya NO, justo
            # porque postergar sin verificar dejaba la solicitud sin salida.
            #
            # Por eso se sella `email_verified_at`: registra un hecho sobre el
            # EMAIL, no sobre la cola de revisión. Sin esto una `rejected` que
            # después confirma quedaría con el hecho perdido, el segundo click
            # daría 400 en `_verify_idempotente` a un usuario que sí confirmó, y
            # si el dueño corrigiera el rechazo con un `waitlist` la solicitud ya
            # no sería aprobable nunca.
            #
            # NO se re-abre el estado (sigue en waitlist/rejected) ni se avisa al
            # dueño: ya revisó esta solicitud, un aviso sería ruido.
            if solicitud.email_verified_at is None:
                solicitud.email_verified_at = ahora
                self._auditar(
                    solicitud,
                    decision_type=DECISION_VERIFIED,
                    triggered_by="public:access_request_verify",
                    extra={
                        "token_id": str(token_uuid),
                        "verificada_despues_de_revisar": True,
                    },
                )
                logger.info(
                    "access_request.verified_after_review",
                    request_id=str(solicitud.id),
                    status=solicitud.status,
                )
            await self._session.commit()
            return solicitud

        solicitud.status = AccessRequestStatus.PENDING.value
        if solicitud.email_verified_at is None:
            solicitud.email_verified_at = ahora
        self._auditar(
            solicitud,
            decision_type=DECISION_VERIFIED,
            triggered_by="public:access_request_verify",
            extra={"token_id": str(token_uuid)},
        )
        await self._session.commit()

        _encolar(TASK_AVISO_DUENIO, str(solicitud.id))
        logger.info("access_request.verified", request_id=str(solicitud.id))
        return solicitud

    # ── Cola de revisión ──────────────────────────────────────────────────────

    async def list_requests(
        self,
        *,
        status: AccessRequestStatus | None = None,
        requested_plan: RequestedPlan | None = None,
        priority_first: bool = True,
        limit: int,
        offset: int,
    ) -> tuple[list[AccessRequest], int]:
        """Lista solicitudes y devuelve ``(página, total)``.

        Sin ``status``, lista LA COLA: solo ``pending`` y ``waitlist``. El orden
        por defecto es ``premium`` primero y después la verificada más antigua
        (``email_verified_at ASC, created_at ASC``), con los ``NULL`` al final
        para que el resultado no dependa del motor (PostgreSQL los pone últimos,
        SQLite primero).
        """
        condiciones = [
            AccessRequest.status == status.value
            if status is not None
            else AccessRequest.status.in_(ESTADOS_DE_LA_COLA)
        ]
        if requested_plan is not None:
            condiciones.append(AccessRequest.requested_plan == requested_plan.value)

        consulta = select(AccessRequest).where(*condiciones)
        if priority_first:
            consulta = consulta.order_by(
                _PRIORIDAD_PREMIUM,
                nulls_last(AccessRequest.email_verified_at.asc()),
                AccessRequest.created_at.asc(),
            )
        else:
            consulta = consulta.order_by(AccessRequest.created_at.desc())

        filas = (
            (await self._session.execute(consulta.limit(limit).offset(offset)))
            .scalars()
            .all()
        )
        total = (
            await self._session.execute(
                select(func.count()).select_from(AccessRequest).where(*condiciones)
            )
        ).scalar_one()
        return list(filas), int(total)

    async def get(self, request_id: uuid.UUID) -> AccessRequest | None:
        return (
            await self._session.execute(
                select(AccessRequest).where(AccessRequest.id == request_id)
            )
        ).scalar_one_or_none()

    # ── Decisiones del dueño ──────────────────────────────────────────────────

    async def approve(
        self,
        request_id: uuid.UUID,
        *,
        vertical: Vertical,
        reviewer_user_id: uuid.UUID | None,
        via: str,
        notes: str | None,
    ) -> ApprovalResult:
        """Aprueba la solicitud y acuña la cuenta. Todo en una transacción.

        Es **idempotente**: si ya estaba aprobada devuelve el tenant existente con
        ``already_approved=True``. Nunca acuña un segundo tenant — la doble
        aprobación va a pasar de verdad, porque el dueño puede aprobar desde el
        script de consola y desde la API.

        El ``vertical`` es el que asigna el DUEÑO, no el que declaró el
        solicitante: ese es todo el punto de la revisión manual.
        """
        solicitud = await self._lock(request_id)

        if solicitud.status == AccessRequestStatus.APPROVED.value:
            logger.info("access_request.approve.already", request_id=str(request_id))
            return ApprovalResult(
                request=solicitud,
                tenant_id=solicitud.approved_tenant_id,
                user_id=solicitud.approved_user_id,
                invite_token_id=None,
                already_approved=True,
            )
        if solicitud.status not in ESTADOS_APROBABLES:
            raise AccessRequestNotApprovable(solicitud.status)
        # El estado NO alcanza como prueba del doble opt-in, y esta guardia se
        # queda aunque hoy `waitlist()` también exija `email_verified_at`: la
        # invariante es "no se acuña una cuenta contra un email que nadie
        # confirmó", y depender de que ningún otro método deje pasar una
        # `unverified` a un estado aprobable es exactamente el acoplamiento que
        # hizo falta cerrar acá la primera vez. La condición que importa es
        # esta, no el estado.
        if solicitud.email_verified_at is None:
            raise AccessRequestNotApprovable(
                solicitud.status,
                motivo="nadie confirmó el email (doble opt-in pendiente)",
            )

        # Alguien pudo crear la cuenta a mano entre el envío y la aprobación.
        if await self._user_repo.get_by_email_any_tenant(solicitud.email) is not None:
            raise AccessRequestEmailTaken(solicitud.email)

        # Y si la solicitud vino por "Continuar con Google", esa identidad tiene
        # que estar libre ANTES de acuñar nada (ver el docstring de la excepción).
        if solicitud.google_subject is not None:
            await self._exigir_identidad_google_libre(solicitud.google_subject)

        if solicitud.requested_vertical != vertical.value:
            # No es un error: el dueño puede corregir legítimamente el rubro
            # declarado (y 'otros' SIEMPRE termina corregido acá).
            logger.info(
                "access_request.vertical_reassigned",
                request_id=str(request_id),
                requested_vertical=solicitud.requested_vertical,
                assigned_vertical=vertical.value,
            )

        tenant, user = await self._provision(solicitud, vertical)
        await self._sellar_screening_en_el_perfil(solicitud, tenant)
        await self._vincular_identidad_google(solicitud, tenant, user)

        ahora = datetime.now(UTC)
        solicitud.status = AccessRequestStatus.APPROVED.value
        solicitud.assigned_vertical_code = vertical.value
        solicitud.approved_tenant_id = tenant.tenant_id
        solicitud.approved_user_id = user.user_id
        solicitud.reviewed_at = ahora
        solicitud.reviewed_by_user_id = reviewer_user_id
        solicitud.reviewed_via = via
        solicitud.review_notes = notes

        # Link de invitación: el usuario define su contraseña con un
        # PasswordResetToken de TTL largo (puede ser aprobado mientras duerme).
        # Se reusa el helper de AuthService —privado, pero la Task 7 parametrizó
        # su TTL justamente para este flujo— en vez de escribir un segundo lugar
        # que cree tokens de reset (y que se olvide de invalidar los vigentes).
        invitacion = await AuthService(self._session)._create_password_reset_token(
            user.user_id, ttl_hours=_INVITE_TOKEN_TTL_HOURS
        )
        # Única decisión del flujo que SÍ tiene tenant: lo acuñamos recién, en
        # esta misma transacción.
        self._auditar(
            solicitud,
            decision_type=DECISION_APPROVED,
            triggered_by=f"{via}:access_request_review",
            tenant_id=tenant.tenant_id,
            actor_user_id=reviewer_user_id,
            extra={
                "assigned_vertical_code": vertical.value,
                "approved_tenant_id": str(tenant.tenant_id),
                "approved_user_id": str(user.user_id),
                "review_notes": notes,
                "google_identity_linked": solicitud.google_subject is not None,
            },
        )
        await self._session.commit()

        _encolar(
            TASK_DECISION,
            str(solicitud.id),
            AccessRequestStatus.APPROVED.value,
            str(invitacion.token_id),
        )
        logger.info(
            "access_request.approved",
            request_id=str(request_id),
            tenant_id=str(tenant.tenant_id),
            assigned_vertical=vertical.value,
            requested_plan=solicitud.requested_plan,
            via=via,
        )
        return ApprovalResult(
            request=solicitud,
            tenant_id=tenant.tenant_id,
            user_id=user.user_id,
            invite_token_id=invitacion.token_id,
            already_approved=False,
        )

    async def reject(
        self,
        request_id: uuid.UUID,
        *,
        reviewer_user_id: uuid.UUID | None,
        via: str,
        reason: str,
        notify: bool = True,
    ) -> AccessRequest:
        """Rechaza la solicitud. ``reason`` es obligatorio (mínimo 3 caracteres).

        ``notify=False`` existe porque a veces se descarta spam sin escribirle de
        vuelta. Repetir el rechazo es idempotente y no re-notifica.

        **Postura de consentimiento — la tercera del módulo, y es deliberada.**
        Conviven tres reglas distintas y conviene que se lean juntas:

        - ``expire_stale`` nunca escribe: nadie confirmó esa casilla.
        - ``waitlist`` se niega a actuar sin ``email_verified_at``.
        - ``reject`` **sí** notifica por default aunque el email no esté
          verificado.

        La tercera no es un olvido. El daño es de otra clase: ``waitlist``
        escribía *y* dejaba el trámite trabado, mientras que ``reject`` manda un
        único mail transaccional a una dirección que la persona tipeó ella misma
        y **no** bloquea un reintento del formulario (``rejected`` no está en
        ``OPEN_ACCESS_REQUEST_STATUSES``). Cambiar este default es una decisión
        de producto sobre una operación que ya se usa por CLI, no un arreglo; si
        alguna vez se quiere una regla única, el cambio es gatear este encolado
        por ``email_verified_at`` igual que ``waitlist``.
        """
        motivo = reason.strip()
        if len(motivo) < 3:
            raise ValueError("El motivo del rechazo es obligatorio (mínimo 3 caracteres)")

        solicitud = await self._lock(request_id)
        if solicitud.status == AccessRequestStatus.REJECTED.value:
            return solicitud
        self._exigir_no_aprobada(solicitud, AccessRequestStatus.REJECTED)

        solicitud.status = AccessRequestStatus.REJECTED.value
        solicitud.rejection_reason = motivo
        self._sellar_revision(solicitud, reviewer_user_id, via, notes=None)
        self._auditar(
            solicitud,
            decision_type=DECISION_REJECTED,
            triggered_by=f"{via}:access_request_review",
            actor_user_id=reviewer_user_id,
            extra={"rejection_reason": motivo, "notify": notify},
        )
        await self._session.commit()

        if notify:
            _encolar(
                TASK_DECISION,
                str(solicitud.id),
                AccessRequestStatus.REJECTED.value,
                None,
            )
        logger.info(
            "access_request.rejected",
            request_id=str(request_id),
            via=via,
            notify=notify,
        )
        return solicitud

    async def waitlist(
        self,
        request_id: uuid.UUID,
        *,
        reviewer_user_id: uuid.UUID | None,
        via: str,
        notes: str | None,
    ) -> AccessRequest:
        """Posterga la solicitud. **No es terminal**: después se puede aprobar o rechazar.

        Dos precondiciones, y las dos son la misma decisión vista de dos lados.

        1. El origen no puede ser ``approved`` (ya acuñó un tenant). Postergar
           algo ``rejected`` o ``expired`` sí se permite: es una corrección
           manual del dueño —el único que puede llamar acá— y vuelve a abrir el
           trámite en vez de obligar al solicitante a completar el formulario de
           nuevo.
        2. **El email tiene que estar confirmado** (misma condición que
           ``approve``, y por el mismo motivo). Antes se aceptaba una
           ``unverified``, y eso rompía dos cosas a la vez:

           * *Consentimiento.* Postergar encola el mail de decisión, así que se
             le escribía a una casilla que nadie confirmó — exactamente lo que
             ``expire_stale`` se niega a hacer 70 líneas más abajo. Las dos
             reglas no podían ser ciertas al mismo tiempo.
           * *Recuperabilidad.* ``waitlist`` cuenta como trámite ABIERTO
             (``OPEN_ACCESS_REQUEST_STATUSES``), pero ``approve`` exige el doble
             opt-in, ``resend_invite`` no la atiende y el reenvío del formulario
             solo reemite token desde ``unverified``. Una waitlist sin verificar
             cuyo token de 48 h venciera quedaba sin ninguna salida, y el
             visitante recibía el 201 genérico "te mandamos un correo" para
             siempre sin que llegara nada. Desde ``rejected``-sin-verificar
             encima empeoraba: ``rejected`` no bloquea un trámite nuevo y
             ``waitlist`` sí.

        Para triar spam **antes** del click sigue estando ``reject(notify=False)``,
        que es la herramienta correcta: no manda mail y no bloquea un reintento
        legítimo.
        """
        solicitud = await self._lock(request_id)
        if solicitud.status == AccessRequestStatus.WAITLIST.value:
            return solicitud
        self._exigir_no_aprobada(solicitud, AccessRequestStatus.WAITLIST)
        if solicitud.email_verified_at is None:
            raise AccessRequestInvalidTransition(
                solicitud.status,
                AccessRequestStatus.WAITLIST.value,
                motivo="nadie confirmó el email (doble opt-in pendiente)",
            )

        solicitud.status = AccessRequestStatus.WAITLIST.value
        self._sellar_revision(solicitud, reviewer_user_id, via, notes=notes)
        self._auditar(
            solicitud,
            decision_type=DECISION_WAITLISTED,
            triggered_by=f"{via}:access_request_review",
            actor_user_id=reviewer_user_id,
            extra={"review_notes": notes},
        )
        await self._session.commit()

        _encolar(
            TASK_DECISION, str(solicitud.id), AccessRequestStatus.WAITLIST.value, None
        )
        logger.info("access_request.waitlisted", request_id=str(request_id), via=via)
        return solicitud

    # ── Operación: reenvío de mails y limpieza de la cola ─────────────────────

    async def resend_invite(self, request_id: uuid.UUID, *, via: str) -> ResendResult:
        """Reencola el mail que el SOLICITANTE está esperando, según su estado.

        Existe porque sin panel de administración un mail perdido no tiene
        reintento manual por ningún otro lado:

        * ``unverified`` → se reemite el token de doble opt-in. Respeta el
          cooldown (``enqueued=False`` si todavía corre): un click no puede
          disparar un mail.
        * ``approved`` → se acuña una invitación NUEVA. Es el caso caro: la
          cuenta ya existe pero el usuario nunca pudo entrar —el mail quedó en
          ``failed`` o el link venció— y no tiene forma de darse cuenta solo
          (``/auth/register`` responde 410 y él ni sabe que fue aprobado, así que
          no se le va a ocurrir usar "olvidé mi contraseña").

        NO cambia el estado de la solicitud ni acuña un segundo tenant: la
        invitación sale del MISMO helper que usa ``approve()``
        (``_create_password_reset_token`` con el TTL largo), que además invalida
        los tokens de reset vigentes del usuario.
        """
        solicitud = await self._lock(request_id)

        if solicitud.status == AccessRequestStatus.UNVERIFIED.value:
            return await self._reencolar_verificacion(solicitud)
        if solicitud.status == AccessRequestStatus.APPROVED.value:
            return await self._reencolar_invitacion(solicitud, via)
        raise AccessRequestNotResendable(solicitud.status)

    async def find_stale(self, *, older_than_days: int) -> list[AccessRequest]:
        """Las ``unverified`` más viejas que ``older_than_days`` días. **No escribe.**

        Es el preview de ``expire_stale``: el dry-run del script lista con esto
        exactamente las filas que se van a tocar.
        """
        return list(
            (
                await self._session.execute(
                    select(AccessRequest)
                    .where(*self._condicion_rancia(older_than_days))
                    .order_by(AccessRequest.created_at.asc())
                )
            )
            .scalars()
            .all()
        )

    async def expire_stale(
        self, *, older_than_days: int, via: str
    ) -> list[AccessRequest]:
        """Pasa a ``expired`` las ``unverified`` que nadie confirmó hace mucho.

        **Es higiene de la cola, no rescate de nadie.** Una ``unverified`` vieja
        NO deja trabado a su dueño: si vuelve a mandar el formulario, ``create()``
        deriva a ``_reintento_de_solicitud_abierta`` y —vencido el cooldown— le
        reemite el token. Lo único que resuelve expirarlas es que dejen de
        acumularse para siempre ensuciando el listado de ``unverified`` y
        ocupando el índice único parcial sin nadie del otro lado.

        Solo toca ``unverified``: ``pending`` y ``waitlist`` son la cola de
        revisión del dueño, y ``approved``/``rejected`` ya son terminales.

        No manda ningún mail: nadie confirmó esa casilla, así que escribirle
        sería mandar correo a una dirección sin consentimiento verificado.

        Tampoco sella ``reviewed_at``/``reviewed_by_user_id``/``reviewed_via``:
        expirar no es una revisión, y marcarlo como tal haría parecer que el
        dueño triageó la solicitud. El origen queda en el ``triggered_by`` de la
        auditoría.
        """
        candidatas = (
            (
                await self._session.execute(
                    select(AccessRequest)
                    .where(*self._condicion_rancia(older_than_days))
                    .order_by(AccessRequest.created_at.asc())
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        for solicitud in candidatas:
            solicitud.status = AccessRequestStatus.EXPIRED.value
            self._auditar(
                solicitud,
                decision_type=DECISION_EXPIRED,
                triggered_by=f"{via}:access_request_expire_stale",
                extra={"older_than_days": older_than_days},
            )
        await self._session.commit()

        logger.info(
            "access_request.expired_stale",
            total=len(candidatas),
            older_than_days=older_than_days,
            via=via,
        )
        return list(candidatas)

    # ── Helpers privados ──────────────────────────────────────────────────────

    @staticmethod
    def _condicion_rancia(older_than_days: int) -> tuple[Any, ...]:
        """Predicado único de "``unverified`` rancia": preview y escritura comparten esto.

        La comparación de fechas se resuelve EN SQL (mismo motivo que
        ``_token_en_cooldown``: SQLite devuelve los ``DateTime`` sin tzinfo y
        compararlos en Python contra un aware explota).
        """
        if older_than_days < 1:
            raise ValueError("older_than_days tiene que ser al menos 1")
        corte = datetime.now(UTC) - timedelta(days=older_than_days)
        return (
            AccessRequest.status == AccessRequestStatus.UNVERIFIED.value,
            AccessRequest.created_at < corte,
        )

    async def _reencolar_verificacion(self, solicitud: AccessRequest) -> ResendResult:
        """Reemite el token de doble opt-in de una ``unverified`` (respeta el cooldown).

        No se audita, igual que ``resend_verification``: no hay decisión ni
        transición de estado, solo un mail que se repite.
        """
        if await self._token_en_cooldown(solicitud.id):
            logger.info("access_request.resend_cooldown", request_id=str(solicitud.id))
            return ResendResult(
                request=solicitud,
                kind=ResendKind.VERIFICATION,
                enqueued=False,
                token_id=None,
            )

        token = await self._emitir_token(solicitud)
        await self._session.commit()
        _encolar(TASK_VERIFICACION, str(solicitud.id), str(token.token_id))
        logger.info("access_request.verification_resent", request_id=str(solicitud.id))
        return ResendResult(
            request=solicitud,
            kind=ResendKind.VERIFICATION,
            enqueued=True,
            token_id=token.token_id,
        )

    async def _reencolar_invitacion(
        self, solicitud: AccessRequest, via: str
    ) -> ResendResult:
        """Acuña una invitación nueva para una solicitud ya aprobada."""
        if solicitud.approved_user_id is None:
            # FK ON DELETE SET NULL: el usuario que se había acuñado ya no existe.
            # No hay a quién invitar, y elegir otro sería inventar.
            raise AccessRequestNotResendable(
                solicitud.status,
                motivo="la solicitud aprobada ya no apunta a ningún usuario",
            )

        invitacion = await AuthService(self._session)._create_password_reset_token(
            solicitud.approved_user_id, ttl_hours=_INVITE_TOKEN_TTL_HOURS
        )
        # Vuelve a `pending` para que `--email-failed` deje de listarla: el
        # reintento está en curso y el worker la marcará `sent` o `failed`.
        solicitud.decision_email_status = EmailNotificationStatus.PENDING.value
        self._auditar(
            solicitud,
            decision_type=DECISION_INVITE_RESENT,
            triggered_by=f"{via}:access_request_resend_invite",
            tenant_id=solicitud.approved_tenant_id,
            extra={"approved_user_id": str(solicitud.approved_user_id)},
        )
        await self._session.commit()

        _encolar(
            TASK_DECISION,
            str(solicitud.id),
            AccessRequestStatus.APPROVED.value,
            str(invitacion.token_id),
        )
        logger.info(
            "access_request.invite_resent", request_id=str(solicitud.id), via=via
        )
        return ResendResult(
            request=solicitud,
            kind=ResendKind.INVITE,
            enqueued=True,
            token_id=invitacion.token_id,
        )

    def _auditar(
        self,
        solicitud: AccessRequest,
        *,
        decision_type: str,
        triggered_by: str,
        tenant_id: uuid.UUID | None = None,
        actor_user_id: uuid.UUID | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Agrega la traza a ``decision_audit_log`` DENTRO de la transacción.

        Se hace ``add`` y NO ``commit``: la auditoría tiene que viajar en la
        misma transacción que la decisión que audita. Si se persistiera aparte,
        el caso que importa —la decisión se guarda y la traza no— sería posible.

        ``tenant_id`` es ``None`` en tres de las cuatro decisiones porque el
        tenant recién existe al aprobar (ver la migración ``20260806_0003``).
        """
        self._session.add(
            DecisionAuditLog(
                tenant_id=tenant_id,
                decision_type=decision_type,
                decision_data={
                    "access_request_id": str(solicitud.id),
                    "email": solicitud.email,
                    "business_name": solicitud.business_name,
                    "requested_vertical": solicitud.requested_vertical,
                    "requested_plan": solicitud.requested_plan,
                    "status": solicitud.status,
                    **(extra or {}),
                },
                triggered_by=triggered_by,
                actor_user_id=actor_user_id,
                context={"endpoint": "access_requests"},
                created_at=datetime.now(UTC),
            )
        )

    async def _get_open_by_email(self, email: str) -> AccessRequest | None:
        """La solicitud ABIERTA (unverified/pending/waitlist) de ese email, si hay.

        Una ``rejected`` previa NO cuenta: los negocios cambian y se permite
        volver a pedir acceso.
        """
        return (
            (
                await self._session.execute(
                    select(AccessRequest)
                    .where(
                        AccessRequest.email == email,
                        AccessRequest.status.in_(tuple(OPEN_ACCESS_REQUEST_STATUSES)),
                    )
                    .order_by(AccessRequest.created_at.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )

    async def _reintento_de_solicitud_abierta(
        self, abierta: AccessRequest, google_subject: str | None
    ) -> tuple[AccessRequest, CreateOutcome]:
        """Reenvío del formulario con un trámite ya abierto: nunca crea otra fila.

        Sí **adopta** la identidad de Google si el reenvío la trae y la solicitud
        abierta no la tenía (ver ``_adoptar_identidad_google``): esta es la única
        oportunidad de ligarla, porque el token de prefill se consume igual antes
        de llegar acá.
        """
        await self._adoptar_identidad_google(abierta, google_subject)

        if abierta.status == AccessRequestStatus.UNVERIFIED.value and not (
            await self._token_en_cooldown(abierta.id)
        ):
            token = await self._emitir_token(abierta)
            await self._session.commit()
            _encolar(TASK_VERIFICACION, str(abierta.id), str(token.token_id))
            logger.info(
                "access_request.token_reissued", request_id=str(abierta.id)
            )
            return abierta, CreateOutcome.TOKEN_REISSUED

        logger.info("access_request.duplicate_open", request_id=str(abierta.id))
        return abierta, CreateOutcome.DUPLICATE_OPEN

    async def _adoptar_identidad_google(
        self, abierta: AccessRequest, google_subject: str | None
    ) -> None:
        """Cuelga la identidad de Google de la solicitud que YA estaba abierta.

        El escenario es común, no teórico: el visitante manda el formulario
        público y después prueba "Continuar con Google" creyendo que es un login.
        El token de prefill se consume igual en el router —antes de saber qué
        camino va a tomar ``create()``—, así que si acá no se adopta el subject,
        se quema el token y la identidad se pierde **en silencio**: al aprobar,
        ``approve()`` ve ``None``, no crea el ``UserAuthIdentity`` y el usuario no
        puede entrar con Google.

        Es seguro ligarla en este punto: el canje del token ya exigió que el
        email del prefill sea el del formulario (403
        ``google_prefill_email_mismatch``), y esta solicitud se encontró por ese
        MISMO email normalizado.

        **Nunca pisa un subject distinto ya guardado.** Dos cuentas de Google
        sobre un mismo email en trámite no se resuelve solo: se conserva la
        primera y queda dicho en el log, que es lo que faltaba para que este
        camino deje de ser mudo en cualquiera de sus variantes.

        No se audita, igual que ``_reencolar_verificacion``: no hay decisión ni
        transición de estado, solo un dato que se completa.
        """
        if google_subject is None or abierta.google_subject == google_subject:
            return

        if abierta.google_subject is not None:
            logger.warning(
                "access_request.google_subject_conflict",
                request_id=str(abierta.id),
            )
            return

        abierta.google_subject = google_subject
        # Commit propio, no porque el dato se perdería sin él —el caller HTTP
        # commitea al cerrar el request (`get_db_session`)— sino para que el
        # servicio sea autocontenido: deja la adopción a salvo acá mismo, sin
        # depender de que un paso posterior de `create()` no falle, y hace que
        # este camino se comporte igual que las otras ramas de `create()`, que
        # también commitean lo suyo. En la rama de reemisión de token el commit
        # de más abajo queda redundante, no incorrecto.
        await self._session.commit()
        logger.info(
            "access_request.google_subject_adopted", request_id=str(abierta.id)
        )

    async def _token_en_cooldown(self, request_id: uuid.UUID) -> bool:
        """¿Ya se emitió un token para esta solicitud hace muy poco?

        Evita que reenviar el formulario (o apretar "reenviar") dispare un mail
        por click. La comparación se hace EN SQL: SQLite devuelve los ``DateTime``
        sin tzinfo y compararlos en Python contra un aware explota.
        """
        corte = datetime.now(UTC) - timedelta(seconds=DEDUP_WINDOW_SECONDS)
        reciente = (
            await self._session.execute(
                select(AccessRequestToken.token_id)
                .where(
                    AccessRequestToken.access_request_id == request_id,
                    AccessRequestToken.created_at >= corte,
                )
                .limit(1)
            )
        ).first()
        return reciente is not None

    async def _emitir_token(self, solicitud: AccessRequest) -> AccessRequestToken:
        """Invalida los tokens vigentes de la solicitud y emite uno nuevo."""
        await self._session.execute(
            update(AccessRequestToken)
            .where(
                AccessRequestToken.access_request_id == solicitud.id,
                AccessRequestToken.used.is_(False),
            )
            .values(used=True)
        )
        token = AccessRequestToken(
            access_request_id=solicitud.id,
            expires_at=datetime.now(UTC)
            + timedelta(hours=ACCESS_REQUEST_TOKEN_TTL_HOURS),
            used=False,
        )
        self._session.add(token)
        solicitud.verification_email_status = EmailNotificationStatus.PENDING.value
        await self._session.flush()
        return token

    async def _verify_idempotente(self, token_uuid: uuid.UUID) -> AccessRequest:
        """El claim no prendió: o el token es basura, o ya se usó (doble click)."""
        token = (
            await self._session.execute(
                select(AccessRequestToken).where(
                    AccessRequestToken.token_id == token_uuid
                )
            )
        ).scalar_one_or_none()
        if token is not None:
            solicitud = await self._session.get(
                AccessRequest, token.access_request_id
            )
            # Ya verificada ⇒ 200 idempotente. Un token vencido y NUNCA usado, en
            # cambio, cae al 400 de abajo: nadie confirmó ese email.
            if solicitud is not None and solicitud.email_verified_at is not None:
                return solicitud
        raise self._token_invalido()

    @staticmethod
    def _token_invalido() -> HTTPException:
        """Mismo literal que ``auth_service`` para que el frontend comparta el copy."""
        return HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="token_invalido_o_expirado",
        )

    async def _lock(self, request_id: uuid.UUID) -> AccessRequest:
        """Carga la solicitud con ``FOR UPDATE``, releyendo la fila real.

        Serializa la doble decisión script↔API. En SQLite es un no-op inocuo (el
        dialecto no emite la cláusula), así que los tests no lo ejercitan: lo que
        lo cubre es la idempotencia explícita de cada transición.

        ``populate_existing=True`` no es decorativo: sin él, si la instancia ya
        está en el identity map, SQLAlchemy devuelve la copia cacheada y el
        ``status`` que se chequea puede ser el viejo — el lock bloquearía la fila
        pero la decisión se tomaría sobre datos rancios, que es justo la mitad
        del valor del ``FOR UPDATE``. Y hace falta MÁS de lo que parece, porque
        la factory de sesiones usa ``expire_on_commit=False``
        (``persistence/db/session.py``): tras un commit los atributos no se
        expiran, así que sin esto la copia vieja sobrevive.

        Contrapartida, explícita porque la factory también usa
        ``autoflush=False``: si un caller llegara con cambios PENDIENTES sobre
        esta misma fila, la relectura los pisaría en silencio (no hay autoflush
        que los drene antes del SELECT). Hoy ningún caller está en esa
        situación —los tres entran a decidir con la sesión limpia—, pero si eso
        cambia hay que flushear antes de llamar acá.
        """
        solicitud = (
            await self._session.execute(
                select(AccessRequest)
                .where(AccessRequest.id == request_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if solicitud is None:
            raise AccessRequestNotFound(request_id)
        return solicitud

    @staticmethod
    def _exigir_no_aprobada(
        solicitud: AccessRequest, destino: AccessRequestStatus
    ) -> None:
        """Una solicitud aprobada ya acuñó un tenant: no se "des-aprueba" por acá."""
        if solicitud.status == AccessRequestStatus.APPROVED.value:
            raise AccessRequestInvalidTransition(solicitud.status, destino.value)

    @staticmethod
    def _sellar_revision(
        solicitud: AccessRequest,
        reviewer_user_id: uuid.UUID | None,
        via: str,
        *,
        notes: str | None,
    ) -> None:
        solicitud.reviewed_at = datetime.now(UTC)
        solicitud.reviewed_by_user_id = reviewer_user_id
        solicitud.reviewed_via = via
        solicitud.review_notes = notes

    async def _provision(
        self, solicitud: AccessRequest, vertical: Vertical
    ) -> tuple[Tenant, User]:
        """Acuña la cuenta reusando ``provision_tenant`` (único lugar que crea las 5 filas).

        ``password_hash=None``: el usuario define su contraseña con el link de
        invitación. ``is_active=True`` porque el doble opt-in del email ya se hizo
        al verificar la solicitud.

        ``requested_plan`` NO viaja acá: la suscripción se crea FREE siempre (ver
        el docstring de ``provision_tenant``).
        """
        return await provision_tenant(
            self._session,
            business_name=solicitud.business_name,
            email=solicitud.email,
            full_name=solicitud.full_name,
            phone=solicitud.phone,
            vertical=vertical,
            password_hash=None,
            is_active=True,
        )

    async def google_identity_taken(self, provider_subject: str) -> bool:
        """¿Ese ``provider_subject`` ya está vinculado a algún usuario? **No escribe.**

        Público porque el dry-run del script (``bloqueos_para_aprobar``) necesita
        el MISMO criterio que la guardia de ``approve()``: si preguntara distinto,
        el preview volvería a prometer una cuenta que el ``--apply`` no acuña.
        """
        return (
            await self._session.execute(
                select(UserAuthIdentity.id).where(
                    UserAuthIdentity.provider == _GOOGLE_PROVIDER,
                    UserAuthIdentity.provider_subject == provider_subject,
                )
            )
        ).first() is not None

    async def _exigir_identidad_google_libre(self, provider_subject: str) -> None:
        """Falla si ese ``provider_subject`` ya está vinculado a algún usuario."""
        if await self.google_identity_taken(provider_subject):
            raise AccessRequestGoogleIdentityTaken(provider_subject)

    async def _vincular_identidad_google(
        self, solicitud: AccessRequest, tenant: Tenant, user: User
    ) -> None:
        """Liga la identidad de Google de la solicitud al usuario recién acuñado.

        Sin esto, quien pidió acceso con "Continuar con Google" es aprobado y NO
        puede entrar con Google: tendría que usar el link de contraseña, que es
        justo la fricción que ese camino venía a evitar.

        ``last_login_at`` queda en ``None`` a propósito: la identidad existe pero
        todavía nadie la usó para entrar, y sellar un login que no ocurrió sería
        inventarlo. Va en la MISMA transacción que el resto de la aprobación.

        El INSERT va bajo ``guarded_savepoint`` aunque
        ``_exigir_identidad_google_libre`` ya haya chequeado: ese pre-chequeo es
        un SELECT y el ``FOR UPDATE`` de ``_lock()`` toma la fila de la
        SOLICITUD, no la de la identidad. Dos aprobaciones simultáneas de dos
        solicitudes que comparten ``google_subject`` —el dueño aprobando desde la
        API y desde el script a la vez— pasan las dos el pre-chequeo, y sin esto
        la segunda revienta contra ``uq_auth_identity_provider_subject`` con un
        500 opaco. Con esto termina en el MISMO 409 que el camino secuencial.
        """
        if solicitud.google_subject is None:
            return
        try:
            async with guarded_savepoint(self._session, _CONFLICTO_IDENTIDAD_GOOGLE):
                # El `add` va DENTRO del savepoint: `begin_nested()` flushea antes
                # de crearlo, así que agregar afuera emitiría el INSERT afuera.
                self._session.add(
                    UserAuthIdentity(
                        tenant_id=tenant.tenant_id,
                        user_id=user.user_id,
                        provider=_GOOGLE_PROVIDER,
                        provider_subject=solicitud.google_subject,
                        # El email de la solicitud ES el que verificó Google: el
                        # prefill solo se canjea cuando los dos coinciden (ver el
                        # 403 `google_prefill_email_mismatch` del router).
                        provider_email=solicitud.email,
                    )
                )
        except SavepointConflictError:
            raise AccessRequestGoogleIdentityTaken(solicitud.google_subject) from None

    async def _sellar_screening_en_el_perfil(
        self, solicitud: AccessRequest, tenant: Tenant
    ) -> None:
        """Copia ``main_concern`` (y la trazabilidad) al perfil recién creado.

        **``onboarding_completed`` queda en ``False``**: los 6 números financieros
        se piden en el primer login, no en el formulario público. Acá solo viaja
        lo que el solicitante ya contestó.
        """
        perfil = (
            await self._session.execute(
                select(BusinessProfile).where(
                    BusinessProfile.tenant_id == tenant.tenant_id
                )
            )
        ).scalar_one()
        perfil.custom_fields = {
            **(perfil.custom_fields or {}),
            "main_concern": solicitud.main_concern,
            "access_request_id": str(solicitud.id),
        }
        await self._session.flush()


# ── Armadores de email (funciones puras, testeables sin Celery) ──────────────


def build_verification_email(
    req: AccessRequest, token_id: uuid.UUID | str
) -> tuple[str, str, str]:
    """``(subject, html, text)`` del mail de doble opt-in que recibe el solicitante."""
    url = f"{get_settings().FRONTEND_URL}/solicitud-verificada?token={token_id}"
    cuerpo_html, texto = render_action_email(
        eyebrow="Solicitud de acceso",
        heading="Confirmá tu email",
        body=(
            f"Recibimos tu solicitud de acceso a Véktor para {req.business_name}. "
            "Confirmá que este correo es tuyo para que podamos revisarla. "
            f"El link vale {ACCESS_REQUEST_TOKEN_TTL_HOURS} horas."
        ),
        cta_label="Confirmar mi email →",
        cta_url=url,
        footnote="Si no pediste acceso a Véktor, podés ignorar este email.",
    )
    return "Confirmá tu email — Véktor", cuerpo_html, texto


def build_owner_notification_email(req: AccessRequest) -> tuple[str, str, str]:
    """``(subject, html, text)`` del aviso interno al dueño, con la ficha completa.

    Sin panel de administración, este mail y el script de consola son las
    herramientas reales de operación: por eso lleva TODO el screening, no un
    resumen. El asunto marca la prioridad para que se vea en la bandeja.
    """
    es_premium = req.requested_plan == RequestedPlan.PREMIUM.value
    prefijo = "[PRIORIDAD PREMIUM] " if es_premium else ""
    subject = f"{prefijo}Nueva solicitud de acceso — {req.business_name}"

    filas: list[tuple[str, str]] = [
        ("Intención", "Cuenta Premium" if es_premium else "Cuenta gratuita"),
        ("Prioridad de revisión", "Alta" if es_premium else "Normal"),
        ("Negocio", req.business_name),
        ("Rubro declarado", req.requested_vertical),
        ("Detalle del rubro", req.vertical_other_text or "—"),
        ("Contacto", req.full_name),
        ("Email", req.email),
        ("Teléfono", req.phone or "—"),
        ("Antigüedad", req.years_operating),
        ("Equipo", req.staff_size),
        ("Facturación", req.monthly_revenue_band),
        ("Preocupación principal", req.main_concern),
        ("Registros", req.records_format),
        ("Historial", req.history_depth),
        ("Puede subir archivos", req.can_share_files),
        ("Cómo lo lleva", req.records_notes or "—"),
        ("Algo más", req.applicant_notes or "—"),
        ("Origen", req.cta_source or "—"),
        (
            "Email verificado",
            req.email_verified_at.isoformat() if req.email_verified_at else "no",
        ),
        ("Solicitud", str(req.id)),
    ]
    # Todo lo que escribió el visitante se escapa antes de entrar al HTML.
    filas_html = "".join(
        f"<tr><td style='padding:4px 12px 4px 0;color:#666'>{html.escape(k)}</td>"
        f"<td style='padding:4px 0'><strong>{html.escape(v)}</strong></td></tr>"
        for k, v in filas
    )
    cuerpo_html = (
        "<h2>Nueva solicitud de acceso</h2>"
        f"<table style='font-family:sans-serif;font-size:14px'>{filas_html}</table>"
        "<p style='color:#999;font-size:12px'>Enviada desde el formulario público de Véktor.</p>"
    )
    texto = "Nueva solicitud de acceso\n\n" + "\n".join(f"{k}: {v}" for k, v in filas)
    return subject, cuerpo_html, texto


def build_decision_email(
    req: AccessRequest,
    *,
    decision: AccessRequestStatus | str,
    invite_token: uuid.UUID | str | None,
) -> tuple[str, str, str]:
    """``(subject, html, text)`` del mail de decisión que recibe el solicitante.

    Al solicitante NO se le promete aprobación más rápida por elegir Premium, y el
    rechazo NUNCA transcribe ``rejection_reason``: es una nota interna del dueño.
    """
    valor = decision.value if isinstance(decision, AccessRequestStatus) else decision

    if valor == AccessRequestStatus.APPROVED.value:
        if invite_token is None:
            raise ValueError("Aprobar exige un token de invitación para el link")
        url = f"{get_settings().FRONTEND_URL}/definir-password?token={invite_token}"
        cuerpo_html, texto = render_action_email(
            eyebrow="Solicitud aprobada",
            heading="Tu acceso a Véktor está listo",
            body=(
                f"Revisamos la solicitud de {req.business_name} y habilitamos tu cuenta. "
                "Definí tu contraseña para entrar por primera vez. "
                f"El link vale {_INVITE_TOKEN_TTL_HOURS} horas."
            ),
            cta_label="Definir mi contraseña →",
            cta_url=url,
            footnote=(
                "Cuando entres te vamos a pedir algunos números de tu negocio para "
                "calcular tu score."
            ),
        )
        return "Tu acceso a Véktor está listo", cuerpo_html, texto

    if valor == AccessRequestStatus.WAITLIST.value:
        cuerpo_html, texto = render_action_email(
            eyebrow="Solicitud de acceso",
            heading="Tu solicitud quedó en lista de espera",
            body=(
                f"Recibimos y revisamos la solicitud de {req.business_name}. "
                "Por ahora no tenemos lugar para sumar cuentas nuevas, así que la "
                "dejamos en lista de espera. Te escribimos a este mismo correo "
                "cuando se libere."
            ),
            cta_label=None,
            cta_url=None,
            footnote="No hace falta que vuelvas a completar el formulario.",
        )
        return "Tu solicitud de acceso a Véktor — lista de espera", cuerpo_html, texto

    if valor == AccessRequestStatus.REJECTED.value:
        cuerpo_html, texto = render_action_email(
            eyebrow="Solicitud de acceso",
            heading="Sobre tu solicitud de acceso",
            body=(
                f"Gracias por contarnos sobre {req.business_name}. Por ahora no vamos "
                "a poder habilitar una cuenta: Véktor está afinado para algunos rubros "
                "en particular y preferimos no darte números que no te sirvan. "
                "Si tu negocio cambia, escribinos de nuevo."
            ),
            cta_label=None,
            cta_url=None,
            footnote="Gracias por el interés.",
        )
        return "Sobre tu solicitud de acceso a Véktor", cuerpo_html, texto

    raise ValueError(f"No hay email de decisión para el estado '{valor}'")


def build_account_exists_email(email: str) -> tuple[str, str, str]:
    """``(subject, html, text)`` del "ya tenés cuenta, entrá acá".

    Es la contracara de la neutralidad a enumeración: la respuesta HTTP no puede
    distinguir el caso, así que el único canal que lo distingue es la casilla del
    dueño del email. No dice nada del negocio ni de la solicitud.
    """
    cuerpo_html, texto = render_action_email(
        eyebrow="Solicitud de acceso",
        heading="Ya tenés una cuenta en Véktor",
        body=(
            f"Recibimos una solicitud de acceso con {email}, pero esa dirección ya "
            "tiene una cuenta activa. Entrá con tu contraseña; si no la recordás, "
            "podés recuperarla desde la pantalla de ingreso."
        ),
        cta_label="Ir a iniciar sesión →",
        cta_url=f"{get_settings().FRONTEND_URL}/login",
        footnote="Si no fuiste vos, podés ignorar este email: no se creó nada nuevo.",
    )
    return "Ya tenés una cuenta en Véktor", cuerpo_html, texto
