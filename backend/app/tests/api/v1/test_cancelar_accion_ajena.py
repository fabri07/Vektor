"""Cancelar una acción pendiente exige ser su autor o un rol que confirma (R-5).

Revisión profunda 2026-10-24: `POST /agent/cancel/{id}` y
`POST /agent/cancel/group/{id}` sólo filtraban por tenant, así que cualquier
usuario del negocio (un VIEWER, un ANALYST) rechazaba la acción que el dueño
tenía para aprobar. Ahora cancela el autor o un OWNER/ADMIN; en un grupo el
permiso se valida sobre todas las acciones antes de tocar ninguna, y una acción
de otro negocio sigue siendo 404 para no revelar que existe.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.agent import ChatRequest, _try_nl_confirmation
from app.persistence.models.pending_action import PendingAction
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.tests.services import test_nl_confirmation as _nl
from app.utils.security import create_access_token


async def _usuario(db: AsyncSession, tenant_id: uuid.UUID, rol: str) -> User:
    user = User(
        user_id=uuid.uuid4(),
        tenant_id=tenant_id,
        email=f"{rol.lower()}-{uuid.uuid4().hex[:6]}@kiosco.com",
        full_name=f"Usuario {rol}",
        password_hash="x",
        role_code=rol,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    return user


def _headers_de(user: User) -> dict[str, str]:
    token = create_access_token(
        {"sub": str(user.user_id), "tenant_id": str(user.tenant_id), "role_code": user.role_code}
    )
    return {"Authorization": f"Bearer {token}"}


async def _pendiente(
    db: AsyncSession,
    autor: User,
    *,
    group_id: str | None = None,
    position: int = 0,
) -> PendingAction:
    action = PendingAction(
        tenant_id=autor.tenant_id,
        user_id=autor.user_id,
        action_type="REGISTER_EXPENSE",
        payload={"amount": 100},
        risk_level="MEDIUM",
        status="PENDING",
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )
    if group_id:
        action.approval_group_id = group_id
        action.group_execution_status = "PENDING"
        action.group_position = position
    db.add(action)
    await db.commit()
    return action


def _ids(*acciones: PendingAction) -> list[uuid.UUID]:
    """Los ids, leídos antes del request: después del commit del endpoint
    (`expire_on_commit` de la sesión del request) dispararían una carga perezosa."""
    return [a.id for a in acciones]


async def _estado(db: AsyncSession, action_id: uuid.UUID) -> tuple[str, str | None]:
    """Lee el estado desde la base, no desde la identity map.

    Un SELECT de columnas autoflushea: un cambio a medio hacer en la sesión
    (una acción marcada REJECTED antes de rechazar el grupo) llega a la base y
    se ve. Por eso no se usa `expire_all()`, que lo descartaría en silencio.
    """
    fila = (
        await db.execute(
            select(PendingAction.status, PendingAction.group_execution_status).where(
                PendingAction.id == action_id
            )
        )
    ).one()
    return str(fila[0]), fila[1]


@pytest_asyncio.fixture
async def duenio(db_session: AsyncSession, sample_tenant: Tenant) -> User:
    return await _usuario(db_session, sample_tenant.tenant_id, "OWNER")


@pytest_asyncio.fixture(params=["VIEWER", "ANALYST"])
async def sin_permiso(
    request: pytest.FixtureRequest, db_session: AsyncSession, sample_tenant: Tenant
) -> User:
    return await _usuario(db_session, sample_tenant.tenant_id, str(request.param))


# ── Acción individual ────────────────────────────────────────────────────────


async def test_no_cancela_la_accion_de_otro(
    client: AsyncClient, db_session: AsyncSession, duenio: User, sin_permiso: User
) -> None:
    action = await _pendiente(db_session, duenio)

    resp = await client.post(
        f"/api/v1/agent/cancel/{action.id}", headers=_headers_de(sin_permiso)
    )

    assert resp.status_code == 403, resp.text
    assert "otro usuario" in resp.json()["detail"]
    assert await _estado(db_session, action.id) == ("PENDING", None)


async def test_cancela_la_propia(
    client: AsyncClient, db_session: AsyncSession, sin_permiso: User
) -> None:
    action = await _pendiente(db_session, sin_permiso)

    resp = await client.post(
        f"/api/v1/agent/cancel/{action.id}", headers=_headers_de(sin_permiso)
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"
    assert (await _estado(db_session, action.id))[0] == "REJECTED"


@pytest.mark.parametrize("rol", ["OWNER", "ADMIN"])
async def test_owner_y_admin_cancelan_la_de_otro(
    client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant, rol: str
) -> None:
    autor = await _usuario(db_session, sample_tenant.tenant_id, "VIEWER")
    action = await _pendiente(db_session, autor)
    quien = await _usuario(db_session, sample_tenant.tenant_id, rol)

    resp = await client.post(f"/api/v1/agent/cancel/{action.id}", headers=_headers_de(quien))

    assert resp.status_code == 200, resp.text
    assert (await _estado(db_session, action.id))[0] == "REJECTED"


async def test_accion_de_otro_tenant_es_404(
    client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant, duenio: User
) -> None:
    otro = Tenant(
        tenant_id=uuid.uuid4(),
        legal_name="Otro negocio",
        display_name="Otro",
        currency="ARS",
        status="ACTIVE",
    )
    db_session.add(otro)
    await db_session.commit()
    ajeno = await _usuario(db_session, otro.tenant_id, "OWNER")
    action = await _pendiente(db_session, ajeno)

    resp = await client.post(f"/api/v1/agent/cancel/{action.id}", headers=_headers_de(duenio))

    assert resp.status_code == 404, resp.text
    assert await _estado(db_session, action.id) == ("PENDING", None)


# ── Grupo ────────────────────────────────────────────────────────────────────


async def test_grupo_mixto_se_rechaza_entero(
    client: AsyncClient, db_session: AsyncSession, duenio: User, sin_permiso: User
) -> None:
    group_id = str(uuid.uuid4())
    # La propia va primero: si el permiso se validara acción por acción,
    # ésta quedaría REJECTED antes de llegar a la ajena.
    propia = await _pendiente(db_session, sin_permiso, group_id=group_id, position=0)
    ajena = await _pendiente(db_session, duenio, group_id=group_id, position=1)
    id_propia, id_ajena = _ids(propia, ajena)

    resp = await client.post(
        f"/api/v1/agent/cancel/group/{group_id}", headers=_headers_de(sin_permiso)
    )

    assert resp.status_code == 403, resp.text
    assert await _estado(db_session, id_propia) == ("PENDING", "PENDING")
    assert await _estado(db_session, id_ajena) == ("PENDING", "PENDING")


async def test_grupo_propio_se_cancela(
    client: AsyncClient, db_session: AsyncSession, sin_permiso: User
) -> None:
    group_id = str(uuid.uuid4())
    a1 = await _pendiente(db_session, sin_permiso, group_id=group_id, position=0)
    a2 = await _pendiente(db_session, sin_permiso, group_id=group_id, position=1)
    id1, id2 = _ids(a1, a2)

    resp = await client.post(
        f"/api/v1/agent/cancel/group/{group_id}", headers=_headers_de(sin_permiso)
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["cancelled_count"] == 2
    assert await _estado(db_session, id1) == ("REJECTED", "FAILED")
    assert await _estado(db_session, id2) == ("REJECTED", "FAILED")


@pytest.mark.parametrize("rol", ["OWNER", "ADMIN"])
async def test_owner_y_admin_cancelan_grupo_ajeno(
    client: AsyncClient, db_session: AsyncSession, sample_tenant: Tenant, rol: str
) -> None:
    autor = await _usuario(db_session, sample_tenant.tenant_id, "VIEWER")
    group_id = str(uuid.uuid4())
    a1 = await _pendiente(db_session, autor, group_id=group_id, position=0)
    quien = await _usuario(db_session, sample_tenant.tenant_id, rol)

    resp = await client.post(
        f"/api/v1/agent/cancel/group/{group_id}", headers=_headers_de(quien)
    )

    assert resp.status_code == 200, resp.text
    assert (await _estado(db_session, a1.id))[0] == "REJECTED"


# ── «no» por texto ───────────────────────────────────────────────────────────


async def test_no_por_texto_no_cancela_la_de_otro(
    db_session: AsyncSession, duenio: User, sin_permiso: User
) -> None:
    action = await _pendiente(db_session, duenio)
    redis = _nl.FakeRedis()
    conv_id = str(uuid.uuid4())
    await _nl._seed_pending_pointer(redis, db_session, conv_id, pending_action_id=str(action.id))

    resp = await _try_nl_confirmation(
        body=ChatRequest(message="no", conversation_id=conv_id),
        current_user=sin_permiso,
        db=db_session,
        redis=redis,  # type: ignore[arg-type]
    )

    assert resp is not None
    assert resp.status == "error"
    assert resp.result["nl_confirmation"] == "forbidden"
    assert await _estado(db_session, action.id) == ("PENDING", None)
