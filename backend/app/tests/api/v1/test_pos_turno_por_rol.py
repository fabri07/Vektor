"""Abrir y cerrar un turno de caja exige un rol que cobra (R-2).

Revisión profunda 2026-10-24: `POST /pos/cash-sessions` y su `/close` pedían
sólo `get_current_user`. Un VIEWER (o un ANALYST) podía abrir un turno o
cerrar el de un cajero con un arqueo inventado —el chequeo de "es SU turno"
sólo mira al CASHIER—, y un arqueo cerrado no se reescribe. Ahora piden los
mismos roles que `/pos/operations` y el 403 sale antes de tocar nada: ni turno
nuevo, ni arqueo, ni `last_seen_at` de la terminal.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.pos_terminal import TERMINAL_HEADER
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.pos_cash_session import PosCashSession
from app.persistence.models.pos_terminal import PosTerminal
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.utils.security import create_access_token, hash_password


async def _headers_de(db: AsyncSession, tenant: Tenant, rol: str) -> dict[str, str]:
    user = User(
        user_id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        email=f"{rol.lower()}-{uuid.uuid4().hex[:6]}@kiosco.com",
        full_name=f"Usuario {rol}",
        password_hash=hash_password("X1234567!"),
        role_code=rol,
        is_active=True,
        pos_permissions={},
    )
    db.add(user)
    await db.commit()
    token = create_access_token(
        {"sub": str(user.user_id), "tenant_id": str(tenant.tenant_id), "role_code": rol}
    )
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture(params=["VIEWER", "ANALYST"])
async def sin_permiso(
    request: pytest.FixtureRequest, db_session: AsyncSession, sample_tenant: Tenant
) -> dict[str, str]:
    return await _headers_de(db_session, sample_tenant, str(request.param))


async def _caja(client: AsyncClient, headers: dict[str, str]) -> dict[str, str]:
    alta = await client.post(
        "/api/v1/pos/terminals",
        json={"name": f"Caja {uuid.uuid4().hex[:4]}"},
        headers=headers,
    )
    assert alta.status_code == 201, alta.text
    return {TERMINAL_HEADER: alta.json()["secret"]}


async def _abrir(
    client: AsyncClient, headers: dict[str, str], turno: str
) -> dict[str, object]:
    resp = await client.post(
        "/api/v1/pos/cash-sessions",
        json={"client_session_id": turno, "opening_float_ars": "1000.00"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def _turnos(db: AsyncSession, tenant: Tenant) -> int:
    return int(
        (
            await db.execute(
                select(func.count(PosCashSession.id)).where(
                    PosCashSession.tenant_id == tenant.tenant_id
                )
            )
        ).scalar_one()
    )


async def _cierres_auditados(db: AsyncSession, tenant: Tenant) -> int:
    return int(
        (
            await db.execute(
                select(func.count(DecisionAuditLog.id)).where(
                    DecisionAuditLog.tenant_id == tenant.tenant_id,
                    DecisionAuditLog.decision_type == "POS_CASH_SESSION_CLOSED",
                )
            )
        ).scalar_one()
    )


async def _turno(db: AsyncSession, sid: str) -> PosCashSession:
    return (
        await db.execute(
            select(PosCashSession)
            .where(PosCashSession.id == uuid.UUID(sid))
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


class TestSinPermiso:
    async def test_no_puede_abrir_un_turno_ni_se_escribe_nada(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        auth_headers: dict[str, str],
        sin_permiso: dict[str, str],
    ) -> None:
        caja = await _caja(client, auth_headers)
        terminal = (
            await db_session.execute(
                select(PosTerminal).where(PosTerminal.tenant_id == sample_tenant.tenant_id)
            )
        ).scalar_one()
        visto_antes = terminal.last_seen_at

        resp = await client.post(
            "/api/v1/pos/cash-sessions",
            json={"client_session_id": "turno-intruso", "opening_float_ars": "1000.00"},
            headers={**sin_permiso, **caja},
        )

        assert resp.status_code == 403, resp.text
        assert await _turnos(db_session, sample_tenant) == 0
        # El 403 sale en la dependencia: ni siquiera se resolvió la terminal.
        await db_session.refresh(terminal)
        assert terminal.last_seen_at == visto_antes

    async def test_no_puede_cerrar_el_turno_de_otro_ni_se_firma_arqueo(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        auth_headers: dict[str, str],
        sin_permiso: dict[str, str],
    ) -> None:
        caja = await _caja(client, auth_headers)
        cajero = await _headers_de(db_session, sample_tenant, "CASHIER")
        turno = await _abrir(client, {**cajero, **caja}, "turno-del-cajero")

        resp = await client.post(
            f"/api/v1/pos/cash-sessions/{turno['id']}/close",
            json={"counted_cash_ars": "999999.00"},
            headers=sin_permiso,
        )

        assert resp.status_code == 403, resp.text
        fila = await _turno(db_session, str(turno["id"]))
        assert fila.status == "OPEN"
        assert fila.counted_cash_ars is None
        assert fila.expected_cash_ars is None
        assert fila.closed_at is None
        assert await _cierres_auditados(db_session, sample_tenant) == 0


class TestRolesQueCobran:
    async def test_el_dueno_abre_y_cierra(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        auth_headers: dict[str, str],
    ) -> None:
        turno = await _abrir(client, auth_headers, "turno-dueno")
        cierre = await client.post(
            f"/api/v1/pos/cash-sessions/{turno['id']}/close",
            json={"counted_cash_ars": "1000.00"},
            headers=auth_headers,
        )
        assert cierre.status_code == 200, cierre.text
        fila = await _turno(db_session, str(turno["id"]))
        assert fila.status == "CLOSED"
        assert str(fila.counted_cash_ars) == "1000.00"
        assert await _cierres_auditados(db_session, sample_tenant) == 1

    async def test_el_cajero_abre_y_cierra_su_turno(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        auth_headers: dict[str, str],
    ) -> None:
        caja = await _caja(client, auth_headers)
        cajero = {**await _headers_de(db_session, sample_tenant, "CASHIER"), **caja}
        turno = await _abrir(client, cajero, "turno-propio")
        assert await _turnos(db_session, sample_tenant) == 1

        cierre = await client.post(
            f"/api/v1/pos/cash-sessions/{turno['id']}/close",
            json={"counted_cash_ars": "1000.00"},
            headers=cajero,
        )
        assert cierre.status_code == 200, cierre.text
        assert (await _turno(db_session, str(turno["id"]))).status == "CLOSED"

    async def test_el_cajero_sigue_sin_poder_cerrar_el_de_otro(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        sample_tenant: Tenant,
        auth_headers: dict[str, str],
    ) -> None:
        caja = await _caja(client, auth_headers)
        ana = {**await _headers_de(db_session, sample_tenant, "CASHIER"), **caja}
        beto = {**await _headers_de(db_session, sample_tenant, "CASHIER"), **caja}
        turno = await _abrir(client, ana, "turno-ana")

        resp = await client.post(
            f"/api/v1/pos/cash-sessions/{turno['id']}/close",
            json={"counted_cash_ars": "0.00"},
            headers=beto,
        )
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "CASH_SESSION_NOT_OWN"
        assert (await _turno(db_session, str(turno["id"]))).status == "OPEN"
