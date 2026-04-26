"""Agent automation rule endpoints."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user
from app.application.services.automation_service import (
    create_rule_from_action,
    serialize_rule,
)
from app.config.settings import get_settings
from app.persistence.db.session import get_db_session
from app.persistence.models.agent_automation_rule import AgentAutomationRule
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.pending_action import PendingAction
from app.persistence.models.user import User
from app.schemas.automations import (
    AutomationRuleCreateResponse,
    AutomationRuleResponse,
    AutomationRuleUpdateRequest,
)

router = APIRouter()


@router.get("", response_model=list[AutomationRuleResponse])
async def list_automations(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> list[dict]:
    stmt = (
        select(AgentAutomationRule)
        .where(
            AgentAutomationRule.tenant_id == current_user.tenant_id,
            AgentAutomationRule.user_id == current_user.user_id,
        )
        .order_by(AgentAutomationRule.created_at.desc())
    )
    rules = (await db.execute(stmt)).scalars().all()
    return [serialize_rule(rule) for rule in rules]


@router.post(
    "/from-pending-action/{pending_id}",
    response_model=AutomationRuleCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_automation_from_pending_action(
    pending_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    if not get_settings().ENABLE_AGENT_AUTOMATIONS:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Automatizaciones no disponibles en este entorno.",
        )

    action = await db.get(PendingAction, pending_id)
    if (
        action is None
        or action.tenant_id != current_user.tenant_id
        or action.user_id != current_user.user_id
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Acción no encontrada.")

    try:
        rule, created = await create_rule_from_action(db=db, action=action)
    except ValueError as exc:
        if str(exc) == "automation_rule_limit_reached":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Límite de automatizaciones activas alcanzado.",
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Solo se pueden automatizar acciones aprobadas y ejecutadas correctamente.",
        ) from exc

    await db.commit()
    return {"rule": serialize_rule(rule), "created": created}


@router.patch("/{rule_id}", response_model=AutomationRuleResponse)
async def update_automation(
    rule_id: uuid.UUID,
    body: AutomationRuleUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    rule = await db.get(AgentAutomationRule, rule_id)
    if (
        rule is None
        or rule.tenant_id != current_user.tenant_id
        or rule.user_id != current_user.user_id
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Regla no encontrada.")

    changed = rule.enabled != body.enabled
    rule.enabled = body.enabled
    rule.updated_at = datetime.now(UTC)
    if changed:
        db.add(
            DecisionAuditLog(
                id=uuid.uuid4(),
                tenant_id=current_user.tenant_id,
                decision_type=(
                    "AUTOMATION_RULE_ENABLED" if body.enabled else "AUTOMATION_RULE_DISABLED"
                ),
                decision_data={
                    "rule_id": str(rule.id),
                    "rule_key": rule.rule_key,
                    "action_type": rule.action_type,
                },
                triggered_by="settings:automations",
                actor_user_id=current_user.user_id,
                context={"agent_name": rule.agent_name, "external_system": rule.external_system},
                created_at=datetime.now(UTC),
            )
        )
    await db.commit()
    await db.refresh(rule)
    return serialize_rule(rule)


@router.delete("/{rule_id}")
async def delete_automation(
    rule_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_session),
) -> dict:
    rule = await db.get(AgentAutomationRule, rule_id)
    if (
        rule is None
        or rule.tenant_id != current_user.tenant_id
        or rule.user_id != current_user.user_id
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Regla no encontrada.")

    db.add(
        DecisionAuditLog(
            id=uuid.uuid4(),
            tenant_id=current_user.tenant_id,
            decision_type="AUTOMATION_RULE_DELETED",
            decision_data={
                "rule_id": str(rule.id),
                "rule_key": rule.rule_key,
                "action_type": rule.action_type,
            },
            triggered_by="settings:automations",
            actor_user_id=current_user.user_id,
            context={"agent_name": rule.agent_name, "external_system": rule.external_system},
            created_at=datetime.now(UTC),
        )
    )
    await db.delete(rule)
    await db.commit()
    return {"ok": True}
