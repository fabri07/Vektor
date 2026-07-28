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

import importlib.util
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import access_request_service as srv
from app.application.services.access_request_service import (
    AccessRequestGoogleIdentityTaken,
    AccessRequestInput,
    AccessRequestService,
    CreateOutcome,
)
from app.application.services.tenant_provisioning import provision_tenant
from app.domain.access_request import RequestedPlan
from app.domain.verticals import Vertical
from app.persistence.models.access_request import AccessRequest, AccessRequestToken
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.persistence.models.user_auth_identity import UserAuthIdentity
from app.utils.security import hash_password

_SUBJECT = "google-sub-aprobacion"

_SCRIPTS_DIR = Path(__file__).resolve().parents[3] / "scripts"


def _cargar_script() -> Any:
    """Carga `scripts/access_requests.py` como módulo (mismo patrón que sus tests).

    Se importa acá para verificar que el dry-run del script pregunta por la
    guardia de identidad de Google: es la única forma de que preview y `--apply`
    no vuelvan a divergir.
    """
    if str(_SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS_DIR))
    spec = importlib.util.spec_from_file_location(
        "access_requests_script", _SCRIPTS_DIR / "access_requests.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


async def _contar_solicitudes(db_session: AsyncSession) -> int:
    return int(
        (
            await db_session.execute(select(func.count()).select_from(AccessRequest))
        ).scalar_one()
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


async def test_una_solicitud_ya_abierta_adopta_la_identidad_de_google(
    service: AccessRequestService, db_session: AsyncSession
) -> None:
    """El caso común: primero el formulario público, después "Continuar con Google".

    El token de prefill se consume en el router antes de saber qué camino toma
    `create()`, así que si el reintento no adopta el subject, la identidad se
    pierde en silencio y el aprobado no puede entrar con Google.
    """
    solicitud, primer_outcome = await service.create(
        _input(google_subject=None), ip_hash=None
    )
    assert primer_outcome is CreateOutcome.CREATED
    assert solicitud is not None
    assert solicitud.google_subject is None

    # El mismo email vuelve, ahora por Google: no crea otra fila, pero adopta.
    misma, segundo_outcome = await service.create(_input(), ip_hash=None)

    assert segundo_outcome in (
        CreateOutcome.DUPLICATE_OPEN,
        CreateOutcome.TOKEN_REISSUED,
    )
    assert misma is not None
    assert misma.id == solicitud.id
    assert (await _contar_solicitudes(db_session)) == 1

    # Releído de la base: el subject se commiteó, no quedó pendiente en la sesión.
    await db_session.refresh(misma)
    assert misma.google_subject == _SUBJECT


async def test_la_identidad_adoptada_se_vincula_al_aprobar(
    service: AccessRequestService, db_session: AsyncSession
) -> None:
    """El linkeo tiene que llegar hasta el final, no quedar en la solicitud."""
    solicitud, _ = await service.create(_input(google_subject=None), ip_hash=None)
    assert solicitud is not None
    await service.create(_input(), ip_hash=None)

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
        .first()
    )
    assert token is not None
    await service.verify(str(token.token_id))

    await service.approve(
        solicitud.id,
        vertical=Vertical.KIOSCO_ALMACEN,
        reviewer_user_id=None,
        via="script",
        notes=None,
    )

    identidades = await _identidades(db_session)
    assert [i.provider_subject for i in identidades] == [_SUBJECT]


async def test_no_pisa_una_identidad_distinta_ya_guardada(
    service: AccessRequestService, db_session: AsyncSession
) -> None:
    """Dos cuentas de Google sobre un mismo email en trámite: gana la primera."""
    solicitud, _ = await service.create(_input(), ip_hash=None)
    assert solicitud is not None

    await service.create(_input(google_subject="otro-sub-de-google"), ip_hash=None)

    await db_session.refresh(solicitud)
    assert solicitud.google_subject == _SUBJECT


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

    # Y el dry-run del script lo anticipa, en vez de prometer una cuenta que el
    # `--apply` no va a acuñar (su docstring promete reproducir las guardias).
    script = _cargar_script()
    bloqueos = await script.bloqueos_para_aprobar(db_session, solicitud)
    assert any("identidad de Google" in motivo for motivo in bloqueos), bloqueos


async def test_la_carrera_contra_la_unique_termina_en_el_mismo_conflicto(
    service: AccessRequestService, db_session: AsyncSession
) -> None:
    """El pre-chequeo es un SELECT: dos aprobaciones simultáneas lo pasan las dos.

    Se llama al helper del linkeo DIRECTO, salteando el pre-chequeo, que es
    exactamente lo que ve la segunda aprobación de una carrera. Sin el
    `guarded_savepoint` esto sería un `IntegrityError` crudo (500 opaco); con él
    es el MISMO `AccessRequestGoogleIdentityTaken` que el camino secuencial.
    """
    tenant, user = await provision_tenant(
        db_session,
        business_name="Kiosco El Rápido",
        email="ana@kiosco.example.com",
        full_name="Ana Gómez",
        phone=None,
        vertical=Vertical.KIOSCO_ALMACEN,
        password_hash=None,
    )
    db_session.add(
        UserAuthIdentity(
            tenant_id=tenant.tenant_id,
            user_id=user.user_id,
            provider="google",
            provider_subject=_SUBJECT,
            provider_email=user.email,
        )
    )
    await db_session.flush()

    solicitud = AccessRequest(
        full_name="Otra Persona",
        email="otra@negocio.example.com",
        business_name="Otro Negocio",
        requested_vertical=Vertical.KIOSCO_ALMACEN.value,
        requested_plan=RequestedPlan.FREE.value,
        years_operating="2y_5y",
        staff_size="2_5",
        monthly_revenue_band="3m_10m",
        main_concern="MARGIN",
        records_format="planilla",
        history_depth="1y_3y",
        can_share_files="si_ordenados",
        status="pending",
        google_subject=_SUBJECT,  # la misma identidad, otra solicitud
        consent_version="v1",
        consent_accepted_at=datetime.now(UTC),
    )
    db_session.add(solicitud)
    await db_session.flush()

    with pytest.raises(AccessRequestGoogleIdentityTaken):
        await service._vincular_identidad_google(solicitud, tenant, user)

    # La sesión sigue usable después del savepoint: se puede seguir consultando.
    assert len(await _identidades(db_session)) == 1
