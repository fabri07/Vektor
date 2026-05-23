"""Tests para los contratos AgentTask y AgentTeamPlan (schemas Stage 1)."""

import uuid

import pytest
from pydantic import ValidationError

from app.application.agents.shared.schemas import (
    ActionType,
    AgentResponse,
    AgentTask,
    AgentTeamPlan,
    Confidence,
    RiskLevel,
)


# ── AgentTask ──────────────────────────────────────────────────────────────────

def test_agent_task_defaults():
    task = AgentTask(agent="agent_income", action_type=ActionType.REGISTER_SALE)
    assert isinstance(task.task_id, str)
    assert uuid.UUID(task.task_id)           # es un UUID válido
    assert task.entities == {}
    assert task.depends_on == []
    assert task.approval_group is None


def test_agent_task_entities_default_factory():
    """Dos tasks distintas no comparten el mismo dict de entities."""
    t1 = AgentTask(agent="agent_income", action_type=ActionType.REGISTER_SALE)
    t2 = AgentTask(agent="agent_expense", action_type=ActionType.REGISTER_EXPENSE)
    t1.entities["key"] = "val"
    assert "key" not in t2.entities, "Las entities son mutables compartidas (bug)"


def test_agent_task_depends_on_default_factory():
    """Dos tasks distintas no comparten la misma lista depends_on."""
    t1 = AgentTask(agent="agent_income", action_type=ActionType.REGISTER_SALE)
    t2 = AgentTask(agent="agent_stock", action_type=ActionType.UPDATE_STOCK)
    t1.depends_on.append("some-id")
    assert t2.depends_on == [], "depends_on es mutable compartido (bug)"


def test_agent_task_explicit_values():
    tid = str(uuid.uuid4())
    task = AgentTask(
        task_id=tid,
        agent="agent_health",
        action_type=ActionType.GENERATE_HEALTH_REPORT,
        entities={"days": 30},
        depends_on=["prev-task-id"],
        approval_group="group-abc",
    )
    assert task.task_id == tid
    assert task.entities["days"] == 30
    assert task.depends_on == ["prev-task-id"]
    assert task.approval_group == "group-abc"


def test_agent_task_requires_agent_and_action_type():
    with pytest.raises(ValidationError):
        AgentTask(action_type=ActionType.REGISTER_SALE)  # falta agent
    with pytest.raises(ValidationError):
        AgentTask(agent="agent_income")                   # falta action_type


# ── AgentTeamPlan ──────────────────────────────────────────────────────────────

def test_agent_team_plan_defaults():
    plan = AgentTeamPlan(intent="ingresar_venta")
    assert isinstance(plan.plan_id, str)
    assert uuid.UUID(plan.plan_id)
    assert plan.tasks == []
    assert plan.requires_synthesis is False
    assert plan.fallback_message is None


def test_agent_team_plan_tasks_default_factory():
    """Dos planes no comparten la misma lista de tasks."""
    p1 = AgentTeamPlan(intent="a")
    p2 = AgentTeamPlan(intent="b")
    p1.tasks.append(AgentTask(agent="agent_income", action_type=ActionType.REGISTER_SALE))
    assert len(p2.tasks) == 0, "tasks es mutable compartido (bug)"


def test_agent_team_plan_with_tasks():
    task = AgentTask(agent="agent_stock", action_type=ActionType.UPDATE_STOCK)
    plan = AgentTeamPlan(
        plan_id="plan-123",
        intent="actualizar_stock",
        tasks=[task],
        requires_synthesis=False,
    )
    assert plan.plan_id == "plan-123"
    assert len(plan.tasks) == 1
    assert plan.tasks[0].action_type == ActionType.UPDATE_STOCK


def test_agent_team_plan_model_dump_serializable():
    """model_dump() debe producir un dict que se puede guardar en JSON (sin tipos no serializables)."""
    import json
    task = AgentTask(agent="agent_income", action_type=ActionType.REGISTER_SALE, entities={"x": 1})
    plan = AgentTeamPlan(intent="ingresar_venta", tasks=[task])
    dumped = plan.model_dump()
    json_str = json.dumps(dumped)  # no debe lanzar
    assert '"ingresar_venta"' in json_str


# ── AgentResponse: nuevos campos Stage 1 ──────────────────────────────────────

def test_agent_response_pending_action_ids_optional():
    """pending_action_ids y approval_group_id son opcionales y default None."""
    r = AgentResponse(
        request_id="req-1",
        agent_name="agent_income",
        status="success",
        risk_level=RiskLevel.MEDIUM,
    )
    assert r.pending_action_ids is None
    assert r.approval_group_id is None


def test_agent_response_pending_action_ids_populated():
    r = AgentResponse(
        request_id="req-2",
        agent_name="agent_income",
        status="requires_approval",
        risk_level=RiskLevel.MEDIUM,
        requires_approval=True,
        pending_action_ids=["pa-1", "pa-2"],
        approval_group_id="group-xyz",
    )
    assert r.pending_action_ids == ["pa-1", "pa-2"]
    assert r.approval_group_id == "group-xyz"


def test_agent_response_plan_in_result():
    """El plan puede ser almacenado en result['plan'] como dict serializable."""
    task = AgentTask(agent="agent_health", action_type=ActionType.GENERATE_HEALTH_REPORT)
    plan = AgentTeamPlan(intent="consultar_estado_negocio", tasks=[task])
    r = AgentResponse(
        request_id="req-3",
        agent_name="agent_health",
        status="success",
        risk_level=RiskLevel.LOW,
        result={
            "intent": "consultar_estado_negocio",
            "target_agent": "agent_health",
            "plan": plan.model_dump(),
        },
    )
    assert r.result["plan"]["intent"] == "consultar_estado_negocio"
    assert len(r.result["plan"]["tasks"]) == 1
