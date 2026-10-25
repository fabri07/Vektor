"""Convertir una acción en regla automática exige el rol que confirma (R-3).

Revisión profunda 2026-10-24: `POST /automations/from-pending-action/{id}` sólo
pedía estar logueado, así que un VIEWER (o un ANALYST) convertía en regla una
acción suya que había aprobado el dueño. Una regla ejecuta sin preguntar, así
que la decide quien confirma: los mismos roles que `/agent/confirm`
(`ROLES_QUE_EJECUTAN`, R-1). El 403 sale antes de tocar nada.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.shared.schemas import ActionType, RiskLevel
from app.application.services.automation_service import AUTOMATION_PAYLOAD_AGENT_KEY
from app.config.settings import get_settings
from app.persistence.models.agent_automation_rule import AgentAutomationRule
from app.persistence.models.pending_action import PendingAction
from app.persistence.models.tenant import Tenant
from app.persistence.models.user import User
from app.utils.security import create_access_token

pytestmark = pytest.mark.asyncio


async def _usuario_de(
    db: AsyncSession, tenant: Tenant, rol: str
) -> tuple[User, dict[str, str]]:
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
    return user, {"Authorization": f"Bearer {token}"}


async def _accion_aprobada_de(db: AsyncSession, user: User) -> PendingAction:
    """Una acción del propio usuario, aprobada y ejecutada bien: automatizable."""
    action = PendingAction(
        tenant_id=user.tenant_id,
        user_id=user.user_id,
        action_type=ActionType.CREATE_SUPPLIER_DRAFT,
        payload={
            AUTOMATION_PAYLOAD_AGENT_KEY: "agent_supplier",
            "mode": "mcp",
            "to": "proveedor@example.com",
            "subject": "Pedido",
            "body": "Hola, necesito reponer stock.",
        },
        risk_level=RiskLevel.LOW,
        status="APPROVED",
        execution_status="SUCCEEDED",
        external_system="GOOGLE_GMAIL",
    )
    db.add(action)
    await db.commit()
    await db.refresh(action)
    return action


async def _reglas(db: AsyncSession, tenant: Tenant) -> int:
    return int(
        (
            await db.execute(
                select(func.count(AgentAutomationRule.id)).where(
                    AgentAutomationRule.tenant_id == tenant.tenant_id
                )
            )
        ).scalar_one()
    )


@pytest.fixture(autouse=True)
def _automatizaciones_prendidas(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "ENABLE_AGENT_AUTOMATIONS", True)


@pytest.mark.parametrize("rol", ["VIEWER", "ANALYST"])
async def test_sin_rol_que_confirma_no_automatiza_ni_se_escribe_nada(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    rol: str,
) -> None:
    user, headers = await _usuario_de(db_session, sample_tenant, rol)
    action = await _accion_aprobada_de(db_session, user)

    response = await client.post(
        f"/api/v1/automations/from-pending-action/{action.id}", headers=headers
    )

    assert response.status_code == 403
    assert await _reglas(db_session, sample_tenant) == 0


@pytest.mark.parametrize("rol", ["OWNER", "ADMIN"])
async def test_quien_confirma_automatiza_su_accion(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
    rol: str,
) -> None:
    user, headers = await _usuario_de(db_session, sample_tenant, rol)
    action = await _accion_aprobada_de(db_session, user)

    response = await client.post(
        f"/api/v1/automations/from-pending-action/{action.id}", headers=headers
    )

    assert response.status_code == 201
    assert response.json()["created"] is True
    regla = (
        await db_session.execute(
            select(AgentAutomationRule).where(
                AgentAutomationRule.tenant_id == sample_tenant.tenant_id
            )
        )
    ).scalar_one()
    assert regla.user_id == user.user_id
    assert regla.action_type == str(ActionType.CREATE_SUPPLIER_DRAFT)


async def test_el_admin_no_automatiza_la_accion_de_otro(
    client: AsyncClient,
    db_session: AsyncSession,
    sample_tenant: Tenant,
) -> None:
    """El rol no reemplaza el chequeo de dueño de la acción: sigue siendo 404."""
    dueno, _ = await _usuario_de(db_session, sample_tenant, "OWNER")
    _, headers_admin = await _usuario_de(db_session, sample_tenant, "ADMIN")
    action = await _accion_aprobada_de(db_session, dueno)

    response = await client.post(
        f"/api/v1/automations/from-pending-action/{action.id}", headers=headers_admin
    )

    assert response.status_code == 404
    assert await _reglas(db_session, sample_tenant) == 0
