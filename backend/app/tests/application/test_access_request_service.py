"""Tests de `AccessRequestService` — la máquina de estados de las solicitudes.

Cubre los cinco puntos que el flujo no puede aflojar: neutralidad a enumeración
de cuentas, claim atómico del token, idempotencia de `approve()` (nunca dos
tenants), `plan_code` siempre FREE pese a `requested_plan="premium"` y
`onboarding_completed=False` al aprobar. Más el orden de la cola y los armadores
de email.

El encolado se intercepta con una fixture autouse (`encolados`): además de
registrar las llamadas, verifica que en el momento de encolar la sesión ya no
tiene nada pendiente — o sea, que se commiteó ANTES de tocar el email (regla
cardinal de `app/jobs/contact_lead_worker.py`).
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import access_request_service as srv
from app.application.services.access_request_service import (
    AccessRequestEmailTaken,
    AccessRequestInput,
    AccessRequestInvalidTransition,
    AccessRequestNotApprovable,
    AccessRequestNotFound,
    AccessRequestService,
    CreateOutcome,
    build_account_exists_email,
    build_decision_email,
    build_owner_notification_email,
    build_verification_email,
)
from app.domain.access_request import (
    ACCESS_REQUEST_TOKEN_TTL_HOURS,
    AccessRequestStatus,
    RequestedPlan,
)
from app.domain.contact_lead import DEDUP_WINDOW_SECONDS
from app.domain.verticals import Vertical
from app.persistence.models.access_request import AccessRequest, AccessRequestToken
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.auth_token import PasswordResetToken
from app.persistence.models.business import BusinessProfile, MomentumProfile
from app.persistence.models.tenant import Subscription, Tenant
from app.persistence.models.user import User
from app.utils.security import hash_password


@pytest.fixture
def encolados(monkeypatch, db_session: AsyncSession) -> list[tuple]:
    """Intercepta el encolado y prueba de paso la REGLA CARDINAL.

    Con una transacción todavía abierta en el momento de encolar, el email podría
    salir por una solicitud que después se pierde en un rollback. ``flush()`` NO
    alcanza: lo que tiene que haber pasado es ``commit()``.

    Única excepción: el aviso "ya tenés cuenta" no persiste NADA (esa es toda la
    gracia de la neutralidad a enumeración), así que no hay nada que commitear —
    la transacción abierta ahí es la del SELECT que buscó al usuario.
    """
    llamadas: list[tuple] = []

    def _fake(nombre_tarea: str, *args: object) -> None:
        assert (
            not db_session.in_transaction()
            or nombre_tarea == srv.TASK_CUENTA_EXISTENTE
        ), (
            f"se encoló '{nombre_tarea}' sin commitear: hay que persistir ANTES "
            "de tocar el email"
        )
        llamadas.append((nombre_tarea, *args))

    monkeypatch.setattr(srv, "_encolar", _fake)
    return llamadas


@pytest.fixture
def service(db_session: AsyncSession) -> AccessRequestService:
    return AccessRequestService(db_session)


def _input(**overrides) -> AccessRequestInput:
    base = {
        "full_name": "Ana Gómez",
        "email": "Ana@Kiosco.EXAMPLE.com",
        "phone": "+54 11 5555-1234",
        "business_name": "Kiosco El Rápido",
        "requested_vertical": Vertical.KIOSCO_ALMACEN.value,
        "vertical_other_text": None,
        "requested_plan": RequestedPlan.FREE.value,
        "years_operating": "2y_5y",
        "staff_size": "2_5",
        "monthly_revenue_band": "3m_10m",
        "main_concern": "MARGIN",
        "records_format": "planilla",
        "history_depth": "1y_3y",
        "can_share_files": "si_ordenados",
        "records_notes": None,
        "applicant_notes": None,
        "cta_source": "landing",
        "website": "",
        "elapsed_ms": 9000,
    }
    base.update(overrides)
    return AccessRequestInput(**base)  # type: ignore[arg-type]


async def _contar(session: AsyncSession, modelo) -> int:
    return int(
        (await session.execute(select(func.count()).select_from(modelo))).scalar_one()
    )


async def _token_vigente(session: AsyncSession, request_id: uuid.UUID) -> AccessRequestToken:
    return (
        (
            await session.execute(
                select(AccessRequestToken).where(
                    AccessRequestToken.access_request_id == request_id,
                    AccessRequestToken.used.is_(False),
                )
            )
        )
        .scalars()
        .one()
    )


async def _crear_verificada(
    service: AccessRequestService,
    db_session: AsyncSession,
    encolados: list[tuple],
    **overrides,
) -> AccessRequest:
    solicitud, outcome = await service.create(_input(**overrides), ip_hash=None)
    assert outcome is CreateOutcome.CREATED
    assert solicitud is not None
    token = await _token_vigente(db_session, solicitud.id)
    await service.verify(str(token.token_id))
    encolados.clear()
    return solicitud


# ── create() ─────────────────────────────────────────────────────────────────


async def test_create_persiste_normaliza_y_encola_verificacion(
    service: AccessRequestService, db_session: AsyncSession, encolados: list[tuple]
):
    solicitud, outcome = await service.create(_input(), ip_hash="abc")

    assert outcome is CreateOutcome.CREATED
    assert solicitud is not None
    assert await _contar(db_session, AccessRequest) == 1
    assert solicitud.email == "ana@kiosco.example.com"  # normalizado
    assert solicitud.phone == "+541155551234"
    assert solicitud.status == AccessRequestStatus.UNVERIFIED.value
    assert solicitud.email_verified_at is None
    assert solicitud.consent_version  # el backend es autoritativo
    assert solicitud.ip_hash == "abc"

    token = await _token_vigente(db_session, solicitud.id)
    assert encolados == [(srv.TASK_VERIFICACION, str(solicitud.id), str(token.token_id))]


async def test_create_no_acuna_nada_de_la_cuenta(
    service: AccessRequestService, db_session: AsyncSession, encolados: list[tuple]
):
    """Una solicitud NO es un alta: no aparece ni una fila del acuñado."""
    await service.create(_input(), ip_hash=None)

    for modelo in (Tenant, User, Subscription, BusinessProfile, MomentumProfile):
        assert await _contar(db_session, modelo) == 0, modelo.__name__


@pytest.mark.parametrize(
    "override",
    [{"website": "http://spam.example"}, {"elapsed_ms": 200}],
    ids=["honeypot", "demasiado_rapido"],
)
async def test_create_bot_devuelve_exito_sin_persistir(
    service: AccessRequestService,
    db_session: AsyncSession,
    encolados: list[tuple],
    override: dict,
):
    solicitud, outcome = await service.create(_input(**override), ip_hash=None)

    assert (solicitud, outcome) == (None, CreateOutcome.BOT_DISCARDED)
    assert await _contar(db_session, AccessRequest) == 0
    assert encolados == []


async def test_create_con_email_que_ya_tiene_cuenta_es_neutro(
    service: AccessRequestService,
    db_session: AsyncSession,
    encolados: list[tuple],
    sample_user: User,
):
    """Mismo resultado que un alta nueva: no persiste, no revela, avisa por mail."""
    solicitud, outcome = await service.create(
        _input(email=sample_user.email.upper()), ip_hash=None
    )

    assert solicitud is None
    assert outcome is CreateOutcome.ACCOUNT_EXISTS
    assert await _contar(db_session, AccessRequest) == 0
    assert encolados == [(srv.TASK_CUENTA_EXISTENTE, sample_user.email)]


async def test_create_es_neutro_con_el_mismo_email_en_dos_tenants(
    service: AccessRequestService,
    db_session: AsyncSession,
    encolados: list[tuple],
    sample_user: User,
    second_tenant: Tenant,
):
    """`users.email` NO es único global: dos filas no pueden volverse un 500.

    El unique es `uq_users_tenant_email` (por tenant) y `POST /users` valida
    per-tenant, así que un OWNER puede dar de alta una sub-cuenta con un email
    que ya es OWNER de otro tenant. Con `scalar_one_or_none()` eso era
    `MultipleResultsFound` → 500 en el endpoint público: 500 para ese email y
    201 para todos los demás, o sea el oráculo de enumeración que este flujo
    existe para eliminar.
    """
    db_session.add(
        User(
            tenant_id=second_tenant.tenant_id,
            email=sample_user.email,
            full_name="Homónimo",
            password_hash=hash_password("Secure123"),
            role_code="ADMIN",
        )
    )
    await db_session.commit()

    solicitud, outcome = await service.create(
        _input(email=sample_user.email), ip_hash=None
    )

    assert (solicitud, outcome) == (None, CreateOutcome.ACCOUNT_EXISTS)
    assert await _contar(db_session, AccessRequest) == 0
    assert encolados == [(srv.TASK_CUENTA_EXISTENTE, sample_user.email)]


async def test_create_duplicado_no_crea_una_segunda_fila(
    service: AccessRequestService, db_session: AsyncSession, encolados: list[tuple]
):
    primera, _ = await service.create(_input(), ip_hash=None)
    encolados.clear()

    segunda, outcome = await service.create(_input(), ip_hash=None)

    assert outcome is CreateOutcome.DUPLICATE_OPEN
    assert segunda is not None and primera is not None
    assert segunda.id == primera.id
    assert await _contar(db_session, AccessRequest) == 1
    assert await _contar(db_session, AccessRequestToken) == 1
    assert encolados == []  # dentro del cooldown no se remaila


async def test_create_reemite_el_token_pasado_el_cooldown(
    service: AccessRequestService, db_session: AsyncSession, encolados: list[tuple]
):
    primera, _ = await service.create(_input(), ip_hash=None)
    assert primera is not None
    viejo = await _token_vigente(db_session, primera.id)
    # Envejecemos el token emitido para salir de la ventana de dedup.
    await db_session.execute(
        update(AccessRequestToken)
        .where(AccessRequestToken.token_id == viejo.token_id)
        .values(
            created_at=datetime.now(UTC) - timedelta(seconds=DEDUP_WINDOW_SECONDS + 60)
        )
    )
    await db_session.commit()
    encolados.clear()

    misma, outcome = await service.create(_input(), ip_hash=None)

    assert outcome is CreateOutcome.TOKEN_REISSUED
    assert misma is not None and misma.id == primera.id
    assert await _contar(db_session, AccessRequest) == 1
    assert await _contar(db_session, AccessRequestToken) == 2
    nuevo = await _token_vigente(db_session, primera.id)  # el viejo quedó usado
    assert nuevo.token_id != viejo.token_id
    assert encolados == [(srv.TASK_VERIFICACION, str(primera.id), str(nuevo.token_id))]


async def test_create_tras_un_rechazo_permite_una_solicitud_nueva(
    service: AccessRequestService, db_session: AsyncSession, encolados: list[tuple]
):
    """Los negocios cambian: una `rejected` no bloquea el email para siempre."""
    solicitud = await _crear_verificada(service, db_session, encolados)
    await service.reject(
        solicitud.id, reviewer_user_id=None, via="script", reason="fuera de rubro"
    )
    encolados.clear()

    nueva, outcome = await service.create(_input(), ip_hash=None)

    assert outcome is CreateOutcome.CREATED
    assert nueva is not None and nueva.id != solicitud.id
    assert await _contar(db_session, AccessRequest) == 2


# ── verify() ─────────────────────────────────────────────────────────────────


async def test_verify_pasa_a_pending_consume_el_token_y_avisa_al_duenio(
    service: AccessRequestService, db_session: AsyncSession, encolados: list[tuple]
):
    solicitud, _ = await service.create(_input(), ip_hash=None)
    assert solicitud is not None
    token = await _token_vigente(db_session, solicitud.id)
    encolados.clear()

    verificada = await service.verify(str(token.token_id))

    assert verificada.id == solicitud.id
    assert verificada.status == AccessRequestStatus.PENDING.value
    assert verificada.email_verified_at is not None
    consumido = await db_session.get(AccessRequestToken, token.token_id)
    await db_session.refresh(consumido)
    assert consumido is not None and consumido.used is True
    assert encolados == [(srv.TASK_AVISO_DUENIO, str(solicitud.id))]


@pytest.mark.parametrize(
    "token_str", ["no-es-un-uuid", str(uuid.uuid4())], ids=["basura", "inexistente"]
)
async def test_verify_token_invalido(service: AccessRequestService, token_str: str):
    with pytest.raises(HTTPException) as exc:
        await service.verify(token_str)
    assert exc.value.status_code == 400
    assert exc.value.detail == "token_invalido_o_expirado"


async def test_verify_token_vencido_no_verifica_nada(
    service: AccessRequestService, db_session: AsyncSession, encolados: list[tuple]
):
    solicitud, _ = await service.create(_input(), ip_hash=None)
    assert solicitud is not None
    token = await _token_vigente(db_session, solicitud.id)
    await db_session.execute(
        update(AccessRequestToken)
        .where(AccessRequestToken.token_id == token.token_id)
        .values(expires_at=datetime.now(UTC) - timedelta(hours=1))
    )
    await db_session.commit()
    encolados.clear()

    with pytest.raises(HTTPException) as exc:
        await service.verify(str(token.token_id))

    assert exc.value.detail == "token_invalido_o_expirado"
    await db_session.refresh(solicitud)
    assert solicitud.status == AccessRequestStatus.UNVERIFIED.value
    assert encolados == []


async def test_verify_dos_veces_es_idempotente_y_avisa_una_sola_vez(
    service: AccessRequestService, db_session: AsyncSession, encolados: list[tuple]
):
    """Doble click en el mail / prefetcher de links: 200 las dos veces."""
    solicitud, _ = await service.create(_input(), ip_hash=None)
    assert solicitud is not None
    token = await _token_vigente(db_session, solicitud.id)
    encolados.clear()

    primera = await service.verify(str(token.token_id))
    verificado_en = primera.email_verified_at
    segunda = await service.verify(str(token.token_id))

    assert segunda.id == solicitud.id
    assert segunda.status == AccessRequestStatus.PENDING.value
    assert segunda.email_verified_at == verificado_en  # no se pisa
    assert encolados == [(srv.TASK_AVISO_DUENIO, str(solicitud.id))]


# ── resend_verification() ────────────────────────────────────────────────────


async def test_resend_email_desconocido_es_noop_silencioso(
    service: AccessRequestService, encolados: list[tuple]
):
    await service.resend_verification("nadie@example.com")
    assert encolados == []


async def test_resend_dentro_del_cooldown_no_remaila(
    service: AccessRequestService, encolados: list[tuple]
):
    await service.create(_input(), ip_hash=None)
    encolados.clear()

    await service.resend_verification("ana@kiosco.example.com")

    assert encolados == []


async def test_resend_pasado_el_cooldown_reemite(
    service: AccessRequestService, db_session: AsyncSession, encolados: list[tuple]
):
    solicitud, _ = await service.create(_input(), ip_hash=None)
    assert solicitud is not None
    await db_session.execute(
        update(AccessRequestToken).values(
            created_at=datetime.now(UTC) - timedelta(seconds=DEDUP_WINDOW_SECONDS + 60)
        )
    )
    await db_session.commit()
    encolados.clear()

    await service.resend_verification("ANA@kiosco.example.com")

    nuevo = await _token_vigente(db_session, solicitud.id)
    assert encolados == [(srv.TASK_VERIFICACION, str(solicitud.id), str(nuevo.token_id))]


async def test_resend_de_una_solicitud_ya_verificada_es_noop(
    service: AccessRequestService, db_session: AsyncSession, encolados: list[tuple]
):
    await _crear_verificada(service, db_session, encolados)

    await service.resend_verification("ana@kiosco.example.com")

    assert encolados == []


# ── list_requests() — el orden de la cola ────────────────────────────────────


async def test_la_cola_pone_premium_primero_y_verificada_mas_antigua(
    service: AccessRequestService, db_session: AsyncSession, encolados: list[tuple]
):
    """Y una `unverified` Premium NO desplaza a una verificada: ni aparece."""
    ahora = datetime.now(UTC)

    async def _sembrar(
        email: str,
        plan: RequestedPlan,
        status: AccessRequestStatus,
        verificada_hace: int | None,
        **extra,
    ) -> uuid.UUID:
        fila = AccessRequest(
            full_name="X",
            email=email,
            business_name="N",
            requested_vertical=Vertical.LIMPIEZA.value,
            requested_plan=plan.value,
            years_operating="gt_5y",
            staff_size="solo",
            monthly_revenue_band="no_contesta",
            main_concern="CASH",
            records_format="papel",
            history_depth="ninguno",
            can_share_files="no",
            status=status.value,
            email_verified_at=(
                None if verificada_hace is None else ahora - timedelta(days=verificada_hace)
            ),
            consent_version="v1",
            consent_accepted_at=ahora,
            **extra,
        )
        db_session.add(fila)
        await db_session.flush()
        return fila.id

    premium_nueva = await _sembrar(
        "p1@e.com", RequestedPlan.PREMIUM, AccessRequestStatus.PENDING, 1
    )
    premium_vieja = await _sembrar(
        "p2@e.com", RequestedPlan.PREMIUM, AccessRequestStatus.WAITLIST, 9
    )
    free_vieja = await _sembrar(
        "f1@e.com", RequestedPlan.FREE, AccessRequestStatus.PENDING, 30
    )
    await _sembrar("p3@e.com", RequestedPlan.PREMIUM, AccessRequestStatus.UNVERIFIED, None)
    # Terminales: ni la aprobada ni la rechazada vuelven a la cola.
    await _sembrar(
        "a1@e.com",
        RequestedPlan.PREMIUM,
        AccessRequestStatus.APPROVED,
        2,
        assigned_vertical_code=Vertical.LIMPIEZA.value,
    )
    await _sembrar("r1@e.com", RequestedPlan.PREMIUM, AccessRequestStatus.REJECTED, 3)

    filas, total = await service.list_requests(limit=50, offset=0)

    assert [f.id for f in filas] == [premium_vieja, premium_nueva, free_vieja]
    assert total == 3


async def test_list_requests_filtra_por_estado_y_por_plan(
    service: AccessRequestService, db_session: AsyncSession, encolados: list[tuple]
):
    solicitud = await _crear_verificada(
        service, db_session, encolados, requested_plan=RequestedPlan.PREMIUM.value
    )
    await service.create(_input(email="otra@e.com"), ip_hash=None)

    solo_premium, total_premium = await service.list_requests(
        requested_plan=RequestedPlan.PREMIUM, limit=50, offset=0
    )
    sin_verificar, total_sin = await service.list_requests(
        status=AccessRequestStatus.UNVERIFIED, limit=50, offset=0
    )

    assert [f.id for f in solo_premium] == [solicitud.id] and total_premium == 1
    assert [f.email for f in sin_verificar] == ["otra@e.com"] and total_sin == 1


async def test_get_devuelve_none_si_no_existe(service: AccessRequestService):
    assert await service.get(uuid.uuid4()) is None


# ── approve() ────────────────────────────────────────────────────────────────


async def test_approve_acuna_la_cuenta_con_plan_free_pese_a_pedir_premium(
    service: AccessRequestService, db_session: AsyncSession, encolados: list[tuple]
):
    solicitud = await _crear_verificada(
        service,
        db_session,
        encolados,
        requested_plan=RequestedPlan.PREMIUM.value,
        requested_vertical=Vertical.KIOSCO_ALMACEN.value,
    )

    resultado = await service.approve(
        solicitud.id,
        vertical=Vertical.LIMPIEZA,  # el dueño corrige el rubro declarado
        reviewer_user_id=None,
        via="script",
        notes="compatible",
    )

    assert resultado.already_approved is False
    assert await _contar(db_session, Tenant) == 1
    assert await _contar(db_session, MomentumProfile) == 1

    suscripcion = (await db_session.execute(select(Subscription))).scalars().one()
    assert suscripcion.plan_code == "FREE"  # la intención Premium NO se copia

    usuario = (await db_session.execute(select(User))).scalars().one()
    assert usuario.role_code == "OWNER"
    assert usuario.is_active is True
    assert usuario.email == solicitud.email

    perfil = (await db_session.execute(select(BusinessProfile))).scalars().one()
    assert perfil.vertical_code == Vertical.LIMPIEZA.value  # el asignado, no el pedido
    assert perfil.onboarding_completed is False
    assert perfil.custom_fields["main_concern"] == "MARGIN"
    assert perfil.custom_fields["access_request_id"] == str(solicitud.id)

    await db_session.refresh(solicitud)
    assert solicitud.status == AccessRequestStatus.APPROVED.value
    assert solicitud.assigned_vertical_code == Vertical.LIMPIEZA.value
    assert solicitud.approved_tenant_id == resultado.tenant_id
    assert solicitud.approved_user_id == resultado.user_id
    assert solicitud.reviewed_via == "script"
    assert solicitud.review_notes == "compatible"
    assert solicitud.requested_plan == RequestedPlan.PREMIUM.value  # trazabilidad

    invitacion = (await db_session.execute(select(PasswordResetToken))).scalars().one()
    assert invitacion.token_id == resultado.invite_token_id
    assert invitacion.used is False
    assert encolados == [
        (
            srv.TASK_DECISION,
            str(solicitud.id),
            AccessRequestStatus.APPROVED.value,
            str(invitacion.token_id),
        )
    ]


async def test_approve_dos_veces_no_acuna_un_segundo_tenant(
    service: AccessRequestService, db_session: AsyncSession, encolados: list[tuple]
):
    solicitud = await _crear_verificada(service, db_session, encolados)
    primera = await service.approve(
        solicitud.id,
        vertical=Vertical.KIOSCO_ALMACEN,
        reviewer_user_id=None,
        via="script",
        notes=None,
    )
    encolados.clear()

    segunda = await service.approve(
        solicitud.id,
        vertical=Vertical.LIMPIEZA,  # aunque cambie el vertical, no re-acuña
        reviewer_user_id=None,
        via="api",
        notes="otra vez",
    )

    assert segunda.already_approved is True
    assert segunda.tenant_id == primera.tenant_id
    assert segunda.user_id == primera.user_id
    assert segunda.invite_token_id is None
    assert await _contar(db_session, Tenant) == 1
    assert await _contar(db_session, User) == 1
    assert await _contar(db_session, PasswordResetToken) == 1
    assert encolados == []  # no se re-mailea la bienvenida
    await db_session.refresh(solicitud)
    assert solicitud.assigned_vertical_code == Vertical.KIOSCO_ALMACEN.value


async def test_approve_desde_waitlist_funciona(
    service: AccessRequestService, db_session: AsyncSession, encolados: list[tuple]
):
    solicitud = await _crear_verificada(service, db_session, encolados)
    await service.waitlist(
        solicitud.id, reviewer_user_id=None, via="script", notes="sin cupo"
    )

    resultado = await service.approve(
        solicitud.id,
        vertical=Vertical.DECORACION_HOGAR,
        reviewer_user_id=None,
        via="script",
        notes=None,
    )

    assert resultado.already_approved is False
    assert resultado.tenant_id is not None


@pytest.mark.parametrize(
    "estado",
    [
        AccessRequestStatus.UNVERIFIED,
        AccessRequestStatus.REJECTED,
        AccessRequestStatus.EXPIRED,
    ],
)
async def test_approve_de_un_estado_no_aprobable_levanta(
    service: AccessRequestService,
    db_session: AsyncSession,
    encolados: list[tuple],
    estado: AccessRequestStatus,
):
    solicitud, _ = await service.create(_input(), ip_hash=None)
    assert solicitud is not None
    solicitud.status = estado.value
    await db_session.commit()

    with pytest.raises(AccessRequestNotApprovable):
        await service.approve(
            solicitud.id,
            vertical=Vertical.KIOSCO_ALMACEN,
            reviewer_user_id=None,
            via="api",
            notes=None,
        )

    assert await _contar(db_session, Tenant) == 0


async def test_approve_con_email_tomado_en_el_medio_no_acuna(
    service: AccessRequestService,
    db_session: AsyncSession,
    encolados: list[tuple],
    sample_tenant: Tenant,
):
    solicitud = await _crear_verificada(service, db_session, encolados)
    db_session.add(
        User(
            tenant_id=sample_tenant.tenant_id,
            email=solicitud.email,
            full_name="Impostor",
            password_hash=hash_password("Secure123"),
            role_code="OWNER",
        )
    )
    await db_session.commit()

    with pytest.raises(AccessRequestEmailTaken):
        await service.approve(
            solicitud.id,
            vertical=Vertical.KIOSCO_ALMACEN,
            reviewer_user_id=None,
            via="api",
            notes=None,
        )

    assert await _contar(db_session, Tenant) == 1  # solo el del fixture


async def test_no_se_puede_aprobar_una_unverified_pasada_por_waitlist(
    service: AccessRequestService, db_session: AsyncSession, encolados: list[tuple]
):
    """Bypass del doble opt-in: `waitlist` es legal desde `unverified` Y es aprobable.

    Encadenadas, acuñarían un tenant contra un email que nadie confirmó. La
    guardia que lo corta es `email_verified_at is None` en `approve()`, no el
    estado.
    """
    solicitud, _ = await service.create(_input(), ip_hash=None)
    assert solicitud is not None
    await service.waitlist(
        solicitud.id, reviewer_user_id=None, via="api", notes="parece serio"
    )
    await db_session.refresh(solicitud)
    assert solicitud.status == AccessRequestStatus.WAITLIST.value  # estado aprobable
    assert solicitud.email_verified_at is None  # …pero nadie confirmó el email

    with pytest.raises(AccessRequestNotApprovable, match="doble opt-in"):
        await service.approve(
            solicitud.id,
            vertical=Vertical.KIOSCO_ALMACEN,
            reviewer_user_id=None,
            via="api",
            notes=None,
        )

    assert await _contar(db_session, Tenant) == 0
    assert await _contar(db_session, User) == 0


async def test_verificar_despues_de_waitlist_sella_el_email_y_deja_aprobar(
    service: AccessRequestService, db_session: AsyncSession, encolados: list[tuple]
):
    """La cadena completa: create → waitlist → verify → approve.

    El dueño puede postergar una solicitud ANTES de que el usuario clickee el
    link (triar spam sin esperar el opt-in es legítimo). Cuando después clickea,
    el doble opt-in ocurrió igual y `verify()` tiene que sellar
    `email_verified_at` aunque el estado ya no sea `unverified` — si no, la
    guardia de `approve()` deja la solicitud IRRECUPERABLE: no hay resend, no hay
    solicitud nueva (cae en DUPLICATE_OPEN) y el segundo click da 400.
    """
    solicitud, _ = await service.create(_input(), ip_hash=None)
    assert solicitud is not None
    token = await _token_vigente(db_session, solicitud.id)

    await service.waitlist(
        solicitud.id, reviewer_user_id=None, via="api", notes="a revisar"
    )
    encolados.clear()

    verificada = await service.verify(str(token.token_id))

    assert verificada.email_verified_at is not None  # el hecho quedó sellado
    assert verificada.status == AccessRequestStatus.WAITLIST.value  # sin re-abrir
    assert encolados == []  # el dueño ya la revisó: no se le vuelve a avisar

    resultado = await service.approve(
        solicitud.id,
        vertical=Vertical.KIOSCO_ALMACEN,
        reviewer_user_id=None,
        via="api",
        notes=None,
    )

    assert resultado.already_approved is False
    assert resultado.tenant_id is not None
    assert await _contar(db_session, Tenant) == 1


async def test_un_segundo_click_tras_verificar_desde_waitlist_no_da_400(
    service: AccessRequestService, db_session: AsyncSession, encolados: list[tuple]
):
    """El link del mail lo abren dos veces (prefetchers, escáneres): sigue siendo 200."""
    solicitud, _ = await service.create(_input(), ip_hash=None)
    assert solicitud is not None
    token = await _token_vigente(db_session, solicitud.id)
    await service.waitlist(solicitud.id, reviewer_user_id=None, via="api", notes=None)
    await service.verify(str(token.token_id))

    segunda = await service.verify(str(token.token_id))

    assert segunda.id == solicitud.id
    assert segunda.status == AccessRequestStatus.WAITLIST.value


async def test_approve_de_una_solicitud_inexistente(service: AccessRequestService):
    with pytest.raises(AccessRequestNotFound):
        await service.approve(
            uuid.uuid4(),
            vertical=Vertical.KIOSCO_ALMACEN,
            reviewer_user_id=None,
            via="api",
            notes=None,
        )


# ── reject() / waitlist() ────────────────────────────────────────────────────


async def test_reject_marca_el_motivo_y_notifica(
    service: AccessRequestService, db_session: AsyncSession, encolados: list[tuple]
):
    solicitud = await _crear_verificada(service, db_session, encolados)

    rechazada = await service.reject(
        solicitud.id,
        reviewer_user_id=None,
        via="api",
        reason="no es un rubro soportado",
    )

    assert rechazada.status == AccessRequestStatus.REJECTED.value
    assert rechazada.rejection_reason == "no es un rubro soportado"
    assert rechazada.reviewed_at is not None
    assert encolados == [
        (srv.TASK_DECISION, str(solicitud.id), AccessRequestStatus.REJECTED.value, None)
    ]


async def test_reject_sin_notificar_no_encola(
    service: AccessRequestService, db_session: AsyncSession, encolados: list[tuple]
):
    solicitud = await _crear_verificada(service, db_session, encolados)

    await service.reject(
        solicitud.id, reviewer_user_id=None, via="script", reason="spam", notify=False
    )

    assert encolados == []


async def test_reject_repetido_es_idempotente(
    service: AccessRequestService, db_session: AsyncSession, encolados: list[tuple]
):
    solicitud = await _crear_verificada(service, db_session, encolados)
    await service.reject(
        solicitud.id, reviewer_user_id=None, via="api", reason="motivo original"
    )
    encolados.clear()

    otra = await service.reject(
        solicitud.id, reviewer_user_id=None, via="api", reason="motivo nuevo"
    )

    assert otra.rejection_reason == "motivo original"  # no se pisa
    assert encolados == []


async def test_reject_exige_un_motivo(
    service: AccessRequestService, db_session: AsyncSession, encolados: list[tuple]
):
    solicitud = await _crear_verificada(service, db_session, encolados)

    with pytest.raises(ValueError, match="motivo"):
        await service.reject(solicitud.id, reviewer_user_id=None, via="api", reason="  x ")


async def test_no_se_puede_rechazar_ni_postergar_una_aprobada(
    service: AccessRequestService, db_session: AsyncSession, encolados: list[tuple]
):
    solicitud = await _crear_verificada(service, db_session, encolados)
    await service.approve(
        solicitud.id,
        vertical=Vertical.KIOSCO_ALMACEN,
        reviewer_user_id=None,
        via="script",
        notes=None,
    )

    with pytest.raises(AccessRequestInvalidTransition):
        await service.reject(
            solicitud.id, reviewer_user_id=None, via="api", reason="me arrepentí"
        )
    with pytest.raises(AccessRequestInvalidTransition):
        await service.waitlist(
            solicitud.id, reviewer_user_id=None, via="api", notes=None
        )


async def test_waitlist_no_es_terminal_y_es_idempotente(
    service: AccessRequestService, db_session: AsyncSession, encolados: list[tuple]
):
    solicitud = await _crear_verificada(service, db_session, encolados)

    postergada = await service.waitlist(
        solicitud.id, reviewer_user_id=None, via="api", notes="sin cupo"
    )
    assert postergada.status == AccessRequestStatus.WAITLIST.value
    assert encolados == [
        (srv.TASK_DECISION, str(solicitud.id), AccessRequestStatus.WAITLIST.value, None)
    ]
    encolados.clear()

    await service.waitlist(solicitud.id, reviewer_user_id=None, via="api", notes="otra")
    assert encolados == []  # repetir el mismo estado no re-notifica


# ── Auditoría (decision_audit_log) ───────────────────────────────────────────


async def _auditorias(session: AsyncSession) -> list[DecisionAuditLog]:
    return list(
        (
            await session.execute(
                select(DecisionAuditLog).order_by(DecisionAuditLog.created_at.asc())
            )
        )
        .scalars()
        .all()
    )


async def test_create_y_verify_se_auditan_sin_tenant(
    service: AccessRequestService, db_session: AsyncSession, encolados: list[tuple]
):
    """Las decisiones previas al acuñado no tienen tenant — por eso la columna es nullable."""
    solicitud = await _crear_verificada(service, db_session, encolados)

    filas = await _auditorias(db_session)
    assert [f.decision_type for f in filas] == [
        srv.DECISION_CREATED,
        srv.DECISION_VERIFIED,
    ]
    assert all(f.tenant_id is None for f in filas)
    assert all(f.actor_user_id is None for f in filas)
    assert all(f.decision_data["access_request_id"] == str(solicitud.id) for f in filas)
    assert filas[0].triggered_by == "public:access_request_form"
    assert filas[1].triggered_by == "public:access_request_verify"


async def test_approve_se_audita_con_el_tenant_recien_acunado(
    service: AccessRequestService,
    db_session: AsyncSession,
    encolados: list[tuple],
    sample_user: User,
):
    solicitud = await _crear_verificada(service, db_session, encolados)

    resultado = await service.approve(
        solicitud.id,
        vertical=Vertical.LIMPIEZA,
        reviewer_user_id=sample_user.user_id,
        via="api",
        notes="ok",
    )

    aprobacion = [
        f
        for f in await _auditorias(db_session)
        if f.decision_type == srv.DECISION_APPROVED
    ]
    assert len(aprobacion) == 1
    fila = aprobacion[0]
    # Sólo es posible porque la auditoría va en la MISMA transacción que acuñó
    # el tenant: si se escribiera después del commit, la FK no existiría todavía.
    assert fila.tenant_id == resultado.tenant_id
    assert fila.actor_user_id == sample_user.user_id
    assert fila.triggered_by == "api:access_request_review"
    assert fila.decision_data["assigned_vertical_code"] == Vertical.LIMPIEZA.value
    assert fila.decision_data["requested_plan"] == RequestedPlan.FREE.value


async def test_reject_y_waitlist_se_auditan_con_su_decision_type(
    service: AccessRequestService,
    db_session: AsyncSession,
    encolados: list[tuple],
    sample_user: User,
):
    postergada = await _crear_verificada(service, db_session, encolados)
    rechazada = await _crear_verificada(
        service, db_session, encolados, email="otra@e.com"
    )

    await service.waitlist(
        postergada.id, reviewer_user_id=sample_user.user_id, via="api", notes="sin cupo"
    )
    await service.reject(
        rechazada.id, reviewer_user_id=None, via="script", reason="fuera de rubro"
    )

    tipos = {
        f.decision_type: f
        for f in await _auditorias(db_session)
        if f.decision_type in (srv.DECISION_WAITLISTED, srv.DECISION_REJECTED)
    }
    assert set(tipos) == {srv.DECISION_WAITLISTED, srv.DECISION_REJECTED}
    assert tipos[srv.DECISION_WAITLISTED].tenant_id is None
    assert tipos[srv.DECISION_WAITLISTED].actor_user_id == sample_user.user_id
    assert tipos[srv.DECISION_REJECTED].triggered_by == "script:access_request_review"
    assert (
        tipos[srv.DECISION_REJECTED].decision_data["rejection_reason"]
        == "fuera de rubro"
    )


async def test_las_decisiones_idempotentes_no_duplican_la_auditoria(
    service: AccessRequestService, db_session: AsyncSession, encolados: list[tuple]
):
    solicitud = await _crear_verificada(service, db_session, encolados)
    for _ in range(2):
        await service.approve(
            solicitud.id,
            vertical=Vertical.KIOSCO_ALMACEN,
            reviewer_user_id=None,
            via="script",
            notes=None,
        )

    filas = [
        f
        for f in await _auditorias(db_session)
        if f.decision_type == srv.DECISION_APPROVED
    ]
    assert len(filas) == 1


# ── Armadores de email (puros) ───────────────────────────────────────────────


def _solicitud_falsa(**overrides) -> AccessRequest:
    campos = {
        "id": uuid.uuid4(),
        "full_name": "Ana Gómez",
        "email": "ana@kiosco.example.com",
        "phone": None,
        "business_name": "Kiosco El Rápido",
        "requested_vertical": Vertical.KIOSCO_ALMACEN.value,
        "vertical_other_text": None,
        "requested_plan": RequestedPlan.FREE.value,
        "years_operating": "2y_5y",
        "staff_size": "2_5",
        "monthly_revenue_band": "3m_10m",
        "main_concern": "MARGIN",
        "records_format": "planilla",
        "history_depth": "1y_3y",
        "can_share_files": "si_ordenados",
        "status": AccessRequestStatus.PENDING.value,
        "consent_version": "v1",
        "consent_accepted_at": datetime.now(UTC),
    }
    campos.update(overrides)
    return AccessRequest(**campos)


def test_build_verification_email_lleva_el_token_y_el_ttl():
    token = uuid.uuid4()
    subject, cuerpo_html, texto = build_verification_email(_solicitud_falsa(), token)

    assert "Véktor" in subject
    assert str(token) in cuerpo_html
    assert str(token) in texto  # el texto plano también trae el link
    assert str(ACCESS_REQUEST_TOKEN_TTL_HOURS) in cuerpo_html


def test_build_owner_notification_escapa_lo_que_escribio_el_visitante():
    """Regresión del bug de `build_lead_email`: acá `html` nunca es una local."""
    req = _solicitud_falsa(
        business_name="<script>alert(1)</script>", applicant_notes="a & b"
    )

    subject, cuerpo_html, texto = build_owner_notification_email(req)

    assert "<script>" not in cuerpo_html
    assert "&lt;script&gt;" in cuerpo_html
    assert "a &amp; b" in cuerpo_html
    assert "<script>alert(1)</script>" in subject  # el asunto no es HTML
    assert "a & b" in texto  # el texto plano va sin escapar, como corresponde


def test_build_owner_notification_marca_la_prioridad_premium():
    premium = _solicitud_falsa(requested_plan=RequestedPlan.PREMIUM.value)
    gratuita = _solicitud_falsa()

    subject_premium, html_premium, _ = build_owner_notification_email(premium)
    subject_free, html_free, _ = build_owner_notification_email(gratuita)

    assert subject_premium.startswith("[PRIORIDAD PREMIUM] ")
    assert "Cuenta Premium" in html_premium
    assert subject_free.startswith("Nueva solicitud de acceso")
    assert "Cuenta gratuita" in html_free


def test_build_decision_email_aprobado_lleva_el_link_de_invitacion():
    token = uuid.uuid4()
    subject, cuerpo_html, texto = build_decision_email(
        _solicitud_falsa(), decision=AccessRequestStatus.APPROVED, invite_token=token
    )

    assert "listo" in subject
    assert f"definir-password?token={token}" in cuerpo_html
    assert str(token) in texto


def test_build_decision_email_aprobado_sin_token_es_un_error():
    with pytest.raises(ValueError, match="token"):
        build_decision_email(
            _solicitud_falsa(),
            decision=AccessRequestStatus.APPROVED,
            invite_token=None,
        )


def test_build_decision_email_rechazo_no_transcribe_el_motivo_interno():
    req = _solicitud_falsa(rejection_reason="el tipo parece un chanta")

    _, cuerpo_html, texto = build_decision_email(
        req, decision=AccessRequestStatus.REJECTED, invite_token=None
    )

    assert "chanta" not in cuerpo_html
    assert "chanta" not in texto
    assert "definir-password" not in cuerpo_html  # sin CTA


def test_build_decision_email_waitlist_no_promete_fecha():
    subject, cuerpo_html, _ = build_decision_email(
        _solicitud_falsa(), decision=AccessRequestStatus.WAITLIST, invite_token=None
    )

    assert "lista de espera" in subject.lower()
    assert "definir-password" not in cuerpo_html


def test_build_decision_email_de_un_estado_sin_mail():
    with pytest.raises(ValueError, match="unverified"):
        build_decision_email(
            _solicitud_falsa(),
            decision=AccessRequestStatus.UNVERIFIED,
            invite_token=None,
        )


def test_build_account_exists_email_no_habla_del_negocio():
    subject, cuerpo_html, texto = build_account_exists_email("ana@kiosco.example.com")

    assert "cuenta" in subject.lower()
    assert "ana@kiosco.example.com" in cuerpo_html
    assert "/login" in cuerpo_html
    assert "solicitud" in texto.lower()


# ── Regla cardinal: encolar nunca rompe nada ─────────────────────────────────


def test_encolar_nunca_propaga_aunque_no_exista_el_worker():
    """`_encolar` real (sin mock): el worker es de la Task 10 y todavía no existe."""
    srv._encolar(srv.TASK_VERIFICACION, "x", "y")  # no debe levantar
