"""Celery worker: los cuatro emails del flujo de solicitudes de acceso.

Calcado de ``app/jobs/contact_lead_worker.py`` (mismos parámetros del decorador,
mismo reintento, mismo marcado de ``failed`` al agotarlo). La regla cardinal es
la misma: ``AccessRequestService`` persiste y COMMITEA antes de encolar, así que
acá nunca se asume nada que la transacción no haya cerrado — un fallo de correo
no puede perder una solicitud.

**Sin panel de administración, estos mails y el script de consola son las
herramientas reales de operación del dueño.** Si el aviso no sale, el dueño no se
entera de que hay alguien esperando: por eso al agotar los reintentos se marca la
columna ``*_email_status`` en ``failed``, que es lo que el script lista.

Las cuatro tareas:

======================================  ==========================================
Nombre Celery                           Qué manda
======================================  ==========================================
``jobs.notify_access_request_verification``  doble opt-in al solicitante
``jobs.notify_access_request_owner``         ficha completa al dueño
``jobs.notify_access_request_decision``      aprobado / lista de espera / rechazo
``jobs.notify_account_exists``               "ya tenés cuenta, entrá acá"
======================================  ==========================================

La cuarta existe porque ``POST /access-requests`` es **neutro a enumeración de
cuentas**: responde lo mismo exista o no una cuenta con ese email. Sin ese mail,
quien ya tiene cuenta y vuelve a pedir acceso se queda sin ninguna señal de qué
hacer. Recibe el email ya normalizado y no un ``request_id``, porque en ese caso
NO se persiste ninguna solicitud (y por lo tanto no hay columna que marcar).

⚠️ El nombre Celery de esa cuarta tarea (``jobs.notify_account_exists``) NO
espeja el nombre de la función (``notify_access_request_account_exists``): el
primero lo fija el plan del PR, el segundo lo fija
``access_request_service.TASK_CUENTA_EXISTENTE``, que encola por *atributo del
módulo*. Los dos son contratos escritos; la asimetría es deliberada.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from app.jobs.celery_app import celery_app
from app.observability.logger import get_logger, log_job

if TYPE_CHECKING:  # pragma: no cover - solo para tipos
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.persistence.models.access_request import AccessRequest

logger = get_logger(__name__)

# ── Qué columna audita cada email del flujo ──────────────────────────────────
# Son los nombres reales de `access_requests`; el marcado los usa con `setattr`
# para no repetir tres veces el mismo bloque de sesión.
COLUMNA_VERIFICACION = "verification_email_status"
COLUMNA_AVISO_DUENIO = "owner_notification_status"
COLUMNA_DECISION = "decision_email_status"

# Parámetros del decorador, iguales a los de `notify_contact_lead`.
_PARAMS_TAREA: dict[str, Any] = {
    "bind": True,
    "queue": "notifications",
    "max_retries": 3,
    "default_retry_delay": 30,
    "soft_time_limit": 45,
    "time_limit": 60,
}


# ── Acceso a datos ───────────────────────────────────────────────────────────


@asynccontextmanager
async def _sesion() -> AsyncIterator[AsyncSession]:
    """Sesión efímera con engine propio: el worker no comparte el pool del API."""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine  # noqa: PLC0415
    from sqlalchemy.orm import sessionmaker  # noqa: PLC0415

    from app.config.settings import get_settings  # noqa: PLC0415

    s = get_settings()
    engine = create_async_engine(
        s.DATABASE_URL, pool_pre_ping=True, connect_args=s.pg_connect_args
    )
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)  # type: ignore[call-overload]
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


async def _cargar_solicitud(request_id: str) -> AccessRequest | None:
    """La solicitud, ya desprendida de la sesión.

    Se devuelve el objeto entero (y no una tupla ya armada) porque los cuatro
    armadores de email son funciones PURAS sobre él: no hay lazy loading que
    pueda explotar afuera de la sesión, y ``expire_on_commit=False`` garantiza
    que los atributos sigan cargados.
    """
    import uuid  # noqa: PLC0415

    from app.persistence.models.access_request import AccessRequest  # noqa: PLC0415

    async with _sesion() as session:
        return await session.get(AccessRequest, uuid.UUID(request_id))


async def _marcar(request_id: str, columna: str, estado: str) -> None:
    """Sella el resultado del envío en la columna de estado que corresponda."""
    import uuid  # noqa: PLC0415

    from app.persistence.models.access_request import AccessRequest  # noqa: PLC0415

    async with _sesion() as session:
        solicitud = await session.get(AccessRequest, uuid.UUID(request_id))
        if solicitud is not None:
            setattr(solicitud, columna, estado)
            await session.commit()


async def _autoverificar(request_id: str, token_id: str) -> bool:
    """Escotilla de desarrollo: verifica la solicitud sin mandar el mail.

    Loguea el cuerpo en texto plano del mail real —que ya trae el link con el
    token, así que no hay que duplicar acá la URL— y pasa la solicitud a
    ``pending``. Devuelve si la transición ocurrió de verdad (para no encolar el
    aviso al dueño dos veces si la tarea se reejecuta).

    NO audita en ``decision_audit_log``: es un atajo de desarrollo, no un camino
    de producción. Lo que sí hace es dejar la solicitud en el MISMO estado que
    dejaría un click real en el mail, para que lo que se prueba después sea el
    flujo de verdad.

    ⚠️ El log lleva el token de verificación en claro (nivel WARNING). Es
    aceptable SOLO porque la escotilla no puede estar activa fuera de desarrollo
    (``settings.ACCESS_REQUEST_AUTOVERIFY``): los logs de una máquina de
    desarrollo no van a un sink compartido. Si algún día estos logs se envían a
    un agregador externo, este ``logger.warning`` hay que revisarlo antes.
    """
    import uuid  # noqa: PLC0415
    from datetime import UTC, datetime  # noqa: PLC0415

    from app.application.services.access_request_service import (  # noqa: PLC0415
        build_verification_email,
    )
    from app.domain.access_request import AccessRequestStatus  # noqa: PLC0415
    from app.domain.contact_lead import EmailNotificationStatus  # noqa: PLC0415
    from app.persistence.models.access_request import AccessRequest  # noqa: PLC0415

    async with _sesion() as session:
        solicitud = await session.get(AccessRequest, uuid.UUID(request_id))
        if solicitud is None:
            logger.warning("access_request.autoverify_missing", request_id=request_id)
            return False

        _, _, texto = build_verification_email(solicitud, token_id)
        logger.warning(
            "access_request.autoverify",
            request_id=request_id,
            token_id=token_id,
            cuerpo=texto,
        )

        if solicitud.status != AccessRequestStatus.UNVERIFIED.value:
            return False

        solicitud.status = AccessRequestStatus.PENDING.value
        if solicitud.email_verified_at is None:
            solicitud.email_verified_at = datetime.now(UTC)
        # El canal de entrega fue el log; se marca `sent` para que la escotilla no
        # deje solicitudes "pendientes de email" que ninguna herramienta va a
        # poder resolver.
        solicitud.verification_email_status = EmailNotificationStatus.SENT.value
        await session.commit()
        return True


# ── Envío ────────────────────────────────────────────────────────────────────


def _despachar(
    self: Any,
    *,
    evento: str,
    destinatario: str,
    email: tuple[str, str, str],
    request_id: str | None = None,
    columna: str | None = None,
) -> None:
    """Manda el correo, reintenta ante fallo y sella el resultado.

    ``request_id``/``columna`` van juntos y son opcionales porque
    ``notify_account_exists`` no tiene fila que marcar: ese caso no persiste nada
    (neutralidad a enumeración), así que su único rastro es el log.

    ⚠️ **El fallo definitivo se detecta ANTES de llamar a ``retry``, no
    capturando ``MaxRetriesExceededError``.** ``Task.retry(exc=exc)`` con los
    reintentos agotados hace ``raise_with_context(exc)``
    (``celery/app/task.py``): re-lanza la excepción ORIGINAL, y solo levanta
    ``MaxRetriesExceededError`` cuando ``exc`` es falsy. Como acá siempre se le
    pasa la excepción real, un ``except self.MaxRetriesExceededError`` sería
    código muerto: la ``SMTPError`` se propagaría, la tarea fallaría y la columna
    quedaría en ``pending`` PARA SIEMPRE. El dueño correría
    ``access_requests.py list --email-failed``, no vería a nadie, y concluiría que
    está todo bien mientras alguien espera un mail que nunca salió.
    """
    import asyncio  # noqa: PLC0415

    from app.domain.contact_lead import EmailNotificationStatus  # noqa: PLC0415
    from app.integrations.smtp import SMTPClient  # noqa: PLC0415

    subject, cuerpo_html, texto = email

    def _sellar(estado: EmailNotificationStatus) -> None:
        if request_id is not None and columna is not None:
            asyncio.run(_marcar(request_id, columna, estado.value))

    try:
        SMTPClient().send(destinatario, subject, cuerpo_html, texto, raise_on_error=True)
    except Exception as exc:  # noqa: BLE001 — reintenta; si se agotó, marca failed
        logger.warning(f"{evento}.email_failed", request_id=request_id, error=str(exc))
        maximo = self.max_retries
        # `max_retries=None` en Celery significa "reintentar para siempre", que NO
        # es lo mismo que 0: por eso se compara `is not None` en vez de `or 0`
        # (regla del repo: nunca un default neutral cuando 0 es un valor válido).
        # Las cuatro tareas de este módulo declaran 3, así que en la práctica esta
        # rama siempre puede decidir.
        if maximo is not None and self.request.retries >= maximo:
            _sellar(EmailNotificationStatus.FAILED)
            return
        # `retry()` NO retorna: levanta `Retry` para que el worker reencole. El
        # `raise` es el idiom de Celery y deja explícito que el flujo corta acá.
        # El `from exc` que pide B904 sería sintaxis muerta: Python evalúa
        # `self.retry(...)` primero, esa llamada ya levanta, y la cláusula nunca
        # llega a aplicarse. La causa real viaja en `Retry(exc=exc)`.
        raise self.retry(exc=exc)  # noqa: B904

    _sellar(EmailNotificationStatus.SENT)
    logger.info(f"{evento}.email_sent", request_id=request_id)


# ── Las cuatro tareas ────────────────────────────────────────────────────────


@celery_app.task(name="jobs.notify_access_request_verification", **_PARAMS_TAREA)  # type: ignore[misc]
def notify_access_request_verification(
    self: Any, request_id: str, token_id: str
) -> None:
    """Mail de doble opt-in al solicitante (o escotilla de dev)."""
    import asyncio  # noqa: PLC0415

    from app.application.services.access_request_service import (  # noqa: PLC0415
        build_verification_email,
    )
    from app.config.settings import get_settings  # noqa: PLC0415

    with log_job("jobs.notify_access_request_verification", logger=logger):
        if get_settings().ACCESS_REQUEST_AUTOVERIFY:
            if asyncio.run(_autoverificar(request_id, token_id)):
                # La solicitud quedó en `pending` igual que con un click real:
                # se sostiene la invariante "toda solicitud en la cola generó un
                # aviso al dueño". Encolar nunca puede romper la tarea.
                try:
                    notify_access_request_owner.delay(request_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "access_request.autoverify_enqueue_failed",
                        request_id=request_id,
                        error=str(exc),
                    )
            return

        solicitud = asyncio.run(_cargar_solicitud(request_id))
        if solicitud is None:
            logger.warning(
                "access_request.notify_missing",
                request_id=request_id,
                tarea="verification",
            )
            return

        _despachar(
            self,
            evento="access_request.verification",
            destinatario=solicitud.email,
            email=build_verification_email(solicitud, token_id),
            request_id=request_id,
            columna=COLUMNA_VERIFICACION,
        )


@celery_app.task(name="jobs.notify_access_request_owner", **_PARAMS_TAREA)  # type: ignore[misc]
def notify_access_request_owner(self: Any, request_id: str) -> None:
    """Aviso interno al dueño con la ficha completa del screening.

    El asunto marca ``[PRIORIDAD PREMIUM]`` cuando la solicitud declaró esa
    intención (lo arma ``build_owner_notification_email``): sin UI de admin, la
    bandeja de entrada ES la cola de revisión.
    """
    import asyncio  # noqa: PLC0415

    from app.application.services.access_request_service import (  # noqa: PLC0415
        build_owner_notification_email,
    )
    from app.config.settings import get_settings  # noqa: PLC0415
    from app.domain.contact_lead import EmailNotificationStatus  # noqa: PLC0415

    with log_job("jobs.notify_access_request_owner", logger=logger):
        # Misma casilla (y misma semántica) que los leads del formulario de
        # contacto: en producción sin CONTACT_LEAD_EMAIL no se manda a una
        # dirección no operativa, se marca failed y se loguea.
        destinatario = get_settings().contact_lead_recipient
        if not destinatario:
            logger.error("access_request.no_recipient", request_id=request_id)
            asyncio.run(
                _marcar(
                    request_id,
                    COLUMNA_AVISO_DUENIO,
                    EmailNotificationStatus.FAILED.value,
                )
            )
            return

        solicitud = asyncio.run(_cargar_solicitud(request_id))
        if solicitud is None:
            logger.warning(
                "access_request.notify_missing", request_id=request_id, tarea="owner"
            )
            return

        _despachar(
            self,
            evento="access_request.owner",
            destinatario=destinatario,
            email=build_owner_notification_email(solicitud),
            request_id=request_id,
            columna=COLUMNA_AVISO_DUENIO,
        )


@celery_app.task(name="jobs.notify_access_request_decision", **_PARAMS_TAREA)  # type: ignore[misc]
def notify_access_request_decision(
    self: Any, request_id: str, decision: str, invite_token: str | None = None
) -> None:
    """Mail de la decisión del dueño: aprobada, lista de espera o rechazo."""
    import asyncio  # noqa: PLC0415

    from app.application.services.access_request_service import (  # noqa: PLC0415
        build_decision_email,
    )
    from app.domain.contact_lead import EmailNotificationStatus  # noqa: PLC0415

    with log_job("jobs.notify_access_request_decision", logger=logger):
        solicitud = asyncio.run(_cargar_solicitud(request_id))
        if solicitud is None:
            logger.warning(
                "access_request.notify_missing", request_id=request_id, tarea="decision"
            )
            return

        try:
            email = build_decision_email(
                solicitud, decision=decision, invite_token=invite_token
            )
        except ValueError as exc:
            # Estado sin mail de decisión, o aprobación sin token de invitación:
            # es un error de programación, reintentar no lo arregla. Se marca
            # failed para que el script del dueño lo liste.
            logger.error(
                "access_request.decision_email_invalid",
                request_id=request_id,
                decision=decision,
                error=str(exc),
            )
            asyncio.run(
                _marcar(
                    request_id, COLUMNA_DECISION, EmailNotificationStatus.FAILED.value
                )
            )
            return

        _despachar(
            self,
            evento="access_request.decision",
            destinatario=solicitud.email,
            email=email,
            request_id=request_id,
            columna=COLUMNA_DECISION,
        )


@celery_app.task(name="jobs.notify_account_exists", **_PARAMS_TAREA)  # type: ignore[misc]
def notify_access_request_account_exists(self: Any, email: str) -> None:
    """Mail «ya tenés cuenta, entrá acá»: el único canal que distingue ese caso.

    No hay ``request_id``: cuando el email ya tiene cuenta no se persiste ninguna
    solicitud, así que tampoco hay columna de estado que marcar. Si el envío
    falla definitivamente queda el log y nada más, que es el precio de no
    guardar una fila que sería, ella misma, un registro de que ese email existe.
    """
    from app.application.services.access_request_service import (  # noqa: PLC0415
        build_account_exists_email,
    )

    with log_job("jobs.notify_account_exists", logger=logger):
        _despachar(
            self,
            evento="access_request.account_exists",
            destinatario=email,
            email=build_account_exists_email(email),
        )
