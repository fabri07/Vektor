import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.agents.shared.schemas import ActionType, RiskLevel
from app.application.services.automation_service import AUTOMATION_PAYLOAD_AGENT_KEY
from app.config.settings import get_settings
from app.persistence.models.agent_automation_rule import AgentAutomationRule
from app.persistence.models.pending_action import PendingAction
from app.persistence.models.user import User

pytestmark = pytest.mark.asyncio


async def _successful_pending_action(
    db_session: AsyncSession,
    sample_user: User,
) -> PendingAction:
    action = PendingAction(
        tenant_id=sample_user.tenant_id,
        user_id=sample_user.user_id,
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
    db_session.add(action)
    await db_session.commit()
    await db_session.refresh(action)
    return action


async def test_create_rule_from_successful_pending_action(
    client,
    auth_headers,
    db_session: AsyncSession,
    sample_user: User,
    monkeypatch,
):
    monkeypatch.setattr(get_settings(), "ENABLE_AGENT_AUTOMATIONS", True)
    action = await _successful_pending_action(db_session, sample_user)

    response = await client.post(
        f"/api/v1/automations/from-pending-action/{action.id}",
        headers=auth_headers,
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["created"] is True
    assert payload["rule"]["agent_name"] == "agent_supplier"
    assert payload["rule"]["external_system"] == "GOOGLE_GMAIL"
    assert payload["rule"]["enabled"] is True


async def test_does_not_create_rule_for_failed_pending_action(
    client,
    auth_headers,
    db_session: AsyncSession,
    sample_user: User,
    monkeypatch,
):
    monkeypatch.setattr(get_settings(), "ENABLE_AGENT_AUTOMATIONS", True)
    action = await _successful_pending_action(db_session, sample_user)
    action.execution_status = "FAILED"
    await db_session.commit()

    response = await client.post(
        f"/api/v1/automations/from-pending-action/{action.id}",
        headers=auth_headers,
    )

    assert response.status_code == 409
    rules = (await db_session.execute(select(AgentAutomationRule))).scalars().all()
    assert rules == []


async def test_toggle_rule_enabled_disabled(
    client,
    auth_headers,
    db_session: AsyncSession,
    sample_user: User,
    monkeypatch,
):
    monkeypatch.setattr(get_settings(), "ENABLE_AGENT_AUTOMATIONS", True)
    action = await _successful_pending_action(db_session, sample_user)
    created = await client.post(
        f"/api/v1/automations/from-pending-action/{action.id}",
        headers=auth_headers,
    )
    rule_id = created.json()["rule"]["id"]

    disabled = await client.patch(
        f"/api/v1/automations/{rule_id}",
        headers=auth_headers,
        json={"enabled": False},
    )
    enabled = await client.patch(
        f"/api/v1/automations/{rule_id}",
        headers=auth_headers,
        json={"enabled": True},
    )

    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True


async def test_rules_are_isolated_by_tenant_and_user(
    client,
    auth_headers,
    second_auth_headers,
    db_session: AsyncSession,
    sample_user: User,
    monkeypatch,
):
    monkeypatch.setattr(get_settings(), "ENABLE_AGENT_AUTOMATIONS", True)
    action = await _successful_pending_action(db_session, sample_user)
    created = await client.post(
        f"/api/v1/automations/from-pending-action/{action.id}",
        headers=auth_headers,
    )
    rule_id = created.json()["rule"]["id"]

    other_list = await client.get("/api/v1/automations", headers=second_auth_headers)
    other_patch = await client.patch(
        f"/api/v1/automations/{rule_id}",
        headers=second_auth_headers,
        json={"enabled": False},
    )

    assert other_list.status_code == 200
    assert other_list.json() == []
    assert other_patch.status_code == 404


async def test_feature_flag_off_blocks_rule_creation(
    client,
    auth_headers,
    db_session: AsyncSession,
    sample_user: User,
    monkeypatch,
):
    monkeypatch.setattr(get_settings(), "ENABLE_AGENT_AUTOMATIONS", False)
    action = await _successful_pending_action(db_session, sample_user)

    response = await client.post(
        f"/api/v1/automations/from-pending-action/{action.id}",
        headers=auth_headers,
    )

    assert response.status_code == 404


async def test_unknown_rule_is_not_exposed(
    client,
    auth_headers,
):
    response = await client.patch(
        f"/api/v1/automations/{uuid.uuid4()}",
        headers=auth_headers,
        json={"enabled": False},
    )

    assert response.status_code == 404
