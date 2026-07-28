"""Tests del worker de emails de solicitudes de acceso.

Sin panel de administración, estos cuatro mails son las herramientas reales de
operación del dueño: lo que se cubre acá es que salgan al destinatario correcto,
que un fallo se reintente, y —sobre todo— que un fallo DEFINITIVO quede marcado
en la columna ``*_email_status`` que después lista el script de consola.

Las tareas Celery se invocan por ``__wrapped__`` (la función real, con el Task
como ``self``). El camino de reintento se ejercita sobre ``_despachar`` con un
``self`` falso, porque un Task de verdad llamado fuera de un worker no lleva la
cuenta de reintentos.

Las tareas abren su propia sesión con ``asyncio.run`` sobre un event loop nuevo,
así que los tests sincrónicos parchean ``_cargar_solicitud``/``_marcar`` con
stubs que no tocan la base (mezclar aiosqlite entre loops explota). El único que
sí necesita base —``_autoverificar``— se testea como test async, parcheando
``_sesion`` para que ceda la sesión del test.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from celery.exceptions import MaxRetriesExceededError, Retry
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.application.services.access_request_service import (
    TASK_AVISO_DUENIO,
    TASK_CUENTA_EXISTENTE,
    TASK_DECISION,
    TASK_VERIFICACION,
    build_owner_notification_email,
)
from app.config.settings import Settings, get_settings
from app.domain.access_request import AccessRequestStatus, RequestedPlan
from app.domain.contact_lead import EmailNotificationStatus
from app.jobs import access_request_worker as worker
from app.jobs.celery_app import celery_app
from app.persistence.db.base import Base
from app.persistence.models.access_request import AccessRequest

TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _solicitud(**overrides: Any) -> AccessRequest:
    """Solicitud en memoria con todos los NOT NULL cubiertos."""
    datos: dict[str, Any] = {
        "id": uuid.uuid4(),
        "full_name": "Ana Gómez",
        "email": "ana@kiosco.test",
        "phone": "+5491122334455",
        "business_name": "Kiosco La Esquina",
        "requested_vertical": "kiosco_almacen",
        "vertical_other_text": None,
        "requested_plan": RequestedPlan.FREE.value,
        "years_operating": "2y_5y",
        "staff_size": "2_5",
        "monthly_revenue_band": "3m_10m",
        "main_concern": "MARGIN",
        "records_format": "planilla",
        "history_depth": "1y_3y",
        "can_share_files": "si_ordenados",
        "status": AccessRequestStatus.UNVERIFIED.value,
        "verification_email_status": EmailNotificationStatus.PENDING.value,
        "owner_notification_status": EmailNotificationStatus.PENDING.value,
        "decision_email_status": EmailNotificationStatus.PENDING.value,
        "consent_version": "v1",
        "consent_accepted_at": datetime.now(UTC),
    }
    datos.update(overrides)
    return AccessRequest(**datos)


class _SMTPEspia:
    """Doble de ``SMTPClient``: registra los envíos y puede fallar a pedido."""

    enviados: list[tuple[str, str, str]] = []
    falla: bool = False

    def send(
        self,
        to_email: str,
        subject: str,
        body_html: str,
        body_text: str | None = None,
        *,
        raise_on_error: bool = False,
    ) -> str | None:
        if self.falla:
            raise RuntimeError("resend caído")
        _SMTPEspia.enviados.append((to_email, subject, body_text or ""))
        return "msg-1"


class _SelfFalso:
    """Lo único que el worker usa de ``self``: ``retry`` y su excepción."""

    MaxRetriesExceededError = MaxRetriesExceededError

    def __init__(self, *, max_retries: int = 3) -> None:
        self.intentos = 0
        self._max = max_retries

    def retry(self, exc: BaseException | None = None) -> None:
        self.intentos += 1
        if self.intentos > self._max:
            raise MaxRetriesExceededError
        raise Retry(exc=exc)


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> type[_SMTPEspia]:
    _SMTPEspia.enviados = []
    _SMTPEspia.falla = False
    monkeypatch.setattr("app.integrations.smtp.SMTPClient", _SMTPEspia)
    return _SMTPEspia


@pytest.fixture
def marcados(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    """Intercepta ``_marcar`` para no depender de una base real."""
    registro: list[tuple[str, str, str]] = []

    async def _stub(request_id: str, columna: str, estado: str) -> None:
        registro.append((request_id, columna, estado))

    monkeypatch.setattr(worker, "_marcar", _stub)
    return registro


@pytest.fixture
def sin_autoverify(monkeypatch: pytest.MonkeyPatch) -> None:
    """La suite corre con APP_DEBUG=true, que prende la escotilla de dev."""
    monkeypatch.setattr(get_settings(), "ACCESS_REQUEST_AUTOVERIFY", False)


def _cargar(monkeypatch: pytest.MonkeyPatch, solicitud: AccessRequest | None) -> None:
    async def _stub(request_id: str) -> AccessRequest | None:
        return solicitud

    monkeypatch.setattr(worker, "_cargar_solicitud", _stub)


# ── Contrato con el servicio y con Celery ────────────────────────────────────


def test_el_servicio_encola_atributos_que_existen() -> None:
    """Las 4 constantes de ``access_request_service`` son atributos del worker.

    El servicio encola con ``getattr(modulo, nombre)``: un typo acá no rompe
    ningún import, se traga la excepción y el mail nunca sale.
    """
    for nombre in (
        TASK_VERIFICACION,
        TASK_AVISO_DUENIO,
        TASK_DECISION,
        TASK_CUENTA_EXISTENTE,
    ):
        assert hasattr(worker, nombre), nombre


def test_las_cuatro_tareas_van_a_la_cola_de_notificaciones() -> None:
    rutas = celery_app.conf.task_routes
    for nombre in (
        "jobs.notify_access_request_verification",
        "jobs.notify_access_request_owner",
        "jobs.notify_access_request_decision",
        "jobs.notify_account_exists",
    ):
        assert rutas[nombre] == {"queue": "notifications"}


def test_el_modulo_esta_en_el_include_de_celery() -> None:
    assert "app.jobs.access_request_worker" in celery_app.conf.include


def test_parametros_de_reintento_calcados_del_worker_de_leads() -> None:
    for tarea in (
        worker.notify_access_request_verification,
        worker.notify_access_request_owner,
        worker.notify_access_request_decision,
        worker.notify_access_request_account_exists,
    ):
        assert tarea.max_retries == 3
        assert tarea.default_retry_delay == 30
        assert tarea.soft_time_limit == 45


# ── Asuntos diferenciados ────────────────────────────────────────────────────


def test_asunto_premium_lleva_la_marca_de_prioridad() -> None:
    subject, cuerpo_html, texto = build_owner_notification_email(
        _solicitud(requested_plan=RequestedPlan.PREMIUM.value)
    )
    assert subject == "[PRIORIDAD PREMIUM] Nueva solicitud de acceso — Kiosco La Esquina"
    assert "Cuenta Premium" in cuerpo_html
    assert "Intención: Cuenta Premium" in texto
    assert "Prioridad de revisión: Alta" in texto


def test_asunto_free_no_lleva_la_marca() -> None:
    subject, _, texto = build_owner_notification_email(
        _solicitud(requested_plan=RequestedPlan.FREE.value)
    )
    assert subject == "Nueva solicitud de acceso — Kiosco La Esquina"
    assert "PRIORIDAD PREMIUM" not in subject
    assert "Prioridad de revisión: Normal" in texto


# ── Aviso al dueño ───────────────────────────────────────────────────────────


def test_aviso_al_duenio_va_a_la_casilla_de_leads(
    monkeypatch: pytest.MonkeyPatch,
    smtp: type[_SMTPEspia],
    marcados: list[tuple[str, str, str]],
) -> None:
    solicitud = _solicitud(requested_plan=RequestedPlan.PREMIUM.value)
    _cargar(monkeypatch, solicitud)
    monkeypatch.setattr(get_settings(), "CONTACT_LEAD_EMAIL", "hola@vektor.app")

    worker.notify_access_request_owner.__wrapped__(str(solicitud.id))

    (destinatario, subject, _), = smtp.enviados
    assert destinatario == "hola@vektor.app"
    assert subject.startswith("[PRIORIDAD PREMIUM] ")
    assert marcados == [
        (str(solicitud.id), "owner_notification_status", EmailNotificationStatus.SENT.value)
    ]


def test_sin_casilla_configurada_se_marca_failed_sin_intentar_enviar(
    monkeypatch: pytest.MonkeyPatch,
    smtp: type[_SMTPEspia],
    marcados: list[tuple[str, str, str]],
) -> None:
    """Producción sin CONTACT_LEAD_EMAIL: la solicitud NO se pierde, queda failed."""
    solicitud = _solicitud()
    _cargar(monkeypatch, solicitud)
    monkeypatch.setattr(Settings, "contact_lead_recipient", property(lambda self: ""))

    worker.notify_access_request_owner.__wrapped__(str(solicitud.id))

    assert smtp.enviados == []
    assert marcados == [
        (
            str(solicitud.id),
            "owner_notification_status",
            EmailNotificationStatus.FAILED.value,
        )
    ]


# ── Reintentos y fallo definitivo ────────────────────────────────────────────


def test_un_fallo_de_envio_reintenta(
    smtp: type[_SMTPEspia], marcados: list[tuple[str, str, str]]
) -> None:
    smtp.falla = True
    falso = _SelfFalso()

    with pytest.raises(Retry):
        worker._despachar(
            falso,
            evento="access_request.owner",
            destinatario="hola@vektor.app",
            email=("asunto", "<p>x</p>", "x"),
            request_id="rid",
            columna=worker.COLUMNA_AVISO_DUENIO,
        )

    assert falso.intentos == 1
    # Todavía no es un fallo definitivo: nada que listar para el dueño.
    assert marcados == []


@pytest.mark.parametrize(
    ("columna", "esperada"),
    [
        (worker.COLUMNA_VERIFICACION, "verification_email_status"),
        (worker.COLUMNA_AVISO_DUENIO, "owner_notification_status"),
        (worker.COLUMNA_DECISION, "decision_email_status"),
    ],
)
def test_al_agotar_reintentos_se_marca_failed_en_su_columna(
    smtp: type[_SMTPEspia],
    marcados: list[tuple[str, str, str]],
    columna: str,
    esperada: str,
) -> None:
    smtp.falla = True
    falso = _SelfFalso(max_retries=0)  # el primer retry ya agota

    worker._despachar(
        falso,
        evento="access_request.owner",
        destinatario="hola@vektor.app",
        email=("asunto", "<p>x</p>", "x"),
        request_id="rid",
        columna=columna,
    )

    assert marcados == [("rid", esperada, EmailNotificationStatus.FAILED.value)]


def test_cuenta_existente_no_marca_nada_al_agotar_reintentos(
    smtp: type[_SMTPEspia], marcados: list[tuple[str, str, str]]
) -> None:
    """No hay fila que marcar: guardarla sería, ella misma, revelar que el email existe."""
    smtp.falla = True

    worker._despachar(
        _SelfFalso(max_retries=0),
        evento="access_request.account_exists",
        destinatario="ana@kiosco.test",
        email=("asunto", "<p>x</p>", "x"),
    )

    assert marcados == []


# ── Verificación (doble opt-in) ──────────────────────────────────────────────


def test_verificacion_le_manda_el_link_al_solicitante(
    monkeypatch: pytest.MonkeyPatch,
    smtp: type[_SMTPEspia],
    marcados: list[tuple[str, str, str]],
    sin_autoverify: None,
) -> None:
    solicitud = _solicitud()
    _cargar(monkeypatch, solicitud)
    token = uuid.uuid4()

    worker.notify_access_request_verification.__wrapped__(str(solicitud.id), str(token))

    (destinatario, subject, texto), = smtp.enviados
    assert destinatario == "ana@kiosco.test"
    assert subject == "Confirmá tu email — Véktor"
    assert str(token) in texto
    assert marcados == [
        (
            str(solicitud.id),
            "verification_email_status",
            EmailNotificationStatus.SENT.value,
        )
    ]


def test_solicitud_inexistente_no_manda_ni_marca(
    monkeypatch: pytest.MonkeyPatch,
    smtp: type[_SMTPEspia],
    marcados: list[tuple[str, str, str]],
    sin_autoverify: None,
) -> None:
    _cargar(monkeypatch, None)

    worker.notify_access_request_verification.__wrapped__(
        str(uuid.uuid4()), str(uuid.uuid4())
    )

    assert smtp.enviados == []
    assert marcados == []


def test_autoverify_no_manda_mail_y_avisa_al_duenio(
    monkeypatch: pytest.MonkeyPatch, smtp: type[_SMTPEspia]
) -> None:
    """La escotilla de dev reemplaza el mail, no el resto del flujo."""
    monkeypatch.setattr(get_settings(), "ACCESS_REQUEST_AUTOVERIFY", True)
    encolados: list[str] = []

    async def _autoverificado(request_id: str, token_id: str) -> bool:
        return True

    monkeypatch.setattr(worker, "_autoverificar", _autoverificado)
    monkeypatch.setattr(
        worker.notify_access_request_owner, "delay", lambda rid: encolados.append(rid)
    )

    rid = str(uuid.uuid4())
    worker.notify_access_request_verification.__wrapped__(rid, str(uuid.uuid4()))

    assert smtp.enviados == []
    assert encolados == [rid]


def test_la_escotilla_no_hereda_de_enable_email_verification() -> None:
    """Bajo DEBUG, ``ENABLE_EMAIL_VERIFICATION`` se apaga; la escotilla se prende
    sola, pero por su propia regla — y en producción NUNCA queda activa."""
    dev = Settings(APP_DEBUG=True, ENVIRONMENT="development")
    assert dev.ENABLE_EMAIL_VERIFICATION is False
    assert dev.ACCESS_REQUEST_AUTOVERIFY is True

    demo = Settings(APP_DEBUG=False, DEMO_MODE=True, ENVIRONMENT="development")
    assert demo.ENABLE_EMAIL_VERIFICATION is False
    # DEMO_MODE apaga la verificación de email pero NO abre el opt-in del flujo.
    assert demo.ACCESS_REQUEST_AUTOVERIFY is False


def test_en_produccion_la_escotilla_queda_apagada_aunque_la_seteen() -> None:
    """Saltear el opt-in en prod metería emails no confirmados en la cola."""
    secreto = "x" * 40
    prod = Settings(
        APP_DEBUG=True,
        ENVIRONMENT="production",
        ACCESS_REQUEST_AUTOVERIFY=True,
        SECRET_KEY=secreto,
        JWT_SECRET_KEY=secreto,
    )
    assert prod.ACCESS_REQUEST_AUTOVERIFY is False


# ── Decisión ─────────────────────────────────────────────────────────────────


def test_aprobacion_manda_el_link_de_invitacion(
    monkeypatch: pytest.MonkeyPatch,
    smtp: type[_SMTPEspia],
    marcados: list[tuple[str, str, str]],
) -> None:
    solicitud = _solicitud(status=AccessRequestStatus.APPROVED.value)
    _cargar(monkeypatch, solicitud)
    invitacion = uuid.uuid4()

    worker.notify_access_request_decision.__wrapped__(
        str(solicitud.id), AccessRequestStatus.APPROVED.value, str(invitacion)
    )

    (destinatario, subject, texto), = smtp.enviados
    assert destinatario == "ana@kiosco.test"
    assert subject == "Tu acceso a Véktor está listo"
    assert str(invitacion) in texto
    assert marcados == [
        (str(solicitud.id), "decision_email_status", EmailNotificationStatus.SENT.value)
    ]


def test_aprobacion_sin_token_es_fallo_definitivo_sin_reintento(
    monkeypatch: pytest.MonkeyPatch,
    smtp: type[_SMTPEspia],
    marcados: list[tuple[str, str, str]],
) -> None:
    """Reintentar un error de programación no lo arregla: se marca failed y listo."""
    solicitud = _solicitud(status=AccessRequestStatus.APPROVED.value)
    _cargar(monkeypatch, solicitud)

    worker.notify_access_request_decision.__wrapped__(
        str(solicitud.id), AccessRequestStatus.APPROVED.value, None
    )

    assert smtp.enviados == []
    assert marcados == [
        (
            str(solicitud.id),
            "decision_email_status",
            EmailNotificationStatus.FAILED.value,
        )
    ]


def test_rechazo_no_transcribe_el_motivo_interno(
    monkeypatch: pytest.MonkeyPatch,
    smtp: type[_SMTPEspia],
    marcados: list[tuple[str, str, str]],
) -> None:
    solicitud = _solicitud(
        status=AccessRequestStatus.REJECTED.value,
        rejection_reason="parece spam de un competidor",
    )
    _cargar(monkeypatch, solicitud)

    worker.notify_access_request_decision.__wrapped__(
        str(solicitud.id), AccessRequestStatus.REJECTED.value, None
    )

    (_, _, texto), = smtp.enviados
    assert "spam" not in texto.lower()


# ── Cuenta existente ─────────────────────────────────────────────────────────


def test_cuenta_existente_le_escribe_al_dueño_de_la_casilla(
    smtp: type[_SMTPEspia], marcados: list[tuple[str, str, str]]
) -> None:
    worker.notify_access_request_account_exists.__wrapped__("ana@kiosco.test")

    (destinatario, subject, _), = smtp.enviados
    assert destinatario == "ana@kiosco.test"
    assert subject == "Ya tenés una cuenta en Véktor"
    assert marcados == []


# ── Escotilla de desarrollo contra la base ───────────────────────────────────


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def sesion(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s


@pytest.fixture
def sesion_del_worker(
    monkeypatch: pytest.MonkeyPatch, sesion: AsyncSession
) -> None:
    @asynccontextmanager
    async def _stub() -> AsyncIterator[AsyncSession]:
        yield sesion

    monkeypatch.setattr(worker, "_sesion", _stub)


async def test_autoverificar_deja_la_solicitud_lista_para_revisar(
    sesion: AsyncSession, sesion_del_worker: None
) -> None:
    solicitud = _solicitud()
    sesion.add(solicitud)
    await sesion.commit()

    assert await worker._autoverificar(str(solicitud.id), str(uuid.uuid4())) is True

    assert solicitud.status == AccessRequestStatus.PENDING.value
    assert solicitud.email_verified_at is not None
    assert solicitud.verification_email_status == EmailNotificationStatus.SENT.value


async def test_autoverificar_es_idempotente(
    sesion: AsyncSession, sesion_del_worker: None
) -> None:
    """Reejecutar la tarea no re-avisa al dueño ni pisa la fecha de verificación."""
    solicitud = _solicitud()
    sesion.add(solicitud)
    await sesion.commit()

    await worker._autoverificar(str(solicitud.id), str(uuid.uuid4()))
    verificada_en = solicitud.email_verified_at

    assert await worker._autoverificar(str(solicitud.id), str(uuid.uuid4())) is False
    assert solicitud.email_verified_at == verificada_en
