"""Tests de la cola de revisión de solicitudes (/api/v1/admin/access-requests).

Cubre lo que un error acá costaría caro: que NADIE fuera de SUPERADMIN pueda
listar ni decidir (OWNER, ADMIN y VIEWER incluidos), que `assigned_vertical="otros"`
muera en un 422 antes de tocar el servicio, que aprobar algo que no está en la
cola sea 409, y que la aprobación —la única puerta que hoy acuña cuentas— acuñe
exactamente una.
"""

from __future__ import annotations

import unittest.mock
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import access_request_service
from app.domain.access_request import AccessRequestStatus, RequestedPlan
from app.domain.contact_lead import EmailNotificationStatus
from app.domain.verticals import Vertical
from app.persistence.models.access_request import AccessRequest
from app.persistence.models.business import BusinessProfile
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.utils.security import create_access_token, hash_password

BASE = "/api/v1/admin/access-requests"


@pytest.fixture
def encolar() -> Any:
    """El worker Celery lo agrega la tarea siguiente; acá solo se asierta qué se encoló."""
    with unittest.mock.patch.object(access_request_service, "_encolar") as mock:
        yield mock


async def _headers_con_rol(
    db: AsyncSession, tenant: Tenant, rol: str, email: str
) -> dict[str, str]:
    user = User(
        user_id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        email=email,
        full_name=f"Usuario {rol}",
        password_hash=hash_password("Secure789"),
        role_code=rol,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    token = create_access_token(
        {
            "sub": str(user.user_id),
            "tenant_id": str(tenant.tenant_id),
            "role_code": rol,
        }
    )
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def superadmin_headers(db_session: AsyncSession, sample_tenant: Tenant) -> dict[str, str]:
    return await _headers_con_rol(db_session, sample_tenant, "SUPERADMIN", "super@vektor.app")


async def _sembrar(
    db: AsyncSession,
    *,
    email: str = "solicitante@example.com",
    status: AccessRequestStatus = AccessRequestStatus.PENDING,
    plan: RequestedPlan = RequestedPlan.FREE,
    requested_vertical: str = Vertical.KIOSCO_ALMACEN.value,
    vertical_other_text: str | None = None,
    business_name: str = "Kiosco El Rápido",
) -> AccessRequest:
    """Inserta una solicitud directo, sin pasar por el formulario público."""
    ahora = datetime.now(UTC)
    solicitud = AccessRequest(
        full_name="Juan Pérez",
        email=email,
        phone="+541155551234",
        business_name=business_name,
        requested_vertical=requested_vertical,
        vertical_other_text=vertical_other_text,
        requested_plan=plan.value,
        years_operating="2y_5y",
        staff_size="2_5",
        monthly_revenue_band="3m_10m",
        main_concern="MARGIN",
        records_format="planilla",
        history_depth="1y_3y",
        can_share_files="si_desprolijos",
        status=status.value,
        email_verified_at=(
            ahora if status is not AccessRequestStatus.UNVERIFIED else None
        ),
        verification_email_status=EmailNotificationStatus.PENDING.value,
        owner_notification_status=EmailNotificationStatus.PENDING.value,
        decision_email_status=EmailNotificationStatus.PENDING.value,
        consent_version="v1",
        consent_accepted_at=ahora,
    )
    db.add(solicitud)
    await db.commit()
    return solicitud


async def _releer(db: AsyncSession, solicitud: AccessRequest) -> AccessRequest:
    """Relee del disco lo que escribió el endpoint.

    La fixture usa `expire_on_commit=False`, así que después del commit del request
    la instancia sigue con los valores viejos. El refresh va por `await`: expirar y
    después tocar un atributo dispara el loader SÍNCRONO de SQLAlchemy y revienta
    con `MissingGreenlet`.
    """
    await db.refresh(solicitud)
    return solicitud


async def _contar(db: AsyncSession, modelo: type) -> int:
    return int((await db.execute(select(func.count()).select_from(modelo))).scalar_one())


# ── RBAC ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("rol", ["OWNER", "ADMIN", "VIEWER"])
async def test_ningun_rol_de_negocio_ve_la_cola(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    rol: str,
) -> None:
    """Ni el OWNER de un tenant puede ver solicitudes: es una cola de la plataforma."""
    headers = await _headers_con_rol(
        db_session, sample_tenant, rol, f"{rol.lower()}@kiosco.com"
    )
    res = await client.get(BASE, headers=headers)
    assert res.status_code == 403, res.text


async def test_sin_token_es_401(client: AsyncClient) -> None:
    assert (await client.get(BASE)).status_code == 401


@pytest.mark.parametrize("rol", ["OWNER", "ADMIN", "VIEWER"])
async def test_ningun_rol_de_negocio_aprueba(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    rol: str,
    encolar: Any,
) -> None:
    solicitud = await _sembrar(db_session)
    headers = await _headers_con_rol(
        db_session, sample_tenant, rol, f"{rol.lower()}2@kiosco.com"
    )
    res = await client.post(
        f"{BASE}/{solicitud.id}/approve",
        json={"assigned_vertical": Vertical.LIMPIEZA.value},
        headers=headers,
    )
    assert res.status_code == 403, res.text
    # Y no acuñó nada: sigue habiendo un solo tenant (el del fixture).
    assert await _contar(db_session, Tenant) == 1


# ── Listado ───────────────────────────────────────────────────────────────────


async def test_listado_devuelve_la_cola_con_prioridad_derivada(
    client: AsyncClient, db_session: AsyncSession, superadmin_headers: dict[str, str]
) -> None:
    await _sembrar(db_session, email="free@example.com", plan=RequestedPlan.FREE)
    await _sembrar(db_session, email="premium@example.com", plan=RequestedPlan.PREMIUM)

    res = await client.get(BASE, headers=superadmin_headers)
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["total"] == 2
    # Premium primero (orden derivado de requested_plan, no de una columna).
    assert data["items"][0]["email"] == "premium@example.com"
    assert data["items"][0]["review_priority"] == "high"
    assert data["items"][1]["review_priority"] == "normal"
    # La ficha no filtra el hash de IP.
    assert "ip_hash" not in data["items"][0]


async def test_listado_excluye_lo_que_no_esta_en_la_cola(
    client: AsyncClient, db_session: AsyncSession, superadmin_headers: dict[str, str]
) -> None:
    """Sin `?status`, la cola son `pending` + `waitlist`. Una `unverified` no cuenta:
    sin doble opt-in nadie confirmó ese email."""
    await _sembrar(db_session, email="pendiente@example.com")
    await _sembrar(
        db_session, email="sinverificar@example.com", status=AccessRequestStatus.UNVERIFIED
    )
    await _sembrar(
        db_session, email="rechazada@example.com", status=AccessRequestStatus.REJECTED
    )

    res = await client.get(BASE, headers=superadmin_headers)
    assert res.status_code == 200
    assert [i["email"] for i in res.json()["items"]] == ["pendiente@example.com"]

    # Pero pidiéndolo explícito sí aparece.
    res = await client.get(f"{BASE}?status=unverified", headers=superadmin_headers)
    assert [i["email"] for i in res.json()["items"]] == ["sinverificar@example.com"]


async def test_filtro_por_requested_plan(
    client: AsyncClient, db_session: AsyncSession, superadmin_headers: dict[str, str]
) -> None:
    await _sembrar(db_session, email="free@example.com", plan=RequestedPlan.FREE)
    await _sembrar(db_session, email="premium@example.com", plan=RequestedPlan.PREMIUM)

    res = await client.get(f"{BASE}?requested_plan=premium", headers=superadmin_headers)
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 1
    assert [i["email"] for i in data["items"]] == ["premium@example.com"]


async def test_plan_desconocido_en_el_filtro_es_422(
    client: AsyncClient, superadmin_headers: dict[str, str]
) -> None:
    res = await client.get(f"{BASE}?requested_plan=enterprise", headers=superadmin_headers)
    assert res.status_code == 422


async def test_detalle_y_404(
    client: AsyncClient, db_session: AsyncSession, superadmin_headers: dict[str, str]
) -> None:
    solicitud = await _sembrar(db_session, vertical_other_text=None)
    res = await client.get(f"{BASE}/{solicitud.id}", headers=superadmin_headers)
    assert res.status_code == 200
    assert res.json()["email"] == "solicitante@example.com"

    faltante = await client.get(f"{BASE}/{uuid.uuid4()}", headers=superadmin_headers)
    assert faltante.status_code == 404
    assert faltante.json()["detail"] == "access_request_not_found"


# ── Aprobar ───────────────────────────────────────────────────────────────────


async def test_approve_acuna_la_cuenta(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin_headers: dict[str, str],
    encolar: Any,
) -> None:
    solicitud = await _sembrar(db_session)
    tenants_antes = await _contar(db_session, Tenant)

    res = await client.post(
        f"{BASE}/{solicitud.id}/approve",
        json={"assigned_vertical": Vertical.LIMPIEZA.value, "notes": "Encaja."},
        headers=superadmin_headers,
    )
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["already_approved"] is False
    assert data["tenant_id"] is not None
    assert data["request"]["status"] == AccessRequestStatus.APPROVED.value
    # El vertical lo asigna el DUEÑO, no el solicitante.
    assert data["request"]["assigned_vertical_code"] == Vertical.LIMPIEZA.value
    # El token de invitación NO viaja en el cuerpo (es una credencial).
    assert "invite_token_id" not in data

    assert await _contar(db_session, Tenant) == tenants_antes + 1
    perfil = (
        await db_session.execute(
            select(BusinessProfile).where(
                BusinessProfile.tenant_id == uuid.UUID(data["tenant_id"])
            )
        )
    ).scalar_one()
    assert perfil.vertical_code == Vertical.LIMPIEZA.value
    # Los números financieros se piden en el primer login, no en el formulario.
    assert perfil.onboarding_completed is False


async def test_approve_es_idempotente(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin_headers: dict[str, str],
    encolar: Any,
) -> None:
    """La doble aprobación pasa de verdad (API + script) y no puede acuñar dos tenants."""
    solicitud = await _sembrar(db_session)
    payload = {"assigned_vertical": Vertical.KIOSCO_ALMACEN.value}

    primera = await client.post(
        f"{BASE}/{solicitud.id}/approve", json=payload, headers=superadmin_headers
    )
    tenants = await _contar(db_session, Tenant)
    segunda = await client.post(
        f"{BASE}/{solicitud.id}/approve", json=payload, headers=superadmin_headers
    )

    assert primera.status_code == segunda.status_code == 200
    assert primera.json()["already_approved"] is False
    assert segunda.json()["already_approved"] is True
    assert segunda.json()["tenant_id"] == primera.json()["tenant_id"]
    assert await _contar(db_session, Tenant) == tenants


async def test_approve_con_otros_es_422(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin_headers: dict[str, str],
    encolar: Any,
) -> None:
    """`'otros'` nunca es un vertical operativo: muere en el schema, antes del servicio.

    El CHECK `ck_access_requests_assigned_vertical_code` es el backstop, no la
    primera línea de defensa.
    """
    solicitud = await _sembrar(
        db_session, requested_vertical="otros", vertical_other_text="Ferretería"
    )
    res = await client.post(
        f"{BASE}/{solicitud.id}/approve",
        json={"assigned_vertical": "otros"},
        headers=superadmin_headers,
    )
    assert res.status_code == 422, res.text
    # Nada se movió: ni la solicitud ni una cuenta nueva.
    fila = await _releer(db_session, solicitud)
    assert fila.status == AccessRequestStatus.PENDING.value
    assert await _contar(db_session, Tenant) == 1


async def test_approve_sin_vertical_es_422(
    client: AsyncClient, db_session: AsyncSession, superadmin_headers: dict[str, str]
) -> None:
    solicitud = await _sembrar(db_session)
    res = await client.post(
        f"{BASE}/{solicitud.id}/approve", json={}, headers=superadmin_headers
    )
    assert res.status_code == 422


@pytest.mark.parametrize(
    "estado",
    [
        AccessRequestStatus.UNVERIFIED,
        AccessRequestStatus.REJECTED,
        AccessRequestStatus.EXPIRED,
    ],
)
async def test_approve_de_un_estado_no_aprobable_es_409(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin_headers: dict[str, str],
    estado: AccessRequestStatus,
    encolar: Any,
) -> None:
    solicitud = await _sembrar(db_session, status=estado)
    res = await client.post(
        f"{BASE}/{solicitud.id}/approve",
        json={"assigned_vertical": Vertical.KIOSCO_ALMACEN.value},
        headers=superadmin_headers,
    )
    assert res.status_code == 409, res.text
    assert await _contar(db_session, Tenant) == 1


async def test_approve_con_el_email_ya_tomado_es_409(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_user: User,
    superadmin_headers: dict[str, str],
    encolar: Any,
) -> None:
    """Alguien creó la cuenta a mano entre el envío y la aprobación."""
    solicitud = await _sembrar(db_session, email=sample_user.email)
    res = await client.post(
        f"{BASE}/{solicitud.id}/approve",
        json={"assigned_vertical": Vertical.KIOSCO_ALMACEN.value},
        headers=superadmin_headers,
    )
    assert res.status_code == 409, res.text


async def test_approve_inexistente_es_404(
    client: AsyncClient, superadmin_headers: dict[str, str]
) -> None:
    res = await client.post(
        f"{BASE}/{uuid.uuid4()}/approve",
        json={"assigned_vertical": Vertical.KIOSCO_ALMACEN.value},
        headers=superadmin_headers,
    )
    assert res.status_code == 404


# ── Rechazar / postergar ──────────────────────────────────────────────────────


async def test_reject_registra_el_motivo(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin_headers: dict[str, str],
    encolar: Any,
) -> None:
    solicitud = await _sembrar(db_session)
    res = await client.post(
        f"{BASE}/{solicitud.id}/reject",
        json={"reason": "Rubro no soportado por ahora."},
        headers=superadmin_headers,
    )
    assert res.status_code == 200, res.text
    assert res.json()["status"] == AccessRequestStatus.REJECTED.value
    assert res.json()["reviewed_via"] == "api"

    fila = await _releer(db_session, solicitud)
    assert fila.rejection_reason == "Rubro no soportado por ahora."
    assert fila.reviewed_by_user_id is not None


async def test_reject_sin_motivo_util_es_422(
    client: AsyncClient, db_session: AsyncSession, superadmin_headers: dict[str, str]
) -> None:
    solicitud = await _sembrar(db_session)
    for cuerpo in ({}, {"reason": "no"}):
        res = await client.post(
            f"{BASE}/{solicitud.id}/reject", json=cuerpo, headers=superadmin_headers
        )
        assert res.status_code == 422, res.text


async def test_reject_sin_notificar(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin_headers: dict[str, str],
    encolar: Any,
) -> None:
    """Descartar spam no le escribe de vuelta al que lo mandó."""
    solicitud = await _sembrar(db_session)
    res = await client.post(
        f"{BASE}/{solicitud.id}/reject",
        json={"reason": "Spam evidente.", "notify": False},
        headers=superadmin_headers,
    )
    assert res.status_code == 200
    encolar.assert_not_called()


async def test_waitlist_y_después_aprobar(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin_headers: dict[str, str],
    encolar: Any,
) -> None:
    """`waitlist` no es terminal: sigue siendo aprobable."""
    solicitud = await _sembrar(db_session)
    postergada = await client.post(
        f"{BASE}/{solicitud.id}/waitlist",
        json={"notes": "Sin lugar este mes."},
        headers=superadmin_headers,
    )
    assert postergada.status_code == 200
    assert postergada.json()["status"] == AccessRequestStatus.WAITLIST.value

    aprobada = await client.post(
        f"{BASE}/{solicitud.id}/approve",
        json={"assigned_vertical": Vertical.KIOSCO_ALMACEN.value},
        headers=superadmin_headers,
    )
    assert aprobada.status_code == 200, aprobada.text


async def test_no_se_puede_rechazar_ni_postergar_una_aprobada(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin_headers: dict[str, str],
    encolar: Any,
) -> None:
    """Una aprobada ya acuñó un tenant: no se "des-aprueba" por acá."""
    solicitud = await _sembrar(db_session)
    await client.post(
        f"{BASE}/{solicitud.id}/approve",
        json={"assigned_vertical": Vertical.KIOSCO_ALMACEN.value},
        headers=superadmin_headers,
    )

    rechazo = await client.post(
        f"{BASE}/{solicitud.id}/reject",
        json={"reason": "Me arrepentí."},
        headers=superadmin_headers,
    )
    postergar = await client.post(
        f"{BASE}/{solicitud.id}/waitlist", json={}, headers=superadmin_headers
    )
    assert rechazo.status_code == 409, rechazo.text
    assert postergar.status_code == 409, postergar.text


async def test_no_se_puede_postergar_una_sin_verificar(
    client: AsyncClient,
    db_session: AsyncSession,
    superadmin_headers: dict[str, str],
    encolar: Any,
) -> None:
    """Postergar manda mail y deja el trámite abierto: sin doble opt-in sería
    escribirle a una casilla sin consentimiento y trabarle el reintento. El
    camino para descartarla es `reject` (que tiene `notify`)."""
    solicitud = await _sembrar(db_session, status=AccessRequestStatus.UNVERIFIED)

    postergar = await client.post(
        f"{BASE}/{solicitud.id}/waitlist", json={}, headers=superadmin_headers
    )

    assert postergar.status_code == 409, postergar.text
    assert "doble opt-in" in postergar.json()["detail"]
    encolar.assert_not_called()


async def test_decisiones_sobre_una_solicitud_inexistente_son_404(
    client: AsyncClient, superadmin_headers: dict[str, str]
) -> None:
    faltante = uuid.uuid4()
    rechazo = await client.post(
        f"{BASE}/{faltante}/reject", json={"reason": "Nada."}, headers=superadmin_headers
    )
    postergar = await client.post(
        f"{BASE}/{faltante}/waitlist", json={}, headers=superadmin_headers
    )
    assert rechazo.status_code == 404
    assert postergar.status_code == 404
