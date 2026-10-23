"""Schemas for agent automation rules."""

from typing import Any

from pydantic import BaseModel


class AutomationRuleResponse(BaseModel):
    id: str
    agent_name: str
    action_type: str
    external_system: str | None
    rule_key: str
    criteria: dict[str, Any]
    enabled: bool
    risk_level: str
    created_from_pending_action_id: str | None
    last_executed_at: str | None
    created_at: str
    updated_at: str


class AutomationRuleUpdateRequest(BaseModel):
    enabled: bool


class AutomationRuleCreateResponse(BaseModel):
    rule: AutomationRuleResponse
    created: bool
