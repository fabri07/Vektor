"""Agent automation rules service.

Rules are explicit per-user consent records. They intentionally match only a
stable fingerprint of the action payload, not free-form chat text.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.observability.logger import get_logger
from app.persistence.models.agent_automation_rule import AgentAutomationRule
from app.persistence.models.audit import DecisionAuditLog
from app.persistence.models.pending_action import PendingAction

logger = get_logger(__name__)

MAX_ACTIVE_RULES_PER_USER = 50
AUTOMATION_PAYLOAD_AGENT_KEY = "_agent_name"


def _clean(value: object) -> str:
    return str(value or "").strip().lower()[:120]


def determine_external_system(action_type: str, payload: dict[str, Any]) -> str | None:
    if payload.get("external_system"):
        return str(payload["external_system"])
    if action_type in {"CREATE_SUPPLIER_DRAFT", "CLASSIFY_GMAIL_MESSAGE"}:
        return "GOOGLE_GMAIL"
    if action_type == "CREATE_CALENDAR_EVENT":
        if payload.get("send_email") or payload.get("email_mode"):
            return "GOOGLE_CALENDAR,GOOGLE_GMAIL"
        return "GOOGLE_CALENDAR"
    if action_type == "SYNC_TO_GOOGLE":
        sync_type = str(payload.get("sync_type", ""))
        if "drive" in sync_type:
            return "GOOGLE_DRIVE"
        if "docs" in sync_type:
            return "GOOGLE_DOCS"
        if "gmail" in sync_type:
            return "GOOGLE_GMAIL"
        if "calendar" in sync_type:
            return "GOOGLE_CALENDAR"
        return "GOOGLE_SHEETS"
    # Stage 4: nuevos ActionTypes Google
    if action_type == "UPLOAD_TO_DRIVE":
        return "GOOGLE_DRIVE"
    if action_type == "CREATE_GOOGLE_DOC":
        return "GOOGLE_DOCS"
    if action_type == "APPEND_TO_SHEET":
        return "GOOGLE_SHEETS"
    return None


def build_rule_criteria(
    *,
    agent_name: str,
    action_type: str,
    payload: dict[str, Any],
    external_system: str | None,
) -> dict[str, Any]:
    sync_type = _clean(payload.get("sync_type"))
    criteria: dict[str, Any] = {
        "agent_name": agent_name,
        "action_type": action_type,
        "external_system": external_system,
        "mode": _clean(payload.get("mode")),
        "sync_type": sync_type,
    }

    stable_fields = (
        "category",
        "payment_method",
        "service_type",
        "provider_email",
        "to",
        "spreadsheet_id",
        "range_name",
        "query",
        "folder_id",
        "calendar_id",
        "email_mode",
    )
    for field in stable_fields:
        if payload.get(field) not in (None, "", []):
            criteria[field] = _clean(payload.get(field))

    if action_type == "CREATE_SUPPLIER_DRAFT" and "to" not in criteria:
        criteria["to"] = _clean(payload.get("provider_email"))

    return {key: value for key, value in criteria.items() if value not in ("", None)}


def build_rule_key(
    *,
    agent_name: str,
    action_type: str,
    payload: dict[str, Any],
    external_system: str | None = None,
) -> str:
    external = external_system or determine_external_system(action_type, payload)
    criteria = build_rule_criteria(
        agent_name=agent_name,
        action_type=action_type,
        payload=payload,
        external_system=external,
    )
    stable = "|".join(f"{key}={criteria[key]}" for key in sorted(criteria))
    digest = hashlib.sha256(stable.encode()).hexdigest()[:16]
    app = _clean(external or "local").replace(",", "+")
    operation = _clean(payload.get("sync_type") or action_type).replace(" ", "_")
    return f"{agent_name}:{app}:{operation}:{digest}"


def automation_offer_for_action(action: PendingAction) -> dict[str, Any]:
    payload = action.payload or {}
    agent_name = str(payload.get(AUTOMATION_PAYLOAD_AGENT_KEY) or "agent_unknown")
    action_type = str(action.action_type)
    external_system = action.external_system or determine_external_system(action_type, payload)
    rule_key = build_rule_key(
        agent_name=agent_name,
        action_type=action_type,
        payload=payload,
        external_system=external_system,
    )
    criteria = build_rule_criteria(
        agent_name=agent_name,
        action_type=action_type,
        payload=payload,
        external_system=external_system,
    )
    return {
        "pending_action_id": str(action.id),
        "agent_name": agent_name,
        "action_type": action_type,
        "external_system": external_system,
        "rule_key": rule_key,
        "criteria": criteria,
        "risk_level": action.risk_level,
        "message": "¿Querés automatizar acciones similares en el futuro?",
    }


async def find_enabled_rule(
    *,
    db: AsyncSession,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    agent_name: str,
    action_type: str,
    payload: dict[str, Any],
    external_system: str | None = None,
) -> AgentAutomationRule | None:
    rule_key = build_rule_key(
        agent_name=agent_name,
        action_type=action_type,
        payload=payload,
        external_system=external_system,
    )
    stmt = select(AgentAutomationRule).where(
        AgentAutomationRule.tenant_id == tenant_id,
        AgentAutomationRule.user_id == user_id,
        AgentAutomationRule.rule_key == rule_key,
        AgentAutomationRule.enabled.is_(True),
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def create_rule_from_action(
    *,
    db: AsyncSession,
    action: PendingAction,
) -> tuple[AgentAutomationRule, bool]:
    if action.status != "APPROVED" or action.execution_status != "SUCCEEDED":
        raise ValueError("pending_action_not_successful")

    payload = action.payload or {}
    agent_name = str(payload.get(AUTOMATION_PAYLOAD_AGENT_KEY) or "agent_unknown")
    external_system = action.external_system or determine_external_system(
        action.action_type,
        payload,
    )
    rule_key = build_rule_key(
        agent_name=agent_name,
        action_type=action.action_type,
        payload=payload,
        external_system=external_system,
    )
    criteria = build_rule_criteria(
        agent_name=agent_name,
        action_type=action.action_type,
        payload=payload,
        external_system=external_system,
    )

    existing = await db.execute(
        select(AgentAutomationRule).where(
            AgentAutomationRule.tenant_id == action.tenant_id,
            AgentAutomationRule.user_id == action.user_id,
            AgentAutomationRule.rule_key == rule_key,
        )
    )
    rule = existing.scalar_one_or_none()
    created = False
    if rule is None:
        active_count = await db.scalar(
            select(func.count()).select_from(AgentAutomationRule).where(
                AgentAutomationRule.tenant_id == action.tenant_id,
                AgentAutomationRule.user_id == action.user_id,
                AgentAutomationRule.enabled.is_(True),
            )
        )
        if int(active_count or 0) >= MAX_ACTIVE_RULES_PER_USER:
            raise ValueError("automation_rule_limit_reached")
        rule = AgentAutomationRule(
            tenant_id=action.tenant_id,
            user_id=action.user_id,
            agent_name=agent_name,
            action_type=action.action_type,
            external_system=external_system,
            rule_key=rule_key,
            criteria=criteria,
            enabled=True,
            risk_level=action.risk_level,
            created_from_pending_action_id=action.id,
        )
        db.add(rule)
        created = True
        decision_type = "AUTOMATION_RULE_CREATED"
    else:
        rule.enabled = True
        rule.criteria = criteria
        rule.risk_level = action.risk_level
        rule.external_system = external_system
        decision_type = "AUTOMATION_RULE_ENABLED"

    db.add(
        DecisionAuditLog(
            id=uuid.uuid4(),
            tenant_id=action.tenant_id,
            decision_type=decision_type,
            decision_data={
                "rule_key": rule_key,
                "action_type": action.action_type,
                "pending_action_id": str(action.id),
            },
            triggered_by="agent:automation",
            actor_user_id=action.user_id,
            context={"agent_name": agent_name, "external_system": external_system},
            created_at=datetime.now(UTC),
        )
    )
    await db.flush()
    return rule, created


def serialize_rule(rule: AgentAutomationRule) -> dict[str, Any]:
    return {
        "id": str(rule.id),
        "agent_name": rule.agent_name,
        "action_type": rule.action_type,
        "external_system": rule.external_system,
        "rule_key": rule.rule_key,
        "criteria": rule.criteria or {},
        "enabled": rule.enabled,
        "risk_level": rule.risk_level,
        "created_from_pending_action_id": (
            str(rule.created_from_pending_action_id)
            if rule.created_from_pending_action_id
            else None
        ),
        "last_executed_at": rule.last_executed_at.isoformat() if rule.last_executed_at else None,
        "created_at": rule.created_at.isoformat(),
        "updated_at": rule.updated_at.isoformat(),
    }
