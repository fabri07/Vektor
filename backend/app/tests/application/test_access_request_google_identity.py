"""Aprobar una solicitud que vino por Google tiene que dejarla entrar con Google.

La solicitud guarda el ``google_subject`` (lo puebla el canje del prefill en
``api/v1/access_requests.py``), pero el ``UserAuthIdentity`` recién existe al
aprobar: es la primera vez que hay un ``user_id`` al que colgarlo. Sin ese
linkeo, el aprobado que pidió acceso con "Continuar con Google" vuelve a entrar
con Google, no encuentra su identidad y termina en el formulario de solicitud —
o tiene que usar el link de contraseña, que es justo la fricción que ese camino
venía a evitar.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import access_request_service as srv
from app.application.services.access_request_service import (
    AccessRequestGoogleIdentityTaken,
    AccessRequestInput,
    AccessRequestService,
    CreateOutcome,
)
from app.domain.access_request import RequestedPlan
from app.domain.verticals import Vertical
from app.persistence.models.access_request import AccessRequest, AccessRequestToken
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.persistence.models.user_auth_identity import UserAuthIdentity
from app.utils.security import hash_password

_SUBJECT = "google-sub-aprobacion"


@pytest.fixture(autouse=True)
def encolados(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    llamadas: list[tuple] = []
    monkeypatch.setattr(
        srv, "_encolar", lambda nombre, *args: llamadas.append((nombre, *args))
    )
    return llamadas


@pytest.fixture
def service(db_session: AsyncSession) -> AccessRequestService:
    return AccessRequestService(db_session)


def _input(**overrides: Any) -> AccessRequestInput:
    base: dict[str, Any] = {
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
        "records_notes": None,
        "applicant_notes": None,
        "cta_source": "google_login",
        "google_subject": _SUBJECT,
        "website": "",
        "elapsed_ms": 9000,
    }
    base.update(overrides)
    return AccessRequestInput(**base)


async def _crear_verificada(
    service: AccessRequestService, db_session: AsyncSession, **overrides: Any
) -> AccessRequest:
    solicitud, outcome = await service.create(_input(**overrides), ip_hash=None)
    assert outcome is CreateOutcome.CREATED
    assert solicitud is not None
    token = (
        (
            await db_session.execute(
                select(AccessRequestToken).where(
                    AccessRequestToken.access_request_id == solicitud.id,
                    AccessRequestToken.used.is_(False),
                )
            )
        )
        .scalars()
        .one()
    )
    await service.verify(str(token.token_id))
    return solicitud


async def _identidades(db_session: AsyncSession) -> list[UserAuthIdentity]:
    return list(
        (await db_session.execute(select(UserAuthIdentity))).scalars().all()
    )


async def test_approve_vincula_la_identidad_de_google(
    service: AccessRequestService, db_session: AsyncSession
) -> None:
    solicitud = await _crear_verificada(service, db_session)

    resultado = await service.approve(
        solicitud.id,
        vertical=Vertical.KIOSCO_ALMACEN,
        reviewer_user_id=None,
        via="script",
        notes=None,
    )

    identidades = await _identidades(db_session)
    assert len(identidades) == 1
    identidad = identidades[0]
    assert identidad.provider == "google"
    assert identidad.provider_subject == _SUBJECT
    # El email de la solicitud ES el que verificó Google (el canje del prefill
    # rechaza el par que no coincide con un 403).
    assert identidad.provider_email == solicitud.email
    assert identidad.user_id == resultado.user_id
    assert identidad.tenant_id == resultado.tenant_id
    # Nadie entró todavía: sellar un login que no ocurrió sería inventarlo.
    assert identidad.last_login_at is None


async def test_approve_sin_google_subject_no_crea_identidad(
    service: AccessRequestService, db_session: AsyncSession
) -> None:
    """El alta del formulario público a secas no inventa ninguna identidad."""
    solicitud = await _crear_verificada(service, db_session, google_subject=None)

    await service.approve(
        solicitud.id,
        vertical=Vertical.KIOSCO_ALMACEN,
        reviewer_user_id=None,
        via="script",
        notes=None,
    )

    assert await _identidades(db_session) == []


async def test_approve_audita_si_hubo_linkeo(
    service: AccessRequestService, db_session: AsyncSession
) -> None:
    solicitud = await _crear_verificada(service, db_session)
    await service.approve(
        solicitud.id,
        vertical=Vertical.KIOSCO_ALMACEN,
        reviewer_user_id=None,
        via="script",
        notes=None,
    )

    traza = (
        (
            await db_session.execute(
                select(DecisionAuditLog).where(
                    DecisionAuditLog.decision_type == srv.DECISION_APPROVED
                )
            )
        )
        .scalars()
        .one()
    )
    assert traza.decision_data["google_identity_linked"] is True


async def test_identidad_ya_vinculada_a_otro_usuario_es_conflicto(
    service: AccessRequestService, db_session: AsyncSession
) -> None:
    """Falla ANTES de acuñar: ni 500 por la unique, ni linkeo salteado en silencio.

    Pasa de verdad cuando la misma cuenta de Google abrió dos solicitudes con
    emails distintos y las dos llegan a aprobarse.
    """
    tenant = Tenant(
        tenant_id=uuid.uuid4(),
        legal_name="Otro Negocio",
        display_name="Otro Negocio",
        currency="ARS",
        pricing_reference_mode="MEP",
        status="ACTIVE",
    )
    db_session.add(tenant)
    await db_session.flush()
    usuario = User(
        tenant_id=tenant.tenant_id,
        email="ya.tiene@negocio.example.com",
        full_name="Titular Previo",
        password_hash=hash_password("Secure123"),
        role_code="OWNER",
        is_active=True,
    )
    db_session.add(usuario)
    await db_session.flush()
    db_session.add(
        UserAuthIdentity(
            tenant_id=tenant.tenant_id,
            user_id=usuario.user_id,
            provider="google",
            provider_subject=_SUBJECT,
            provider_email="ya.tiene@negocio.example.com",
        )
    )
    await db_session.commit()

    solicitud = await _crear_verificada(service, db_session)

    with pytest.raises(AccessRequestGoogleIdentityTaken):
        await service.approve(
            solicitud.id,
            vertical=Vertical.KIOSCO_ALMACEN,
            reviewer_user_id=None,
            via="script",
            notes=None,
        )

    # No se acuñó una segunda cuenta ni una segunda identidad.
    assert len(await _identidades(db_session)) == 1
    usuarios = (await db_session.execute(select(User))).scalars().all()
    assert [u.email for u in usuarios] == ["ya.tiene@negocio.example.com"]
