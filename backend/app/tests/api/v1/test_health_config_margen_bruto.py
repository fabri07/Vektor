"""El objetivo de margen de /settings/health-config es BRUTO y queda auditado (D-4).

Revisión profunda 2026-10-24: la pantalla prometía «margen bruto» y el motor lo
aplicaba como vara del margen NETO del score. Ahora el objetivo es bruto en
todos lados (pantalla, schema, análisis por producto del chat), el score mide el
neto contra el rubro (``test_health_score_service``), y guardar o restablecer el
objetivo queda en ``decision_audit_log`` con el antes y el después.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.heuristic_override import BusinessHeuristicOverride
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.utils.security import create_access_token

_URL = "/api/v1/settings/health-config"


async def _headers_de(db: AsyncSession, tenant: Tenant, rol: str) -> dict[str, str]:
    user = User(
        user_id=uuid.uuid4(),
        tenant_id=tenant.tenant_id,
        email=f"{rol.lower()}-{uuid.uuid4().hex[:6]}@kiosco.com",
        full_name=f"Usuario {rol}",
        password_hash="x",
        role_code=rol,
        is_active=True,
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


async def _overrides(db: AsyncSession, tenant: Tenant) -> dict[str, float]:
    rows = await db.execute(
        select(BusinessHeuristicOverride).where(
            BusinessHeuristicOverride.tenant_id == tenant.tenant_id
        )
    )
    return {r.param_key: float(r.param_value["value"]) for r in rows.scalars()}


async def _auditorias(db: AsyncSession, tenant: Tenant) -> list[DecisionAuditLog]:
    rows = await db.execute(
        select(DecisionAuditLog)
        .where(
            DecisionAuditLog.tenant_id == tenant.tenant_id,
            DecisionAuditLog.decision_type.in_(
                ["MARGIN_CONFIG_UPDATED", "MARGIN_CONFIG_RESET"]
            ),
        )
        .order_by(DecisionAuditLog.created_at)
    )
    return list(rows.scalars())


async def test_sin_configurar_dice_que_es_bruto_y_predeterminado(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    resp = await client.get(_URL, headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["margin_kind"] == "bruto"
    assert body["is_custom"] is False
    assert (body["target_margin_pct"], body["warning_margin_pct"]) == (35.0, 20.0)


async def test_guardar_persiste_y_audita_antes_y_despues(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sample_user: User,
    auth_headers: dict[str, str],
) -> None:
    resp = await client.patch(
        _URL,
        headers=auth_headers,
        json={"target_margin_pct": 40.0, "warning_margin_pct": 25.0},
    )
    await db_session.commit()

    assert resp.status_code == 200
    assert resp.json()["margin_kind"] == "bruto"
    assert await _overrides(db_session, sample_tenant) == {
        "target_margin_pct": 40.0,
        "warning_margin_pct": 25.0,
    }
    (audit,) = await _auditorias(db_session, sample_tenant)
    assert audit.decision_type == "MARGIN_CONFIG_UPDATED"
    assert audit.actor_user_id == sample_user.user_id
    assert audit.decision_data["before"]["target_margin_pct"] == 35.0
    assert audit.decision_data["before"]["is_custom"] is False
    assert audit.decision_data["after"]["target_margin_pct"] == 40.0
    assert audit.decision_data["after"]["warning_margin_pct"] == 25.0


async def test_restablecer_borra_y_audita(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    auth_headers: dict[str, str],
) -> None:
    await client.patch(
        _URL,
        headers=auth_headers,
        json={"target_margin_pct": 40.0, "warning_margin_pct": 25.0},
    )
    resp = await client.delete(_URL, headers=auth_headers)
    await db_session.commit()

    assert resp.status_code == 200
    assert resp.json()["is_custom"] is False
    assert await _overrides(db_session, sample_tenant) == {}
    auditorias = await _auditorias(db_session, sample_tenant)
    assert [a.decision_type for a in auditorias] == [
        "MARGIN_CONFIG_UPDATED",
        "MARGIN_CONFIG_RESET",
    ]
    reset = auditorias[-1]
    assert reset.decision_data["before"]["target_margin_pct"] == 40.0
    assert reset.decision_data["after"]["target_margin_pct"] == 35.0


async def test_sin_permiso_no_guarda_ni_audita(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    sin_permiso: dict[str, str],
) -> None:
    resp = await client.patch(
        _URL,
        headers=sin_permiso,
        json={"target_margin_pct": 40.0, "warning_margin_pct": 25.0},
    )

    assert resp.status_code == 403
    assert await _overrides(db_session, sample_tenant) == {}
    total = (
        await db_session.execute(
            select(func.count(DecisionAuditLog.id)).where(
                DecisionAuditLog.tenant_id == sample_tenant.tenant_id,
                DecisionAuditLog.decision_type == "MARGIN_CONFIG_UPDATED",
            )
        )
    ).scalar_one()
    assert total == 0
