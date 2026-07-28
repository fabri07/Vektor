"""Test end-to-end del circuito de aprobación: solicitud → cuenta → primer login.

Es la única prueba que recorre el trámite entero de punta a punta —formulario
público, doble opt-in, aprobación manual, invitación, login y onboarding— sobre
SQLite y contra la API real. Cada test arranca del anterior a través de la cadena
de fixtures (``solicitud`` → ``verificada`` → ``aprobada`` → ``sesion``), así que
todos recorren el circuito hasta el punto que asertan.

Dos aserciones valen por sí solas y tienen test propio para que no se pierdan
dentro de otro:

1. ``test_crear_la_solicitud_no_acuna_ninguna_fila_de_cuenta`` — mandar el
   formulario NO acuña nada: cero ``Tenant``, ``User``, ``Subscription``,
   ``BusinessProfile`` y ``MomentumProfile``. Es la premisa entera del PR: si esto
   afloja, el registro abierto volvió por la ventana.
2. ``test_la_suscripcion_es_free_pese_a_la_intencion_premium`` — la solicitud pide
   ``requested_plan="premium"`` y la cuenta acuñada igual tiene una
   ``Subscription`` con ``plan_code == "FREE"``. La intención Premium es prioridad
   de revisión y trazabilidad comercial, nunca una suscripción paga: no hay
   circuito de cobro ni límites configurados. Esta aserción es lo que impide que
   alguien "arregle" eso después y termine regalando el plan pago.

Celery se intercepta en el ``.delay`` de cada tarea real (mismo patrón que
``app/tests/api/v1/test_contact.py``): así se asierta QUÉ se encoló sin que el
worker corra, y de paso se verifica que las tareas existen con el nombre que
``access_request_service._encolar`` resuelve por ``getattr``. Ningún mail se
manda: el SMTP vive del otro lado del ``.delay``.
"""

from __future__ import annotations

import unittest.mock
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services.access_request_service import (
    AccessRequestService,
    ApprovalResult,
)
from app.domain.access_request import AccessRequestStatus, RequestedPlan
from app.domain.verticals import Vertical
from app.jobs import access_request_worker, score_worker
from app.persistence.models.access_request import AccessRequest, AccessRequestToken
from app.persistence.models.auth_token import PasswordResetToken
from app.persistence.models.business import (
    BusinessProfile,
    BusinessSnapshot,
    MomentumProfile,
)
from app.persistence.models.tenant import Subscription, Tenant
from app.persistence.models.user import User

URL_SOLICITUDES = "/api/v1/access-requests"
URL_VERIFY = f"{URL_SOLICITUDES}/verify"
URL_RESET = "/api/v1/auth/reset-password"
URL_LOGIN = "/api/v1/auth/login"
URL_ME = "/api/v1/auth/me"
URL_ONBOARDING = "/api/v1/onboarding/submit"

#: Rubro que declara el solicitante…
VERTICAL_DECLARADO = Vertical.KIOSCO_ALMACEN
#: …y el que le asigna el dueño al aprobar. Que sean distintos es el punto de la
#: revisión manual: manda el asignado, no el declarado.
VERTICAL_ASIGNADO = Vertical.LIMPIEZA

#: Contraseña que define el usuario con el link de invitación.
PASSWORD_NUEVA = "Secure123"

#: Las cinco tablas que acuña un alta de cuenta.
TABLAS_DE_CUENTA: tuple[type, ...] = (
    Tenant,
    User,
    Subscription,
    BusinessProfile,
    MomentumProfile,
)

#: Ventas semanales del onboarding. El service las mensualiza con el factor 4.3.
VENTAS_SEMANALES = Decimal("350000")

_SOLICITUD: dict[str, Any] = {
    "full_name": "Ana Gómez",
    "email": "ana@limpieza.example.com",
    "phone": "+54 11 5555-1234",
    "business_name": "Limpieza Brillante",
    "requested_vertical": VERTICAL_DECLARADO.value,
    # La intención comercial que NO se convierte en suscripción paga.
    "requested_plan": RequestedPlan.PREMIUM.value,
    "years_operating": "2y_5y",
    "staff_size": "2_5",
    "monthly_revenue_band": "3m_10m",
    "main_concern": "MARGIN",
    "records_format": "planilla",
    "history_depth": "1y_3y",
    "can_share_files": "si_ordenados",
    "records_notes": None,
    "applicant_notes": None,
    "consent": True,
    "cta_source": "landing_hero",
    "website": "",
    "elapsed_ms": 9000,
}


@dataclass(frozen=True)
class Encolados:
    """Los ``.delay`` interceptados de las tareas que toca este circuito.

    ``cuenta_existente`` está para asertar que NO se llama: en este flujo el email
    no tiene cuenta previa.
    """

    verificacion: unittest.mock.MagicMock
    aviso_duenio: unittest.mock.MagicMock
    decision: unittest.mock.MagicMock
    cuenta_existente: unittest.mock.MagicMock
    score: unittest.mock.MagicMock


@pytest.fixture
def encolados() -> Iterator[Encolados]:
    """Intercepta el encolado de las 4 tareas del flujo + el recálculo de score.

    Se parchea el ``.delay`` de la tarea REAL, no la función ``_encolar``: así el
    test también verifica que cada tarea existe con el nombre de atributo que el
    servicio resuelve por ``getattr`` (si alguien renombra una, esto se entera).
    """
    with (
        unittest.mock.patch.object(
            access_request_worker.notify_access_request_verification, "delay"
        ) as verificacion,
        unittest.mock.patch.object(
            access_request_worker.notify_access_request_owner, "delay"
        ) as aviso_duenio,
        unittest.mock.patch.object(
            access_request_worker.notify_access_request_decision, "delay"
        ) as decision,
        unittest.mock.patch.object(
            access_request_worker.notify_access_request_account_exists, "delay"
        ) as cuenta_existente,
        unittest.mock.patch.object(
            score_worker.trigger_score_recalculation, "delay"
        ) as score,
    ):
        yield Encolados(
            verificacion=verificacion,
            aviso_duenio=aviso_duenio,
            decision=decision,
            cuenta_existente=cuenta_existente,
            score=score,
        )


async def _contar(session: AsyncSession, modelo: type) -> int:
    return int(
        (await session.execute(select(func.count()).select_from(modelo))).scalar_one()
    )


async def _la_solicitud(session: AsyncSession) -> AccessRequest:
    session.expire_all()
    return (await session.execute(select(AccessRequest))).scalars().one()


# ── Paso 1: el formulario público ─────────────────────────────────────────────


@pytest_asyncio.fixture
async def solicitud(
    client: AsyncClient, db_session: AsyncSession, encolados: Encolados
) -> AccessRequest:
    """Paso 1: el visitante manda el formulario pidiendo cuenta Premium."""
    res = await client.post(URL_SOLICITUDES, json=_SOLICITUD)
    assert res.status_code == 201, res.text
    return await _la_solicitud(db_session)


async def test_crear_la_solicitud_no_acuna_ninguna_fila_de_cuenta(
    solicitud: AccessRequest, db_session: AsyncSession
) -> None:
    """LA premisa del PR: una solicitud no es un alta.

    Si esta aserción se afloja, la feature entera dejó de existir aunque el resto
    de la suite siga verde.
    """
    assert solicitud.status == AccessRequestStatus.UNVERIFIED.value
    assert solicitud.requested_plan == RequestedPlan.PREMIUM.value

    for modelo in TABLAS_DE_CUENTA:
        assert await _contar(db_session, modelo) == 0, (
            f"La solicitud creó filas en {modelo.__name__}: la cuenta se acuña "
            "recién al aprobar."
        )


async def test_crear_la_solicitud_encola_el_doble_opt_in(
    solicitud: AccessRequest, db_session: AsyncSession, encolados: Encolados
) -> None:
    token = (await db_session.execute(select(AccessRequestToken))).scalars().one()
    encolados.verificacion.assert_called_once_with(
        str(solicitud.id), str(token.token_id)
    )
    encolados.cuenta_existente.assert_not_called()  # ese email no tenía cuenta


# ── Paso 2: doble opt-in ──────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def verificada(
    client: AsyncClient,
    db_session: AsyncSession,
    solicitud: AccessRequest,
    encolados: Encolados,
) -> AccessRequest:
    """Paso 2: el solicitante clickea el link del mail."""
    token = (
        await db_session.execute(
            select(AccessRequestToken).where(AccessRequestToken.used.is_(False))
        )
    ).scalar_one()
    res = await client.post(URL_VERIFY, json={"token": str(token.token_id)})
    assert res.status_code == 200, res.text
    return await _la_solicitud(db_session)


async def test_verificar_pone_la_solicitud_en_la_cola_sin_acunar_nada(
    verificada: AccessRequest, db_session: AsyncSession, encolados: Encolados
) -> None:
    """Confirmar el email mueve la solicitud a la cola; sigue sin haber cuenta."""
    assert verificada.status == AccessRequestStatus.PENDING.value
    assert verificada.email_verified_at is not None

    for modelo in TABLAS_DE_CUENTA:
        assert await _contar(db_session, modelo) == 0, modelo.__name__

    # Recién ahora el dueño se entera de que tiene algo para revisar.
    encolados.aviso_duenio.assert_called_once_with(str(verificada.id))


# ── Paso 3: la aprobación manual ──────────────────────────────────────────────


@pytest_asyncio.fixture
async def aprobada(
    db_session: AsyncSession, verificada: AccessRequest, encolados: Encolados
) -> ApprovalResult:
    """Paso 3: el dueño aprueba y le asigna el rubro (distinto del declarado)."""
    return await AccessRequestService(db_session).approve(
        verificada.id,
        vertical=VERTICAL_ASIGNADO,
        reviewer_user_id=None,
        via="script",
        notes="rubro corregido a limpieza",
    )


async def test_aprobar_acuna_exactamente_una_cuenta(
    aprobada: ApprovalResult, db_session: AsyncSession
) -> None:
    """Paso 4: las cinco filas de la cuenta, una de cada una, y nada más.

    ``BusinessSnapshot`` en 0 es parte de la aserción: los números financieros se
    piden en el primer login, no en el formulario público — aprobar no puede
    inventar un snapshot con datos que nadie dio.
    """
    assert aprobada.already_approved is False

    for modelo in TABLAS_DE_CUENTA:
        assert await _contar(db_session, modelo) == 1, modelo.__name__
    assert await _contar(db_session, BusinessSnapshot) == 0

    tenant = (await db_session.execute(select(Tenant))).scalars().one()
    assert tenant.status == "ACTIVE"
    assert tenant.tenant_id == aprobada.tenant_id

    usuario = (await db_session.execute(select(User))).scalars().one()
    assert usuario.role_code == "OWNER"
    assert usuario.is_active is True
    assert usuario.email == _SOLICITUD["email"]
    assert usuario.user_id == aprobada.user_id

    perfil = (await db_session.execute(select(BusinessProfile))).scalars().one()
    assert perfil.vertical_code == VERTICAL_ASIGNADO.value  # el asignado, no el pedido
    assert perfil.onboarding_completed is False
    assert perfil.custom_fields["main_concern"] == _SOLICITUD["main_concern"]


async def test_la_suscripcion_es_free_pese_a_la_intencion_premium(
    aprobada: ApprovalResult, db_session: AsyncSession
) -> None:
    """La intención Premium NO se copia a ``Subscription.plan_code``.

    El formulario pregunta si el visitante quiere cuenta gratuita o Premium, pero
    hoy no existe un Premium operativo: no hay cobro, no hay límites configurados
    ni features detrás de un flag. La intención se conserva en la solicitud para
    trazabilidad comercial y para priorizar la revisión; la suscripción que se
    acuña es FREE siempre. Copiar ``requested_plan`` a ``plan_code`` sería regalar
    un plan pago sin contrato detrás.
    """
    solicitud = await _la_solicitud(db_session)
    assert solicitud.requested_plan == RequestedPlan.PREMIUM.value

    suscripcion = (await db_session.execute(select(Subscription))).scalars().one()
    assert suscripcion.plan_code == "FREE"


async def test_aprobar_encola_la_decision_con_el_link_de_invitacion(
    aprobada: ApprovalResult, db_session: AsyncSession, encolados: Encolados
) -> None:
    # El `expire_all()` de `_la_solicitud` va ANTES de cargar la invitación: si
    # se expira un objeto ya cargado, el próximo atributo dispara un refresh
    # lazy que en async explota (`MissingGreenlet`) en vez de fallar el assert.
    solicitud = await _la_solicitud(db_session)
    invitacion = (
        (await db_session.execute(select(PasswordResetToken))).scalars().one()
    )
    assert invitacion.token_id == aprobada.invite_token_id
    assert invitacion.used is False

    encolados.decision.assert_called_once_with(
        str(solicitud.id),
        AccessRequestStatus.APPROVED.value,
        str(invitacion.token_id),
    )


# ── Paso 5: invitación → contraseña → primer login ────────────────────────────


@pytest_asyncio.fixture
async def sesion(
    client: AsyncClient, db_session: AsyncSession, aprobada: ApprovalResult
) -> dict[str, str]:
    """Paso 5: el usuario define su contraseña con la invitación y entra."""
    invitacion = (
        await db_session.execute(
            select(PasswordResetToken).where(PasswordResetToken.used.is_(False))
        )
    ).scalar_one()

    reset = await client.post(
        URL_RESET,
        json={"token": str(invitacion.token_id), "new_password": PASSWORD_NUEVA},
    )
    assert reset.status_code == 200, reset.text

    login = await client.post(
        URL_LOGIN, json={"email": _SOLICITUD["email"], "password": PASSWORD_NUEVA}
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def test_la_invitacion_deja_definir_password_y_entrar(
    sesion: dict[str, str], client: AsyncClient, db_session: AsyncSession
) -> None:
    """El usuario aprobado nunca eligió contraseña: la define con la invitación.

    Y el token queda consumido: el link del mail vale una sola vez.
    """
    invitacion = (
        (await db_session.execute(select(PasswordResetToken))).scalars().one()
    )
    await db_session.refresh(invitacion)
    assert invitacion.used is True

    me = await client.get(URL_ME, headers=sesion)
    assert me.status_code == 200, me.text
    cuerpo = me.json()
    assert cuerpo["email"] == _SOLICITUD["email"]
    assert cuerpo["role_code"] == "OWNER"
    assert cuerpo["subscription"]["plan_code"] == "FREE"
    # Todavía no cargó los números: el onboarding es el paso siguiente.
    assert cuerpo["onboarding_completed"] is False


# ── Paso 6: el onboarding del primer login (sin rubro) ────────────────────────


async def test_el_onboarding_del_primer_login_no_pide_el_rubro(
    sesion: dict[str, str],
    client: AsyncClient,
    db_session: AsyncSession,
    encolados: Encolados,
) -> None:
    """Los 6 números financieros van acá; el rubro ya lo fijó la aprobación.

    El payload NO manda ``vertical_code`` (el schema lo prohíbe con
    ``extra="forbid"``): el usuario recién aprobado no puede reescribir el rubro
    que decidió el dueño. Y ``main_concern`` tampoco viaja: la aprobación lo copió
    a ``custom_fields`` desde el screening y el snapshot lo toma de ahí.
    """
    res = await client.post(
        URL_ONBOARDING,
        json={
            "weekly_sales_estimate_ars": float(VENTAS_SEMANALES),
            "monthly_inventory_cost_ars": 180000,
            "monthly_fixed_expenses_ars": 80000,
            "cash_on_hand_ars": 150000,
            "product_count_estimate": 45,
            "supplier_count_estimate": 3,
        },
        headers=sesion,
    )
    assert res.status_code == 200, res.text

    me = await client.get(URL_ME, headers=sesion)
    assert me.status_code == 200
    assert me.json()["onboarding_completed"] is True

    db_session.expire_all()
    snapshot = (await db_session.execute(select(BusinessSnapshot))).scalars().one()
    assert snapshot.snapshot_version == "onboarding_v1"
    entradas = snapshot.raw_inputs_json
    assert entradas is not None
    # Viene del screening del formulario público, no de este body.
    assert entradas["main_concern"] == _SOLICITUD["main_concern"]
    assert entradas["vertical_code"] == VERTICAL_ASIGNADO.value

    perfil = (await db_session.execute(select(BusinessProfile))).scalars().one()
    assert perfil.heuristic_profile_version == f"{VERTICAL_ASIGNADO.value}:v1"
    assert perfil.monthly_sales_estimate_ars == VENTAS_SEMANALES * Decimal("4.3")

    encolados.score.assert_called_once_with(
        str(perfil.tenant_id), str(snapshot.id)
    )


async def test_el_onboarding_rechaza_un_vertical_code_en_el_body(
    sesion: dict[str, str], client: AsyncClient
) -> None:
    """Un bundle viejo que todavía manda el rubro se entera con un 422.

    Ignorarlo en silencio dejaría al usuario creyendo que eligió su rubro.
    """
    res = await client.post(
        URL_ONBOARDING,
        json={
            "weekly_sales_estimate_ars": 350000,
            "monthly_inventory_cost_ars": 180000,
            "monthly_fixed_expenses_ars": 80000,
            "cash_on_hand_ars": 150000,
            "product_count_estimate": 45,
            "supplier_count_estimate": 3,
            "vertical_code": VERTICAL_DECLARADO.value,
        },
        headers=sesion,
    )
    assert res.status_code == 422, res.text


# ── Paso 7: aprobar dos veces ─────────────────────────────────────────────────


async def test_aprobar_de_nuevo_no_acuna_un_segundo_tenant(
    aprobada: ApprovalResult, db_session: AsyncSession, encolados: Encolados
) -> None:
    """La doble aprobación pasa de verdad: el dueño decide por script Y por API.

    Re-aprobar devuelve la misma cuenta con ``already_approved=True`` y no toca
    nada: ni un segundo tenant, ni una invitación nueva, ni otro mail.
    """
    encolados.decision.reset_mock()

    segunda = await AccessRequestService(db_session).approve(
        (await _la_solicitud(db_session)).id,
        # Incluso cambiando el rubro: la cuenta ya está acuñada.
        vertical=VERTICAL_DECLARADO,
        reviewer_user_id=None,
        via="api",
        notes="otra vez",
    )

    assert segunda.already_approved is True
    assert segunda.tenant_id == aprobada.tenant_id
    assert segunda.user_id == aprobada.user_id
    assert segunda.invite_token_id is None

    for modelo in TABLAS_DE_CUENTA:
        assert await _contar(db_session, modelo) == 1, modelo.__name__
    assert await _contar(db_session, PasswordResetToken) == 1

    perfil = (await db_session.execute(select(BusinessProfile))).scalars().one()
    assert perfil.vertical_code == VERTICAL_ASIGNADO.value  # el de la primera
    encolados.decision.assert_not_called()  # no se re-mailea la bienvenida


async def test_aprobar_una_solicitud_inexistente_no_acuna_nada(
    verificada: AccessRequest, db_session: AsyncSession
) -> None:
    """Cierre del circuito: sin solicitud no hay cuenta (ni a medio crear)."""
    from app.application.services.access_request_service import (  # noqa: PLC0415
        AccessRequestNotFound,
    )

    with pytest.raises(AccessRequestNotFound):
        await AccessRequestService(db_session).approve(
            uuid.uuid4(),
            vertical=VERTICAL_ASIGNADO,
            reviewer_user_id=None,
            via="script",
            notes=None,
        )

    for modelo in TABLAS_DE_CUENTA:
        assert await _contar(db_session, modelo) == 0, modelo.__name__
